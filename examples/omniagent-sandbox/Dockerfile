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

# Use the official Python image as the base image
FROM python:3.12.11

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    curl \
    ca-certificates \
    gnupg \
    ripgrep \
    pandoc \
    libreoffice \
    poppler-utils \
    qpdf \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (LTS version) for npx support
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Aliyun CLI (used by .omniagent/skills/device-logs to query SLS).
# Binary goes to /usr/local/bin so every UID can call it.
RUN curl -fsSL https://aliyuncli.alicdn.com/aliyun-cli-linux-latest-amd64.tgz \
        -o /tmp/aliyun-cli.tgz \
    && tar -xzf /tmp/aliyun-cli.tgz -C /usr/local/bin/ \
    && rm /tmp/aliyun-cli.tgz \
    && aliyun version

# Copy API service to a hidden system directory
COPY pyproject.toml main.py /opt/.runtime/
RUN uv pip install --system --no-cache -r /opt/.runtime/pyproject.toml \
    && chown -R 1000:1000 /opt/.runtime \
    && chmod 500 /opt/.runtime

# Create `user` (UID 1000) — 跨 backend 统一身份（与 E2B envd 默认 user 对齐）。
# HOME=/home/user 是标准 Linux 习惯；/workspace 只是工作目录，不再兼任 HOME。
# Server runs as root (no USER directive) so the runtime can switch identity
# per-request via setuid:
# - /execute spawns subprocess with user=AGENT_UID
# - /upload chown's the written file to AGENT_UID
# Mirrors E2B envd—agent identity decoupled from server identity, change
# AGENT_UID via Pod env without rebuilding image.
RUN useradd -u 1000 -m -d /home/user -s /bin/bash user \
    && mkdir -p /workspace \
    && chown -R user:user /workspace /home/user

# Pre-install the Aliyun CLI SLS plugin into user's home so subprocess
# spawned as AGENT_UID (1000) can use `aliyun sls get-logs-v2` etc. without
# needing internet at request time. Plugin lives at ~/.aliyun/plugins/.
USER user
RUN aliyun plugin install --names aliyun-cli-sls
USER root

# Operator identity that runtime hands to /execute subprocess and /upload
# chown. 命名沿用 AGENT_UID/GID（OmniAgent 层"agent 操作身份"概念），Linux
# 真实 user 名是 `user`——二者解耦，改 UID 无需 rebuild。
ENV AGENT_UID=1000
ENV AGENT_GID=1000

WORKDIR /workspace

# Lark (Feishu) CLI 装到默认全局 prefix /usr，runtime PATH 自带 /usr/bin 兜
# 住——不再需要 NPM_CONFIG_PREFIX hack 把它塞 HOME。
RUN npm install -g @larksuite/cli \
    && npm cache clean --force

EXPOSE 8888

# Server runs as root—needed for setuid/chown per request. Subprocess + file
# ownership get downgraded to AGENT_UID at request time (see main.py).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--log-level", "info", "--app-dir", "/opt/.runtime"]
