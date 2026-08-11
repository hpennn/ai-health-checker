FROM python:3.11-slim

LABEL maintainer="hpennn"
LABEL description="AI Health Checker - 多站点健康检测系统"

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# 创建数据目录
RUN mkdir -p /app/data

# 设置工作目录
WORKDIR /app/backend

# 暴露端口
EXPOSE 8700

# 启动命令
CMD ["python", "main.py"]
