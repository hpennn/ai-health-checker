# AI Health Checker 节点 Agent

运行在本地电脑上的工作节点，连接服务器面板接收检测任务。

## 功能

- 🔌 **自动注册**：首次启动生成唯一节点 ID，自动注册到服务器
- 💓 **心跳保活**：每 30 秒上报状态，服务器实时显示节点在线情况
- 🔍 **三种检测类型**：
  - **sync**：HTTP 深度检测（SSL/资源/SEO，无需浏览器）
  - **async**：搜索引擎关键词检测（百度/Bing/Google，无需浏览器）
  - **browser**：Playwright 真实浏览器访问（需 Chromium）
- 📦 **远程安装**：面板可下发指令安装 pip 包或 Playwright 浏览器
- 🪟 **Windows 兼容**：UTF-8 编码修复，Windows 10 + Python 3.14 可运行
- 🔄 **断线重连**：网络中断后自动重连，不影响其他任务

## 安装步骤

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 Playwright 浏览器（仅 browser checker 需要）

```bash
playwright install chromium
```

> 国内网络可设置镜像：
> ```bash
> set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
> playwright install chromium
> ```

> 如果不安装浏览器，sync 和 async 类型的 checker 仍可正常运行，browser 类型会自动跳过。

### 3. 运行节点 Agent

```bash
python node_agent.py --server http://你的服务器IP:8700
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--server` | 服务器面板地址 | `http://47.113.216.237:8700` |
| `--name` | 节点名称（显示在面板上） | 自动生成 |

## 使用示例

```bash
# 基本用法
python node_agent.py

# 指定服务器和节点名称
python node_agent.py --server http://47.113.216.237:8700 --name 家里电脑

# 无头模式运行（browser checker 在后台运行）
# browser checker 的 headless 配置由面板上的 checker 配置控制
```

## Windows 后台运行

### 方法一：最小化窗口

```batch
start /min python node_agent.py --server http://47.113.216.237:8700
```

### 方法二：开机自启动

1. 按 `Win + R`，输入 `shell:startup`
2. 在打开的文件夹中创建 `node_agent.bat`：
   ```batch
   @echo off
   cd /d "C:\path\to\node_agent"
   python node_agent.py --server http://47.113.216.237:8700
   ```

### 方法三：任务计划程序

设置开机自动运行，无需登录。

## 节点 ID

首次运行时自动生成唯一节点 ID，保存在同目录的 `node_id.txt` 文件中。
**请勿删除此文件**，否则服务器会将其视为新节点。

## 环境要求

- Python 3.10+
- 操作系统：Windows / macOS / Linux
- 浏览器检测（可选）：Playwright + Chromium
