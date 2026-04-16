# Changelog

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
