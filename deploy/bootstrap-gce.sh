#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cs-monitor}"
DEPLOY_USER="${DEPLOY_USER:-$USER}"

# 这个脚本只做 VM 的一次性基础设施初始化：
# 安装 Docker、准备部署目录，并把当前用户加入 docker 组。
# SSH 公钥、防火墙开放 8000 端口仍建议在 Google Cloud 控制台或 gcloud 中显式配置。
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
fi

. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker "$DEPLOY_USER"
sudo mkdir -p "$APP_DIR"
sudo chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

echo "VM 初始化完成。请重新登录 SSH，让 docker 用户组权限生效。"
echo "部署目录：$APP_DIR"
