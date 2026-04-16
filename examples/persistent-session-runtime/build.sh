#!/bin/bash

# Docker 镜像构建和推送脚本
# 目标仓库: aidong-backend.tencentcloudcr.com/llm/persistent-session-sandbox

set -e

REGISTRY="aidong-backend.tencentcloudcr.com"
IMAGE_NAME="llm/persistent-session-sandbox"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}"

# 获取版本标签，默认使用 latest
VERSION=${1:-latest}

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================"
echo "构建 Docker 镜像"
echo "镜像: ${FULL_IMAGE}:${VERSION}"
echo "======================================"

# 切换到 Dockerfile 所在目录
cd "${SCRIPT_DIR}"

# 构建镜像
docker build -t "${FULL_IMAGE}:${VERSION}" .

# 如果版本不是 latest，同时打上 latest 标签
if [ "${VERSION}" != "latest" ]; then
    docker tag "${FULL_IMAGE}:${VERSION}" "${FULL_IMAGE}:latest"
fi

echo "======================================"
echo "推送镜像到腾讯云容器镜像仓库"
echo "======================================"

# 推送镜像
docker push "${FULL_IMAGE}:${VERSION}"

if [ "${VERSION}" != "latest" ]; then
    docker push "${FULL_IMAGE}:latest"
fi

echo "======================================"
echo "完成!"
echo "镜像已推送: ${FULL_IMAGE}:${VERSION}"
echo "======================================"
