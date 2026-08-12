# 多阶段构建 - 减小镜像体积
FROM python:3.11-slim AS builder

WORKDIR /app

# 安装编译依赖（仅在构建阶段）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖并安装
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# ===== 运行阶段 =====
FROM python:3.11-slim

WORKDIR /app

# 仅复制必要的运行时依赖
COPY --from=builder /install /usr/local

# 创建非root用户（安全最佳实践）
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

# 复制应用代码
COPY backend/ ./
COPY frontend/ ./frontend/

# 切换到非root用户
USER appuser

EXPOSE 8700

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8700/api/health', timeout=5).read()" || exit 1

CMD ["python", "main.py"]
