# AI Health Checker 🩺

> 多站点健康检测系统 - 10 个 Checker 子 Agent 模拟不同 IP 自动检测 17 个网站项目

## ✨ 特性

- 🤖 **10 个 Checker Agent** - 模拟不同浏览器、设备和 IP 地址
- 🌍 **真实 IP 模拟** - X-Forwarded-For / X-Real-IP 头模拟全球不同地区 IP
- ⏱️ **随机间隔** - 30-120 秒随机检查间隔，避免被检测为机器人
- 📊 **多维度检测** - HTTP 状态码、响应时间、页面内容、SSL 证书、API 端点
- 🎨 **暗色主题面板** - 单文件 HTML，Tailwind CSS + Alpine.js
- 🐳 **Docker 一键部署**

## 🏗️ 项目结构

```
ai-health-checker/
├── Dockerfile
├── docker-compose.yml
├── backend/
│   ├── main.py          # FastAPI 主服务 (端口 8700)
│   ├── checker.py       # 10 个 Checker 子 Agent 核心引擎
│   ├── config.py        # 配置管理
│   └── requirements.txt
├── frontend/
│   └── dashboard.html   # 监控面板（单文件 HTML）
└── data/
    └── check_results.json
```

## 🔍 检测项目

| Checker ID | 身份 | 类型 | 负责项目数 |
|-----------|------|------|-----------|
| 1 | Chrome-Win10 | 桌面端 | 2 |
| 2 | Firefox-Win11 | 桌面端 | 2 |
| 3 | Safari-macOS | 桌面端 | 2 |
| 4 | Edge-Win10 | 桌面端 | 2 |
| 5 | Chrome-iPhone | 移动端 | 2 |
| 6 | Safari-iPad | 平板 | 1 |
| 7 | Chrome-Android | 移动端 | 2 |
| 8 | Firefox-Ubuntu | 桌面端 | 2 |
| 9 | Chrome-Linux | 桌面端 | 1 |
| 10 | Samsung-Android | 移动端 | 1 |

## 📡 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 监控面板 |
| GET | `/api/status` | 所有项目当前状态 |
| GET | `/api/status/{project_name}` | 单个项目详情和历史 |
| POST | `/api/check/{project_name}` | 立即检查单个项目 |
| POST | `/api/check-all` | 立即检查所有项目 |
| GET | `/api/history` | 最近 100 条检查历史 |
| GET | `/api/agents` | 10 个 Checker 运行状态 |
| GET | `/api/health` | 服务健康检查 |

## 🚀 部署

### Docker Compose（推荐）

```bash
git clone https://github.com/hpennn/ai-health-checker.git
cd ai-health-checker
docker-compose up -d
```

访问：http://localhost:8700

### 直接运行

```bash
cd backend
pip install -r requirements.txt
python main.py
```

## ⚙️ 配置说明

在 `backend/config.py` 中可配置：

- `PROJECTS` - 监控项目列表
- `CHECK_INTERVAL_MIN/MAX` - 检查间隔范围
- `REQUEST_TIMEOUT` - 请求超时时间
- `SLOW_THRESHOLD` - 慢响应阈值
- `HISTORY_MAX_SIZE` - 历史记录最大条数

## 📝 License

MIT
