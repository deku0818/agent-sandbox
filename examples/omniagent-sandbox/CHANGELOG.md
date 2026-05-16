# Changelog

## [0.2.5] - 2026-05-16

### Added
- `OmniAgentSandboxClient` 新增 `shutdown_after_seconds` 参数:hard deadline 模式,sandbox 创建后 N 秒由 controller 自动 `Delete`,防止调用方异常退出忘了 `destroy` 导致 sandbox 长期占资源
- `OmniAgentSandboxClient` 新增 `idle_timeout_seconds` 参数:E2B 风格 idle TTL,每次 `run` / `write` / `read` 调用都把 `SandboxClaim.spec.lifecycle.shutdownTime` 推到 `now+N`,活跃则续期;两参数同传时 idle 接管
- `K8sHelper.patch_sandbox_claim_lifecycle(name, namespace, lifecycle)` 方法,封装 `CustomObjectsApi.patch_namespaced_custom_object` 用于 idle TTL 续期
- `OmniAgentSandboxClient` 默认连接配置改为根据 `KUBERNETES_SERVICE_HOST` env 自动选择:集群内走 `SandboxInClusterConnectionConfig`(`svc.cluster.local` DNS 直连,绕开 kubectl port-forward 子进程),集群外保留 `SandboxLocalTunnelConnectionConfig`
- `_ensure_sandbox` 为 `SandboxConnector` 注入按 sandbox_id 闭包绑定的 `get_pod_ip` callback,支持 `SandboxInClusterConnectionConfig(use_pod_ip=True)` 时绕开 DNS 走 pod IP
- Sandbox runtime 新增 `/healthz` 和 `/readyz` 路由别名,对齐 K8s probe 惯用路径;`/` 保留向后兼容

### Changed
- `/execute` 端点每次结束输出结构化日志(`cmd_len` / `exit_code` / `duration_ms` / `cwd`),timeout 路径对齐相同字段;原先仅 timeout 路径 log,正常路径完全静默,排查链路问题无据可查

## [0.2.4] - 2026-05-15

### Changed
- Sandbox runtime 容器身份模型重构对齐 E2B envd:server 以 root 起,`/execute` 通过 subprocess `user=AGENT_UID` 切到 agent 身份,`/upload` 落盘后 chown 文件与新建中间目录链给 agent;agent 身份与 server 身份解耦,改 `AGENT_UID` env 无需重建镜像
- Dockerfile 新建标准 `user` (UID 1000,`HOME=/home/user`),移除 `USER 1000` 与 `NPM_CONFIG_PREFIX` hack;Lark CLI 改装到默认全局 prefix `/usr`,不再依赖 HOME 内 npm-global
- 非 root 起 server 时退化为旧行为(subprocess 沿用当前身份、upload 不 chown),向后兼容

## [0.2.3] - 2026-04-21

### Added
- `ExecuteRequest` 新增 `env` 字段，客户端可在每次 execute 时注入自定义环境变量，容器侧将其与 `os.environ` 合并（用户值优先）后传给子进程
- `OmniAgentSandboxClient.run` 新增 `env: dict[str, str] | None` 参数，向 sandbox 透传环境变量

### Changed
- `SandboxConnector` 的 HTTP `Session` 连接池提升至 `pool_connections=16, pool_maxsize=16`，避免同一 sandbox 的并发 execute 请求在 TCP 层串行阻塞

## [0.2.2] - 2026-04-17

### Added
- Dockerfile 新增 agent 用户（UID 1000），以 `/workspace` 作为 HOME，集中存放 npm global、pip --user、shell 历史等配置
- Dockerfile 预装 Lark (Feishu) CLI (`@larksuite/cli`)，与运行时的 npm 安装路径保持一致
- 新增 `NPM_CONFIG_PREFIX`、`PATH` 等环境变量，指向 `/workspace/.npm-global` 和 `/workspace/.local/bin`

### Changed
- `get_safe_path` 放宽限制，新增 `ALLOWED_DIRS = ("/workspace", "/tmp")`，允许访问 `/tmp` 下的临时文件

## [0.2.1] - 2026-04-16

### Fixed
- Dockerfile 修复 /opt/.runtime 目录权限，chown 到 1000:1000 以便非 root 用户访问
- `get_safe_path` 支持绝对路径（如 `/workspace/foo.md`）和相对路径（如 `foo.md`），避免绝对路径被错误拼接

## [0.2.0] - 2026-04-16

### Changed
- **Breaking**: 从持久 PTY shell 改为 per-command subprocess 模型
- 删除 PersistentShell 类（PTY fork、marker 协议、ANSI 清理、echo 过滤）
- 只持久化工作目录（cwd），每次 execute 启动新 bash 进程
- 项目重命名为 omniagent-sandbox

## [0.1.1] - 2026-04-16

### Added
- 新增文档处理依赖（markitdown, pdfplumber, pypdf, reportlab, pdf2image 等）
- 新增数据处理依赖（pandas, numpy, openpyxl）
- 新增对象存储支持（minio）
- 新增 LangChain MCP 适配器（langchain-mcp-adapters）
- Dockerfile 添加 pandoc、libreoffice、poppler-utils、qpdf 系统工具
- 新增 Docker 镜像构建和推送脚本 build.sh

### Changed
- 升级 Python 基础镜像从 3.11-slim 到 3.12.11
- Shell 初始化时添加 stty -echo 以抑制回显
- 代码格式化（ruff format）

## [0.1.0] - Initial Release

- 初始版本，提供持久化 shell 会话运行时
