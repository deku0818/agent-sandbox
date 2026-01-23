# Copyright 2025 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
PersistentSandboxClient - A simplified client for persistent sandbox sessions.

Each session_id maps to a unique SandboxClaim/Pod. The sandbox is automatically
created on first use and persists until explicitly destroyed.
"""

import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import requests
from kubernetes import client, config, watch
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CLAIM_API_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1alpha1"
CLAIM_PLURAL_NAME = "sandboxclaims"

SANDBOX_API_GROUP = "agents.x-k8s.io"
SANDBOX_API_VERSION = "v1alpha1"
SANDBOX_PLURAL_NAME = "sandboxes"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)


@dataclass
class ExecutionResult:
    """Result of a command execution."""
    stdout: str
    stderr: str
    exit_code: int
    cwd: str = ""


class PersistentSandboxClient:
    """
    A simplified client for persistent sandbox sessions.

    Usage:
        client = PersistentSandboxClient(template_name="persistent-session")
        result = client.run("my-session", "echo hello")
        result = client.run("my-session", "export FOO=bar")
        result = client.run("my-session", "echo $FOO")  # outputs: bar
        client.destroy("my-session")
    """

    def __init__(
        self,
        template_name: str,
        namespace: str = "default",
        gateway_name: str | None = None,
        gateway_namespace: str = "default",
        api_url: str | None = None,
        server_port: int = 8888,
        sandbox_ready_timeout: int = 180,
        gateway_ready_timeout: int = 180,
    ):
        self.template_name = template_name
        self.namespace = namespace
        self.gateway_name = gateway_name
        self.gateway_namespace = gateway_namespace
        self.api_url = api_url
        self.server_port = server_port
        self.sandbox_ready_timeout = sandbox_ready_timeout
        self.gateway_ready_timeout = gateway_ready_timeout

        # Track active connections: session_id -> (process | None, base_url)
        self._tunnels: dict[str, tuple[subprocess.Popen | None, str]] = {}

        # Initialize Kubernetes client
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        self.k8s_api = client.CustomObjectsApi()

        # HTTP session with retries
        self.http = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        self.http.mount("http://", HTTPAdapter(max_retries=retries))

    def run(self, session_id: str, command: str, timeout: int = 60) -> ExecutionResult:
        """
        Execute a command in the persistent shell for the given session.
        Creates the sandbox automatically if it doesn't exist.
        """
        base_url = self._ensure_sandbox(session_id)

        response = self._request(base_url, session_id, "POST", "execute",
                                 json={"command": command, "timeout": timeout},
                                 timeout=timeout + 10)
        data = response.json()
        return ExecutionResult(
            stdout=data.get('stdout', ''),
            stderr=data.get('stderr', ''),
            exit_code=data.get('exit_code', -1),
            cwd=data.get('cwd', '')
        )

    def write(self, session_id: str, path: str, content: bytes | str) -> None:
        """Upload a file to the sandbox."""
        base_url = self._ensure_sandbox(session_id)

        if isinstance(content, str):
            content = content.encode('utf-8')

        self._request(base_url, session_id, "POST", "upload",
                      files={'file': (os.path.basename(path), content)})

    def read(self, session_id: str, path: str) -> bytes:
        """Download a file from the sandbox."""
        base_url = self._ensure_sandbox(session_id)
        response = self._request(base_url, session_id, "GET", f"download/{path}")
        return response.content

    def destroy(self, session_id: str) -> None:
        """Destroy the sandbox for the given session."""
        # Stop tunnel if exists
        if session_id in self._tunnels:
            proc, _ = self._tunnels.pop(session_id)
            if proc is not None:
                proc.terminate()

        # Delete SandboxClaim
        try:
            self.k8s_api.delete_namespaced_custom_object(
                group=CLAIM_API_GROUP,
                version=CLAIM_API_VERSION,
                namespace=self.namespace,
                plural=CLAIM_PLURAL_NAME,
                name=session_id
            )
            logging.info(f"Destroyed sandbox: {session_id}")
        except client.ApiException as e:
            if e.status != 404:
                raise

    def _ensure_sandbox(self, session_id: str) -> str:
        """Ensure sandbox exists and return its base_url."""
        # Check if we already have a connection
        if session_id in self._tunnels:
            proc, base_url = self._tunnels[session_id]
            if proc is None or proc.poll() is None:  # No process needed or still running
                return base_url
            del self._tunnels[session_id]

        # Check if sandbox exists
        if not self._sandbox_exists(session_id):
            self._create_sandbox(session_id)

        # Establish connection based on configuration
        if self.api_url:
            # Case 1: API URL provided manually
            self._tunnels[session_id] = (None, self.api_url)
            return self.api_url
        elif self.gateway_name:
            # Case 2: Gateway mode
            base_url = self._get_gateway_url()
            self._tunnels[session_id] = (None, base_url)
            return base_url
        else:
            # Case 3: Dev mode (port-forward)
            return self._start_tunnel(session_id)

    def _sandbox_exists(self, session_id: str) -> bool:
        """Check if sandbox exists and is ready."""
        try:
            sandbox = self.k8s_api.get_namespaced_custom_object(
                group=SANDBOX_API_GROUP,
                version=SANDBOX_API_VERSION,
                namespace=self.namespace,
                plural=SANDBOX_PLURAL_NAME,
                name=session_id
            )
            for cond in sandbox.get('status', {}).get('conditions', []):
                if cond.get('type') == 'Ready' and cond.get('status') == 'True':
                    return True
            return False
        except client.ApiException as e:
            if e.status == 404:
                return False
            raise

    def _create_sandbox(self, session_id: str) -> None:
        """Create a new sandbox."""
        manifest = {
            "apiVersion": f"{CLAIM_API_GROUP}/{CLAIM_API_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {"name": session_id},
            "spec": {"sandboxTemplateRef": {"name": self.template_name}}
        }

        logging.info(f"Creating sandbox: {session_id}")
        try:
            self.k8s_api.create_namespaced_custom_object(
                group=CLAIM_API_GROUP,
                version=CLAIM_API_VERSION,
                namespace=self.namespace,
                plural=CLAIM_PLURAL_NAME,
                body=manifest
            )
        except client.ApiException as e:
            if e.status == 409:
                # SandboxClaim already exists, continue to wait for it to be ready
                logging.info(f"SandboxClaim already exists: {session_id}, waiting for ready")
            else:
                raise

        # Wait for ready
        w = watch.Watch()
        for event in w.stream(
            func=self.k8s_api.list_namespaced_custom_object,
            namespace=self.namespace,
            group=SANDBOX_API_GROUP,
            version=SANDBOX_API_VERSION,
            plural=SANDBOX_PLURAL_NAME,
            field_selector=f"metadata.name={session_id}",
            timeout_seconds=self.sandbox_ready_timeout
        ):
            if event["type"] in ["ADDED", "MODIFIED"]:
                for cond in event['object'].get('status', {}).get('conditions', []):
                    if cond.get('type') == 'Ready' and cond.get('status') == 'True':
                        logging.info(f"Sandbox ready: {session_id}")
                        w.stop()
                        return

        raise TimeoutError(f"Sandbox {session_id} did not become ready")

    def _get_gateway_url(self) -> str:
        """Get URL from Gateway."""
        w = watch.Watch()
        for event in w.stream(
            func=self.k8s_api.list_namespaced_custom_object,
            namespace=self.gateway_namespace,
            group="gateway.networking.k8s.io",
            version="v1",
            plural="gateways",
            field_selector=f"metadata.name={self.gateway_name}",
            timeout_seconds=self.gateway_ready_timeout,
        ):
            if event["type"] in ["ADDED", "MODIFIED"]:
                addresses = event['object'].get('status', {}).get('addresses', [])
                if addresses:
                    ip = addresses[0].get('value')
                    if ip:
                        w.stop()
                        return f"http://{ip}"
        raise TimeoutError(f"Gateway {self.gateway_name} did not get an IP")

    def _start_tunnel(self, session_id: str) -> str:
        """Start port-forward tunnel to the sandbox."""
        # Find free port
        with socket.socket() as s:
            s.bind(('', 0))
            port = s.getsockname()[1]

        proc = subprocess.Popen(
            ["kubectl", "port-forward", "svc/sandbox-router-svc",
             f"{port}:8080", "-n", self.namespace],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # Wait for tunnel to be ready
        for _ in range(30):
            if proc.poll() is not None:
                raise RuntimeError("Port-forward failed")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    base_url = f"http://127.0.0.1:{port}"
                    self._tunnels[session_id] = (proc, base_url)
                    return base_url
            except (TimeoutError):
                time.sleep(0.5)

        proc.kill()
        raise TimeoutError("Failed to establish tunnel")

    def _request(self, base_url: str, session_id: str,
                 method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make HTTP request to sandbox."""
        url = f"{base_url}/{endpoint}"
        headers = kwargs.pop("headers", {})
        headers["X-Sandbox-ID"] = session_id
        headers["X-Sandbox-Namespace"] = self.namespace
        headers["X-Sandbox-Port"] = str(self.server_port)

        response = self.http.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response
