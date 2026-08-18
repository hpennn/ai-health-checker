# 轻量级 Python 镜像 - 无需浏览器
FROM python:3.11-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/data /app/data/screenshots \
    && chown -R appuser:appuser /app
COPY backend/ ./
COPY frontend/ ./frontend/
USER appuser
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8700/api/health', timeout=5).read()" || exit 1
CMD ["python", "main.py"]
