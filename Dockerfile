# 多阶段构建 - 支持 Playwright 浏览器巡检
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

# 安装 Playwright 所需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium 系统依赖
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxtst6 \
    libxi6 \
    libxau6 \
    libxdmcp6 \
    libxcb1 \
    fonts-liberation \
    fonts-noto-cjk \
    # 其他运行时依赖
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 仅复制必要的运行时依赖
COPY --from=builder /install /usr/local

# 安装 Playwright Chromium 浏览器
RUN playwright install chromium

# 创建非root用户（安全最佳实践）
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/data \
    && mkdir -p /app/data/screenshots \
    && chown -R appuser:appuser /app

# 复制应用代码
COPY backend/ ./
COPY frontend/ ./frontend/

# 切换到非root用户
USER appuser

EXPOSE 8700

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8700/api/health', timeout=5).read()" || exit 1

CMD ["python", "main.py"]
