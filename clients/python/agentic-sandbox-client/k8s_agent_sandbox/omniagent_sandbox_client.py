# Copyright 2026 The Kubernetes Authors.
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
OmniAgent Sandbox Client - Per-session sandbox with per-command execution.

Each session_id maps to a unique SandboxClaim/Pod. The sandbox is automatically
created on first use and persists until explicitly destroyed.
"""

import logging
import sys
import time

from kubernetes import client as k8s_client

from .connector import SandboxConnector
from .k8s_helper import K8sHelper
from .models import (
    ExecutionResult,
    SandboxConnectionConfig,
    SandboxLocalTunnelConnectionConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)


class OmniAgentSandboxClient:
    """
    A simplified client for persistent sandbox sessions.

    Usage:
        client = OmniAgentSandboxClient(template_name="omniagent-sandbox")
        result = client.run("my-session", "echo hello")
        client.destroy("my-session")
    """

    def __init__(
        self,
        template_name: str,
        namespace: str = "default",
        connection_config: SandboxConnectionConfig | None = None,
        sandbox_ready_timeout: int = 180,
    ):
        self.template_name = template_name
        self.namespace = namespace
        self.connection_config = (
            connection_config or SandboxLocalTunnelConnectionConfig()
        )
        self.sandbox_ready_timeout = sandbox_ready_timeout

        self.k8s_helper = K8sHelper()

        # Track active connectors: session_id -> SandboxConnector
        self._connectors: dict[str, SandboxConnector] = {}

    def run(
        self,
        session_id: str,
        command: str,
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """
        Execute a command in the sandbox for the given session.
        Creates the sandbox automatically if it doesn't exist.

        If ``env`` is provided, the sandbox runtime merges it with the
        container's environ before launching the subprocess (user values win).
        """
        connector = self._ensure_sandbox(session_id)
        payload: dict[str, object] = {"command": command, "timeout": timeout}
        if env:
            payload["env"] = env
        response = connector.send_request(
            "POST",
            "execute",
            json=payload,
            timeout=timeout + 10,
        )
        return ExecutionResult(**response.json())

    def write(self, session_id: str, path: str, content: bytes | str) -> None:
        """Upload a file to the sandbox."""
        connector = self._ensure_sandbox(session_id)
        if isinstance(content, str):
            content = content.encode("utf-8")
        connector.send_request("POST", "upload", files={"file": (path, content)})

    def read(self, session_id: str, path: str) -> bytes:
        """Download a file from the sandbox."""
        connector = self._ensure_sandbox(session_id)
        response = connector.send_request("GET", f"download/{path}")
        return response.content

    def destroy(self, session_id: str) -> None:
        """Destroy the sandbox for the given session."""
        if session_id in self._connectors:
            self._connectors.pop(session_id).close()
        self.k8s_helper.delete_sandbox_claim(session_id, self.namespace)

    def _ensure_sandbox(self, session_id: str) -> SandboxConnector:
        """Ensure sandbox exists and return its connector."""
        if session_id in self._connectors:
            return self._connectors[session_id]

        # Create claim if it doesn't already exist
        try:
            self.k8s_helper.create_sandbox_claim(
                session_id, self.template_name, self.namespace
            )
        except k8s_client.ApiException as e:
            if e.status != 409:
                raise
            logging.info(f"SandboxClaim already exists: {session_id}")

        # Resolve sandbox name (supports warm pool adoption)
        start_time = time.monotonic()
        sandbox_id = self.k8s_helper.resolve_sandbox_name(
            session_id, self.namespace, self.sandbox_ready_timeout
        )
        elapsed = time.monotonic() - start_time
        remaining_timeout = max(1, int(self.sandbox_ready_timeout - elapsed))

        # Wait for sandbox to become ready
        self.k8s_helper.wait_for_sandbox_ready(
            sandbox_id, self.namespace, remaining_timeout
        )

        connector = SandboxConnector(
            sandbox_id=sandbox_id,
            namespace=self.namespace,
            connection_config=self.connection_config,
            k8s_helper=self.k8s_helper,
        )
        self._connectors[session_id] = connector
        return connector
