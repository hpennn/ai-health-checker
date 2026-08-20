# 本地 Visitor 客户端

使用真实浏览器（Playwright + Chromium）在本地电脑上模拟真实用户访问，为项目带来真实住宅 IP 的访问流量。

## 功能特点

- 🚀 **真实浏览器**：基于 Playwright 的 Chromium，模拟真实用户行为
- 📱 **多设备模拟**：支持桌面端、移动端、平板设备的 User-Agent 和视口
- 🎲 **智能随机**：随机滚动页面、随机点击内链、随机停留时间
- ⚖️ **权重选择**：按项目 visit_count 权重随机选择访问项目
- 📊 **结果上报**：自动将访问结果上报到服务器
- 🔄 **配置同步**：定期从服务器拉取最新项目配置

## 安装步骤

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 Playwright 浏览器

```bash
playwright install chromium
```

> 如果在国内网络下载慢，可以设置镜像：
> ```bash
> set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
> playwright install chromium
> ```

## 使用方法

### 基本用法（有头模式，默认服务器）

```bash
python local_visitor.py
```

### 无头模式（后台运行，不显示浏览器窗口）

```bash
python local_visitor.py --headless
```

### 自定义服务器地址

```bash
python local_visitor.py --server http://你的服务器IP:8700
```

### 自定义访问间隔

```bash
# 每 3-8 分钟访问一次
python local_visitor.py --interval-min 3 --interval-max 8
```

### 限制访问次数

```bash
# 只访问 10 次后退出
python local_visitor.py --max-visits 10
```

### 完整参数示例

```bash
python local_visitor.py ^
    --server http://47.113.216.237:8700 ^
    --interval-min 5 ^
    --interval-max 15 ^
    --headless ^
    --max-visits 100
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--server` | 服务器 API 地址 | `http://47.113.216.237:8700` |
| `--interval-min` | 最小访问间隔（分钟） | `5` |
| `--interval-max` | 最大访问间隔（分钟） | `15` |
| `--headless` | 无头模式（不显示浏览器窗口） | 关闭 |
| `--max-visits` | 最大访问次数，0 表示无限 | `0` |

## 模拟行为说明

### User-Agent 池
- 桌面端：Chrome、Firefox、Safari、Edge（Windows/macOS）
- 移动端：iPhone Chrome、iPhone Safari、Android Chrome、Samsung Browser
- 平板：iPad Safari

每次访问随机选择一个 UA，并对应设置视口尺寸。

### 访问流程
1. 按权重随机选择一个项目
2. 访问项目首页，等待 2-5 秒
3. 随机滚动页面（1-4 次）
4. 提取页面内链，随机点击 1-3 个内页
5. 每个内页停留 3-10 秒，并随机滚动
6. 上报访问结果到服务器
7. 等待随机间隔后进行下一次访问

### 客户端标识
- 首次运行时自动生成唯一客户端 ID（保存在 `client_id.txt`）
- 用于服务器端区分不同的本地访问客户端

## Windows 后台运行

### 方法一：使用 start 命令（最小化窗口）

```batch
start /min python local_visitor.py --headless
```

### 方法二：创建开机自启动

1. 按 `Win + R`，输入 `shell:startup`
2. 在打开的文件夹中创建 `visitor.bat`：
   ```batch
   @echo off
   cd /d "C:\path\to\local_visitor"
   python local_visitor.py --headless
   ```

### 方法三：使用任务计划程序

1. 打开「任务计划程序」
2. 创建基本任务，触发器选择「计算机启动时」
3. 操作选择「启动程序」，程序填 `python`
4. 参数填 `local_visitor.py --headless`
5. 起始位置填脚本所在目录

## 查看结果

访问服务器的 Dashboard 页面，在「本地访问统计」区域可以查看：

- 总访问次数、成功次数、失败次数
- 各项目的访问分布
- 各客户端的活跃度
- 最近 24 小时访问量
- 最近访问记录

## 注意事项

1. **网络环境**：建议在家庭宽带等住宅 IP 环境下运行，效果最佳
2. **防火墙**：确保本地网络可以访问服务器的 8700 端口
3. **资源占用**：有头模式会打开真实浏览器窗口，占用较多内存；建议后台运行时使用 `--headless`
4. **访问频率**：建议间隔不要太短（至少 3 分钟以上），避免被目标网站识别
5. **客户端 ID**：不要删除 `client_id.txt`，否则会生成新的客户端标识
