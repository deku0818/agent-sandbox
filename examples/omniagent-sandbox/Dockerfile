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

# Set Python path environment variable
ENV PYTHONPATH=/workspace

# Create user workspace
RUN mkdir -p /workspace && chown 1000:1000 /workspace

WORKDIR /workspace
USER 1000
EXPOSE 8888

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--log-level", "info", "--app-dir", "/opt/.runtime"]
