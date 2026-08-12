# AI Health Checker 🩺

> 多站点健康检测系统 - 同步 Checker + 异步搜索词 Checker，支持巡检控制、时间段、动态配置、WebSocket 实时推送

## ✨ 特性

### v2.0 新增功能
- 🎛️ **巡检总开关** - 一键启停所有Checker，暂停但保持进程
- ⏰ **巡检时间段** - 可配置开始/结束时间，时间段外自动暂停
- ⏱️ **巡检间隔配置** - 5-1440分钟可调，支持随机扰动
- 🔄 **多轮检测** - 每轮内每项目可配置1-10次检查
- 🔍 **异步Checker（搜索词模式）** - 通过搜索引擎搜索关键词后访问目标网站
- 📊 **动态异步Checker管理** - 0-20个动态增减，自动分配项目
- 🌐 **多搜索引擎支持** - 百度、必应、Google 可选
- 📡 **WebSocket实时推送** - 配置变更和状态实时推送到前端
- 💾 **配置持久化** - config.json 存储，热更新无需重启

### 基础功能
- 🤖 **10 个同步 Checker Agent** - 模拟不同浏览器、设备和 IP 地址
- 🌍 **真实 IP 模拟** - X-Forwarded-For / X-Real-IP 头模拟全球不同地区 IP
- 📊 **多维度检测** - HTTP 状态码、响应时间、页面内容、SSL 证书、API 端点
- 🎨 **暗色主题面板** - 单文件 HTML，Tailwind CSS + Alpine.js
- 🐳 **Docker 一键部署**

## 🏗️ 项目结构

```
ai-health-checker/
├── Dockerfile
├── docker-compose.yml
├── backend/
│   ├── main.py              # FastAPI 主服务 (端口 8700)
│   ├── checker.py           # 10 个同步 Checker 引擎 + 管理器
│   ├── async_checker.py     # 异步搜索词 Checker 引擎
│   ├── config.py            # 配置管理（含运行时配置）
│   └── requirements.txt
├── frontend/
│   └── dashboard.html       # 监控面板（单文件 HTML）
└── data/
    ├── config.json          # 运行时配置（自动生成）
    └── check_results.json   # 检查结果历史
```

## 🔍 检测项目

17 个项目由 10 个同步 Checker 轮询分配检测，异步 Checker 可额外通过搜索引擎访问。

## 📡 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 监控面板 |
| GET | `/api/status` | 所有项目当前状态 |
| GET | `/api/status/{project_name}` | 单个项目详情和历史 |
| POST | `/api/check/{project_name}` | 立即检查单个项目 |
| POST | `/api/check-all` | 立即检查所有项目 |
| GET | `/api/history` | 最近 100 条检查历史 |
| GET | `/api/agents` | 同步 Checker 运行状态 |
| **POST** | **`/api/control`** | **启停巡检（start/stop）** |
| **GET** | **`/api/config`** | **获取当前配置** |
| **POST** | **`/api/config`** | **更新配置** |
| **GET** | **`/api/async-checkers`** | **异步Checker状态** |
| WS | `/ws` | WebSocket 实时推送 |
| GET | `/api/health` | 服务健康检查 |

### 配置字段说明（config.json）

```json
{
  "inspection_enabled": true,
  "time_range": { "start": "00:00", "end": "23:59" },
  "interval_minutes": 30,
  "rounds_per_inspection": 1,
  "rounds_interval_seconds": 3,
  "async_checker_count": 0,
  "search_engine": "baidu",
  "project_search_keywords": {
    "项目名称": ["关键词1", "关键词2", "关键词3"]
  }
}
```

| 字段 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `inspection_enabled` | 巡检总开关 | true | true/false |
| `time_range` | 巡检时间段 | 00:00-23:59 | HH:MM |
| `interval_minutes` | 巡检间隔（分钟） | 30 | 1-1440 |
| `rounds_per_inspection` | 每轮检查次数 | 1 | 1-10 |
| `rounds_interval_seconds` | 轮内间隔（秒） | 3 | 1-60 |
| `async_checker_count` | 异步Checker数量 | 0 | 0-20 |
| `search_engine` | 搜索引擎 | baidu | baidu/bing/google |
| `project_search_keywords` | 项目搜索关键词 | 预设 | 每项目1-3个 |

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
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py
```

## ⚙️ 使用说明

1. **启动服务**后，访问 Dashboard
2. 点击顶部 **绿色/红色大按钮** 一键启停巡检
3. 点击 **配置** 按钮打开配置面板：
   - **基础配置**：巡检时间段、间隔、每轮次数
   - **异步Checker/搜索词**：设置异步Checker数量、搜索引擎、各项目搜索关键词
4. 配置保存后 **实时生效**，无需重启
5. WebSocket 自动连接，状态实时同步

## 📝 License

MIT
