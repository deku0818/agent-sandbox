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
import os
import sys
import time

from kubernetes import client as k8s_client

from .connector import SandboxConnector
from .exceptions import SandboxNotFoundError
from .k8s_helper import K8sHelper
from .models import (
    ExecutionResult,
    SandboxConnectionConfig,
    SandboxInClusterConnectionConfig,
    SandboxLocalTunnelConnectionConfig,
)
from .utils import construct_sandbox_claim_lifecycle_spec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)


def _default_connection_config() -> SandboxConnectionConfig:
    # 集群内（KUBERNETES_SERVICE_HOST 存在）→ in-cluster DNS 直连,绕开 router 和
    # kubectl port-forward 子进程,延迟和资源占用都更优;集群外保留 port-forward
    # 兜底,保持本地开发体验不变。
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return SandboxInClusterConnectionConfig()
    return SandboxLocalTunnelConnectionConfig()


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
        shutdown_after_seconds: int | None = None,
        idle_timeout_seconds: int | None = None,
    ):
        self.template_name = template_name
        self.namespace = namespace
        self.connection_config = connection_config or _default_connection_config()
        self.sandbox_ready_timeout = sandbox_ready_timeout
        # TTL 兜底,两种模式互斥语义:
        # - shutdown_after_seconds: hard deadline,sandbox 创建后 N 秒整死,不续。
        # - idle_timeout_seconds:    idle TTL(类 E2B),每次 run/write/read 调用
        #   都把 deadline 推到 now+N。同时传两个时 idle 接管(初始与续期都用它)。
        self.shutdown_after_seconds = shutdown_after_seconds
        self.idle_timeout_seconds = idle_timeout_seconds

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

    def _touch_or_invalidate(self, session_id: str) -> bool:
        """续 TTL 并据此判断缓存的 connector 是否仍指向活的沙箱。

        SandboxClaim 已被 controller GC 时,patch 返回 ``404``——这是权威失效
        信号(比"撞请求 502 才知道"早一跳)。检测到 404 立即把缓存 connector
        pop 掉,由 ``_ensure_sandbox`` 抛 ``SandboxNotFoundError`` 让调用方走
        完整 destroy + rebuild,以便业务级 bootstrap 重做。

        Returns:
            True  - 沙箱仍健康(已续 TTL,或本就没启用 idle 模式)
            False - 沙箱已 GC(缓存 connector 已清,调用方应处理失效)

        其他 patch 失败(K8s API 短暂不可达等)按软失败处理:warn 后照常复用
        connector——下次调用会再续,真坏了 send_request 也会暴露问题。
        """
        if self.idle_timeout_seconds is None:
            # 非 idle 模式没有续 TTL 的入口,失效检测兜底依赖请求路径的 connection error
            return True

        lifecycle = construct_sandbox_claim_lifecycle_spec(self.idle_timeout_seconds)
        try:
            self.k8s_helper.patch_sandbox_claim_lifecycle(
                session_id, self.namespace, lifecycle
            )
            return True
        except k8s_client.ApiException as e:
            if e.status == 404:
                logging.info(
                    f"SandboxClaim {session_id} not found—evicting stale connector"
                )
                stale = self._connectors.pop(session_id, None)
                if stale is not None:
                    try:
                        stale.close()
                    except Exception as close_err:
                        logging.debug(
                            f"Closing stale connector for {session_id} failed: {close_err}"
                        )
                return False
            logging.warning(f"Failed to renew TTL for {session_id}: {e}")
            return True
        except Exception as e:
            logging.warning(f"Failed to renew TTL for {session_id}: {e}")
            return True

    def destroy(self, session_id: str) -> None:
        """Destroy the sandbox for the given session."""
        if session_id in self._connectors:
            self._connectors.pop(session_id).close()
        self.k8s_helper.delete_sandbox_claim(session_id, self.namespace)

    def _ensure_sandbox(self, session_id: str) -> SandboxConnector:
        """Ensure sandbox exists and return its connector.

        三种情况:

        1. 缓存命中 + 沙箱仍活: ``_touch_or_invalidate`` 续 TTL 成功 → 直接返
           原 connector。整次调用最多一个 K8s patch(本来就要做来续 TTL,零额
           外开销)。
        2. 缓存命中 + 沙箱已被 controller GC: ``_touch_or_invalidate`` 检测
           到 patch 404,已 pop 掉陈旧 connector,**抛 ``SandboxNotFoundError``**
           让调用方走完整 destroy + rebuild。不在 SDK 内默默重建——SandboxClaim
           被 GC 意味着 Pod 文件系统全丢,调用方往往有业务级 bootstrap(如
           安装包、同步配置文件、注入环境变量)要重做;SDK 越权静默重建会
           隐藏这件事,导致新 Pod 是"裸"状态。
        3. 缓存里没有(首次调用): 保留 create-if-missing 便利路径——首次创
           建沙箱由 SDK 自动完成。
        """
        if session_id in self._connectors:
            if self._touch_or_invalidate(session_id):
                return self._connectors[session_id]
            raise SandboxNotFoundError(
                f"SandboxClaim {session_id} no longer exists "
                f"(idle TTL GC or external delete); connector cache cleared. "
                f"Call destroy(session_id) then retry to rebuild from scratch, "
                f"or use a fresh session_id."
            )

        # Create claim if it doesn't already exist。初始 lifecycle:idle 模式优先
        # (避免创建后到第一次 _touch_or_invalidate 之间出现空窗),否则用 hard
        # deadline;两者都没设就不带 lifecycle,沿用 SandboxClaim 默认行为。
        initial_ttl = self.idle_timeout_seconds or self.shutdown_after_seconds
        lifecycle = (
            construct_sandbox_claim_lifecycle_spec(initial_ttl)
            if initial_ttl is not None
            else None
        )
        try:
            self.k8s_helper.create_sandbox_claim(
                session_id,
                self.template_name,
                self.namespace,
                lifecycle=lifecycle,
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

        # get_pod_ip 只有 InClusterConnectionStrategy(use_pod_ip=True)会调用。
        # 闭包绑定 sandbox_id 保证多 session 互不串。
        def get_pod_ip() -> str | None:
            obj = self.k8s_helper.get_sandbox(sandbox_id, self.namespace) or {}
            pod_ips = (obj.get("status") or {}).get("podIPs") or []
            return pod_ips[0] if pod_ips else None

        connector = SandboxConnector(
            sandbox_id=sandbox_id,
            namespace=self.namespace,
            connection_config=self.connection_config,
            k8s_helper=self.k8s_helper,
            get_pod_ip=get_pod_ip,
        )
        self._connectors[session_id] = connector
        return connector
