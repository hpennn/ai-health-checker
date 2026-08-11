#!/bin/bash
# 使用镜像站拉取代码并部署 ai-health-checker
cd /www/wwwroot
if [ ! -d "ai-health-checker" ]; then
    git clone https://gitclone.com/github.com/hpennn/ai-health-checker.git
fi
cd ai-health-checker
git -c http.proxy= pull origin main
docker compose down
docker compose build
docker compose up -d
echo "✅ ai-health-checker 已启动，访问 http://服务器IP:8700"
