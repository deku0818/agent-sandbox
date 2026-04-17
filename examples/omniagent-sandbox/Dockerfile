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

# Copy API service to a hidden system directory
COPY pyproject.toml main.py /opt/.runtime/
RUN uv pip install --system --no-cache -r /opt/.runtime/pyproject.toml \
    && chown -R 1000:1000 /opt/.runtime \
    && chmod 500 /opt/.runtime

# Create agent user (UID 1000) and use /workspace as HOME. Since /workspace
# is not mounted from outside, keeping everything the agent installs or
# configures (npm global, pip --user, `lark login` tokens, shell history)
# inside $HOME=/workspace makes the sandbox self-contained and mirrors a
# real user's machine where dotfiles and working files coexist.
RUN useradd -u 1000 -s /bin/bash -M -d /workspace agent \
    && mkdir -p /workspace/.npm-global /workspace/.local/bin \
    && chown -R agent:agent /workspace

ENV HOME=/workspace
ENV NPM_CONFIG_PREFIX=/workspace/.npm-global
ENV PATH=/workspace/.npm-global/bin:/workspace/.local/bin:$PATH
ENV PYTHONPATH=/workspace

WORKDIR /workspace
USER 1000

# Install Lark (Feishu) CLI as the agent user so it lands in ~/.npm-global
# and exercises the same install path the agent uses at runtime.
RUN npm install -g @larksuite/cli \
    && npm cache clean --force

EXPOSE 8888

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--log-level", "info", "--app-dir", "/opt/.runtime"]
