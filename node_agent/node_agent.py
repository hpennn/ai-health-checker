"""本地节点 Agent - 运行在用户本地电脑上，连接服务器面板接收 checker 任务
支持三种 checker 类型：
  - sync:   HTTP 深度检测（httpx + DeepInspector 逻辑，无需浏览器）
  - async:  搜索引擎关键词检测（httpx，无需浏览器）
  - browser: Playwright 真实浏览器访问（需要 Chromium）

Windows 兼容：UTF-8 编码、控制台编码修复、路径处理
Python 3.14 兼容：playwright 不可用时优雅降级
"""
import argparse
import asyncio
import json
import os
import platform
import random
import re
import ssl
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

# ========== Windows 编码修复 ==========
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ========== 常量 ==========
HEARTBEAT_INTERVAL = 30  # 秒
REQUEST_TIMEOUT = 15
SLOW_THRESHOLD = 5
VERSION = "1.0.0"

# User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.118 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.119 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ========== 环境信息收集 ==========
def get_node_id_file() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_id.txt")


def load_or_create_node_id() -> str:
    fpath = get_node_id_file()
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                nid = f.read().strip()
                if nid:
                    return nid
        except Exception:
            pass
    nid = f"local-{uuid.uuid4().hex[:8]}"
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(nid)
    except Exception:
        pass
    return nid


def collect_env_info() -> dict:
    """收集本地环境信息"""
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "python_version": platform.python_version(),
        "platform": platform.machine(),
        "capabilities": {
            "has_browser": False,
            "playwright_version": None,
            "chromium_installed": False,
        },
        "installed_packages": [],
    }

    # 已安装 pip 包
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            packages = json.loads(result.stdout)
            info["installed_packages"] = [
                f"{p['name']}=={p['version']}" for p in packages
            ]
            # 检查 playwright
            for p in packages:
                if p["name"].lower() == "playwright":
                    info["capabilities"]["playwright_version"] = p["version"]
                    info["capabilities"]["has_browser"] = True
    except Exception as e:
        log(f"[Env] 获取 pip 包列表失败: {e}")

    # 检查 Chromium 是否安装（多策略检测，不依赖 playwright 内部 API）
    if info["capabilities"]["playwright_version"]:
        chromium_installed = False

        # 方法1: 检查 Playwright 浏览器目录（按平台标准路径）
        try:
            candidate_dirs = []
            if sys.platform == "win32":
                base = os.environ.get("LOCALAPPDATA", "")
                if base:
                    candidate_dirs.append(os.path.join(base, "ms-playwright"))
            elif sys.platform == "darwin":
                candidate_dirs.append(os.path.expanduser("~/Library/Caches/ms-playwright"))
            else:
                candidate_dirs.append(os.path.expanduser("~/.cache/ms-playwright"))
            # 也检查 PLAYWRIGHT_BROWSERS_PATH 环境变量
            env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
            if env_path:
                candidate_dirs.append(env_path)

            for browsers_dir in candidate_dirs:
                if browsers_dir and os.path.isdir(browsers_dir):
                    for name in os.listdir(browsers_dir):
                        if name.lower().startswith("chromium"):
                            chromium_installed = True
                            log(f"[Env] 在 {browsers_dir} 找到 {name}")
                            break
                if chromium_installed:
                    break
        except Exception as e:
            log(f"[Env] Chromium 目录检测失败: {e}")

        # 方法2: 用 playwright install --dry-run 兜底
        if not chromium_installed:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
                    capture_output=True, text=True, timeout=15,
                    encoding="utf-8", errors="replace",
                )
                output = ((result.stdout or "") + (result.stderr or "")).lower()
                if "already installed" in output or "is installed" in output:
                    chromium_installed = True
                    log("[Env] dry-run 检测到 Chromium 已安装")
            except Exception as e:
                log(f"[Env] Chromium dry-run 检测失败: {e}")

        info["capabilities"]["chromium_installed"] = chromium_installed

    return info


# ========== HTTP 检测引擎（sync） ==========
async def run_sync_check(project: dict, config: dict) -> dict:
    """执行 sync 类型深度 HTTP 检测（复用 DeepInspector 逻辑）"""
    result = {
        "project_name": project["name"],
        "project_url": project["url"],
        "name": project["name"],
        "url": project["url"],
        "category": project.get("category", ""),
        "checker_type": "sync",
        "status": "unknown",
        "status_code": None,
        "response_time_ms": None,
        "content_length": 0,
        "title": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "deep_inspect": None,
        "error": None,
    }
    start = time.time()
    ua = config.get("user_agent") or random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=REQUEST_TIMEOUT,
            follow_redirects=True, verify=True,
        ) as client:
            resp = await client.get(project["url"])
            ms = round((time.time() - start) * 1000, 2)
            result["status_code"] = resp.status_code
            result["response_time_ms"] = ms
            result["content_length"] = len(resp.text)

            if config.get("deep_inspect", True):
                html = resp.text
                title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
                result["title"] = title_m.group(1).strip()[:100] if title_m else ""
                result["deep_inspect"] = {
                    "has_title": bool(result["title"]),
                    "has_meta_description": bool(re.search(r'<meta[^>]+name=["\']description["\']', html, re.I)),
                    "has_meta_viewport": bool(re.search(r'<meta[^>]+name=["\']viewport["\']', html, re.I)),
                    "has_og_tags": bool(re.search(r'<meta[^>]+property=["\']og:', html, re.I)),
                    "ssl_check": await _check_ssl(project["url"]),
                }

            if resp.status_code == 200:
                result["status"] = "slow" if ms > SLOW_THRESHOLD * 1000 else "online"
            else:
                result["status"] = "offline"
    except httpx.TimeoutException:
        result["status"] = "offline"
        result["error"] = "请求超时"
        result["response_time_ms"] = round((time.time() - start) * 1000, 2)
    except Exception as e:
        result["status"] = "offline"
        result["error"] = str(e)[:120]
        result["response_time_ms"] = round((time.time() - start) * 1000, 2)

    return result


async def _check_ssl(url: str) -> dict:
    """检查 SSL 证书"""
    if not url.startswith("https://"):
        return {"valid": None, "expiry": None, "error": "非 HTTPS"}
    try:
        hostname = url.replace("https://", "").split("/")[0].split(":")[0]
        ctx = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, 443, ssl=ctx, server_hostname=hostname),
            timeout=5,
        )
        cert = writer.get_extra_info("ssl_object").getpeercert()
        writer.close()
        await writer.wait_closed()
        not_after = cert.get("notAfter", "")
        expiry = None
        if not_after:
            try:
                expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        return {"valid": True, "expiry": expiry, "error": None}
    except Exception as e:
        return {"valid": False, "expiry": None, "error": str(e)[:100]}


# ========== 搜索引擎检测引擎（async） ==========
def _build_search_url(engine: str, keyword: str) -> str:
    if engine == "baidu":
        return f"https://www.baidu.com/s?wd={quote_plus(keyword)}"
    elif engine == "bing":
        return f"https://www.bing.com/search?q={quote_plus(keyword)}"
    else:
        return f"https://www.google.com/search?q={quote_plus(keyword)}"


def _extract_links(html: str, engine: str) -> list[dict]:
    results = []
    if engine == "baidu":
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]
            if title and 'baidu.com' not in url:
                results.append({"url": url, "title": title})
    elif engine == "bing":
        for m in re.finditer(r'<h2><a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a></h2>', html, re.I | re.S):
            results.append({"url": m.group(1), "title": re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]})
    else:
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]
            if title and 'google' not in url and 'gstatic' not in url:
                results.append({"url": url, "title": title})
    return results[:20]


async def run_async_check(project: dict, config: dict, search_engine: str = "baidu",
                           keywords: list | None = None) -> dict:
    """执行 async 类型搜索引擎检测"""
    result = {
        "project_name": project["name"],
        "project_url": project["url"],
        "name": project["name"],
        "url": project["url"],
        "category": project.get("category", ""),
        "checker_type": "async",
        "search_engine": search_engine,
        "status": "unknown",
        "keyword": "",
        "matched_target": False,
        "search_status_code": None,
        "visit_status_code": None,
        "response_time_ms": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    start = time.time()
    kw_list = keywords or [project["name"]]
    keyword = random.choice(kw_list)
    result["keyword"] = keyword
    search_url = _build_search_url(search_engine, keyword)
    target_domain = urlparse(project["url"]).netloc
    ua = random.choice(USER_AGENTS)
    headers = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"}

    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True,
        ) as client:
            resp = await client.get(search_url)
            result["search_status_code"] = resp.status_code
            if resp.status_code != 200:
                result["status"] = "offline"
                result["error"] = f"搜索引擎返回 {resp.status_code}"
                return result

            links = _extract_links(resp.text, search_engine)
            click_url = None
            for link in links:
                if target_domain in link["url"]:
                    click_url = link["url"]
                    result["matched_target"] = True
                    break
            if not click_url and links:
                click_url = links[0]["url"]

            if click_url:
                visit_start = time.time()
                try:
                    vresp = await client.get(click_url, headers={**headers, "Referer": search_url})
                    result["visit_status_code"] = vresp.status_code
                    result["response_time_ms"] = round((time.time() - visit_start) * 1000, 2)
                    result["status"] = "online" if vresp.status_code == 200 else "offline"
                except Exception as ve:
                    result["status"] = "offline"
                    result["error"] = f"访问失败: {str(ve)[:80]}"
            else:
                homepage = project["url"].rstrip("/") + "/"
                vresp = await client.get(homepage, headers=headers)
                result["visit_status_code"] = vresp.status_code
                result["response_time_ms"] = round((time.time() - start) * 1000, 2)
                result["status"] = "online" if vresp.status_code == 200 else "offline"

            if result["status"] == "online" and result["response_time_ms"] and \
               result["response_time_ms"] > SLOW_THRESHOLD * 1000:
                result["status"] = "slow"
    except Exception as e:
        result["status"] = "offline"
        result["error"] = f"搜索检测异常: {str(e)[:80]}"

    return result


# ========== 浏览器检测引擎（browser） ==========
async def _play_videos(page, play_count: int, dur_min: int, dur_max: int) -> dict:
    """在页面中查找并播放视频元素。优先 <video>，其次常见播放按钮。"""
    info = {"played": False, "count": 0, "total_seconds": 0}
    try:
        video_info = await page.evaluate("""() => {
            const videos = Array.from(document.querySelectorAll('video'));
            return videos.map((v, i) => ({
                index: i,
                visible: v.offsetParent !== null,
                duration: v.duration || 0,
            }));
        }""")
    except Exception:
        video_info = []

    candidates = [v for v in video_info if v.get("visible")]
    if candidates:
        random.shuffle(candidates)
        for vinfo in candidates[:play_count]:
            try:
                idx = vinfo["index"]
                await page.evaluate(f"""() => {{
                    const v = document.querySelectorAll('video')[{idx}];
                    if (v) v.scrollIntoView({{behavior:'smooth', block:'center'}});
                }}""")
                await asyncio.sleep(1)
                played = await page.evaluate(f"""() => {{
                    const v = document.querySelectorAll('video')[{idx}];
                    if (!v) return false;
                    v.muted = true;
                    const p = v.play();
                    if (p && p.catch) p.catch(()=>{{}});
                    return true;
                }}""")
                if played:
                    wait_sec = random.randint(dur_min, dur_max)
                    if vinfo.get("duration") and vinfo["duration"] > 0:
                        wait_sec = min(wait_sec, int(vinfo["duration"]) + 2)
                    await asyncio.sleep(wait_sec)
                    await page.evaluate(f"""() => {{
                        const v = document.querySelectorAll('video')[{idx}];
                        if (v) {{ v.pause(); v.muted = false; }}
                    }}""")
                    info["played"] = True
                    info["count"] += 1
                    info["total_seconds"] += wait_sec
            except Exception as e:
                log(f"  [Video] 播放视频元素失败: {e}")
                continue

    if info["count"] == 0:
        play_selectors = [
            "button[aria-label*='播放']",
            "button[class*='play-btn']",
            "div[class*='play-btn']",
            ".bilibili-player-video-btn-start",
            ".ytp-play-button",
        ]
        for sel in play_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    await btn.click()
                    wait_sec = random.randint(dur_min, dur_max)
                    await asyncio.sleep(wait_sec)
                    info["played"] = True
                    info["count"] += 1
                    info["total_seconds"] += wait_sec
                    break
            except Exception:
                continue

    return info


async def run_browser_check(project: dict, config: dict, browser_ctx: dict | None = None) -> dict:
    """使用 Playwright 真实浏览器访问。若传入 browser_ctx 则复用已有浏览器实例。"""
    result = {
        "project_name": project["name"],
        "project_url": project["url"],
        "name": project["name"],
        "url": project["url"],
        "category": project.get("category", ""),
        "checker_type": "browser",
        "success": False,
        "pages_visited": 0,
        "duration_seconds": 0,
        "user_agent": "",
        "device_type": "desktop",
        "status": "offline",
        "status_code": None,
        "response_time_ms": None,
        "title": "",
        "content_length": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "screenshot": None,
        "inner_pages": [],
        "video_played": False,
        "video_count": 0,
        "video_play_seconds": 0,
    }
    start = time.time()

    headless = config.get("headless", True)
    visit_count = config.get("visit_count", 3)
    visit_inner = config.get("visit_inner_pages", True)
    # 视频播放配置
    video_enabled = config.get("video_enabled", False)
    video_play_count = max(1, min(int(config.get("video_play_count", 1)), 10))
    video_duration_min = max(5, int(config.get("video_duration_min", 15)))
    video_duration_max = max(video_duration_min, int(config.get("video_duration_max", 45)))
    ua_info = random.choice([
        {"ua": USER_AGENTS[0], "type": "desktop", "viewport": {"width": 1366, "height": 768}},
        {"ua": USER_AGENTS[1], "type": "desktop", "viewport": {"width": 1440, "height": 900}},
        {"ua": USER_AGENTS[4], "type": "mobile", "viewport": {"width": 390, "height": 844, "isMobile": True, "hasTouch": True, "deviceScaleFactor": 3}},
        {"ua": USER_AGENTS[6], "type": "tablet", "viewport": {"width": 768, "height": 1024, "isMobile": True, "hasTouch": True, "deviceScaleFactor": 2}},
    ])
    result["user_agent"] = ua_info["ua"]
    result["device_type"] = ua_info["type"]

    owns_browser = browser_ctx is None
    pw = None
    browser = None
    try:
        if browser_ctx:
            browser = browser_ctx["browser"]
        else:
            # 单项目独立运行时才自己启动浏览器
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                result["error"] = "Playwright 未安装，无法执行浏览器检测"
                result["duration_seconds"] = round(time.time() - start, 2)
                return result
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )

        context = await browser.new_context(
            user_agent=ua_info["ua"],
            viewport=ua_info["viewport"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        log(f"  → 浏览器访问: {project['name']} ({project['url']})")
        resp = await page.goto(project["url"], wait_until="domcontentloaded", timeout=30000)
        result["response_time_ms"] = round((time.time() - start) * 1000, 2)
        if resp:
            result["status_code"] = resp.status
        await asyncio.sleep(random.uniform(2, 4))

        try:
            result["title"] = await page.title()
            result["content_length"] = len(await page.content())
        except Exception:
            pass

        result["pages_visited"] = 1
        result["success"] = True
        result["status"] = "online"

        # 随机滚动
        try:
            for _ in range(random.randint(1, 3)):
                await asyncio.sleep(random.uniform(0.5, 1.5))
                scroll_h = await page.evaluate("() => document.body.scrollHeight")
                target_y = random.randint(0, max(100, scroll_h - 500))
                await page.evaluate(f"window.scrollTo({{top:{target_y},behavior:'smooth'}})")
        except Exception:
            pass

        # 视频播放
        if video_enabled:
            try:
                vp = await _play_videos(
                    page, video_play_count, video_duration_min, video_duration_max,
                )
                result["video_played"] = vp["played"]
                result["video_count"] = vp["count"]
                result["video_play_seconds"] = vp["total_seconds"]
                if vp["played"]:
                    log(f"  ▶ 播放了 {vp['count']} 个视频，共 {vp['total_seconds']} 秒")
            except Exception as ve:
                log(f"  [Video] 播放异常: {ve}")

        # 访问内页
        if visit_inner:
            try:
                internal_links = await page.evaluate("""(baseUrl) => {
                    const base = new URL(baseUrl);
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    const internal = new Set();
                    for (const a of links) {
                        try {
                            const u = new URL(a.href, baseUrl);
                            if (u.hostname === base.hostname && u.protocol.startsWith('http')
                                && !u.hash && !u.href.startsWith('mailto:') && !u.href.startsWith('javascript:')) {
                                internal.add(u.href);
                            }
                        } catch(e) {}
                    }
                    return Array.from(internal);
                }""", project["url"])

                num_inner = min(random.randint(1, min(visit_count, 3)), len(internal_links))
                for link_url in random.sample(internal_links, min(num_inner, len(internal_links))):
                    try:
                        await page.goto(link_url, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(random.uniform(2, 5))
                        result["pages_visited"] += 1
                        result["inner_pages"].append(link_url[:120])
                    except Exception:
                        continue
            except Exception:
                pass

        await context.close()
    except Exception as e:
        result["success"] = False
        result["status"] = "offline"
        result["error"] = str(e)[:200]
        log(f"  ✗ 浏览器访问失败: {e}")
    finally:
        # 只有自己启动的浏览器才关闭
        if owns_browser:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass

    result["duration_seconds"] = round(time.time() - start, 2)
    return result


# ========== 安装指令执行 ==========
async def execute_install_command(cmd: dict) -> dict:
    """执行安装指令，返回结果"""
    command = cmd.get("command", "")
    pkg = cmd.get("package", "")
    cmd_id = cmd.get("id", "")
    result = {
        "command_id": cmd_id,
        "command": command,
        "success": False,
        "output": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if command == "install_package" and pkg:
            log(f"[Install] 安装 pip 包: {pkg}")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            result["success"] = proc.returncode == 0
            result["output"] = output[-500:]
            log(f"[Install] pip install {pkg} {'成功' if result['success'] else '失败'}")
        elif command == "install_browser":
            log("[Install] 安装 Playwright Chromium 浏览器...")
            env = os.environ.copy()
            # 国内镜像
            if sys.platform == "win32":
                env.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", "https://npmmirror.com/mirrors/playwright")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            result["success"] = proc.returncode == 0
            result["output"] = output[-500:]
            log(f"[Install] playwright install chromium {'成功' if result['success'] else '失败'}")
        else:
            result["error"] = f"未知指令: {command}"
    except asyncio.TimeoutError:
        result["error"] = "安装超时"
        log(f"[Install] 安装超时: {command}")
    except Exception as e:
        result["error"] = str(e)[:200]
        log(f"[Install] 安装异常: {e}")
    return result


# ========== 节点 Agent 主类 ==========
class NodeAgent:
    def __init__(self, server: str, name: str = ""):
        self.server = server.rstrip("/")
        self.node_id = load_or_create_node_id()
        self.name = name or f"节点-{self.node_id[:8]}"
        self.env_info = collect_env_info()
        self.tasks: list[dict] = []
        self.projects: list[dict] = []
        self.running = True
        self._last_heartbeat = 0
        self._install_results: list[dict] = []
        self._checker_states: dict[str, dict] = {}  # checker_id -> last_run_time

        log(f"=" * 50)
        log(f"AI Health Checker 节点 Agent v{VERSION}")
        log(f"节点 ID: {self.node_id}")
        log(f"节点名称: {self.name}")
        log(f"服务器: {self.server}")
        log(f"操作系统: {self.env_info['os']}")
        log(f"Python: {self.env_info['python_version']}")
        log(f"浏览器支持: {'是' if self.env_info['capabilities']['has_browser'] else '否'}")
        log(f"Chromium: {'已安装' if self.env_info['capabilities']['chromium_installed'] else '未安装'}")
        log(f"=" * 50)

    async def _api_post(self, path: str, data: dict) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self.server}{path}", json=data)
                if resp.status_code == 404:
                    return {"_status_code": 404}
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log(f"[API] POST {path} 失败: {e}")
            return None

    async def _api_get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{self.server}{path}", params=params)
                if resp.status_code == 404:
                    return {"_status_code": 404}
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log(f"[API] GET {path} 失败: {e}")
            return None

    async def register(self) -> bool:
        """注册到服务器"""
        payload = {
            "node_id": self.node_id,
            "name": self.name,
            "ip": "",
            "os": self.env_info["os"],
            "python_version": self.env_info["python_version"],
            "capabilities": self.env_info["capabilities"],
            "installed_packages": self.env_info["installed_packages"],
        }
        result = await self._api_post("/api/node/register", payload)
        if result and "node" in result:
            self.tasks = result.get("tasks", [])
            self.projects = result.get("projects", [])
            log(f"[Register] 注册成功，获取到 {len(self.tasks)} 个 checker 任务")
            return True
        log("[Register] 注册失败")
        return False

    async def heartbeat(self, _depth: int = 0) -> bool:
        """发送心跳并获取更新；服务器返回404时自动重新注册"""
        if _depth > 3:
            return False
        # 如果有待上报的安装结果，安装后环境可能变化，重新收集一次
        if self._install_results:
            log("[Heartbeat] 检测到安装结果，重新收集环境信息...")
            self.env_info = collect_env_info()

        payload = {
            "node_id": self.node_id,
            "status": "online",
            "capabilities": self.env_info["capabilities"],
            "installed_packages": self.env_info.get("installed_packages", []),
            "install_results": self._install_results if self._install_results else None,
        }
        result = await self._api_post("/api/node/heartbeat", payload)
        self._install_results = []

        if result and result.get("_status_code") == 404:
            log("[Heartbeat] 服务器未找到本节点，重新注册...")
            if await self.register():
                return await self.heartbeat(_depth + 1)
            return False

        if result and "node" in result:
            self.tasks = result.get("tasks", [])
            self.projects = result.get("projects", [])
            install_cmds = result.get("install_commands", [])
            if install_cmds:
                log(f"[Heartbeat] 收到 {len(install_cmds)} 个安装指令")
                for cmd in install_cmds:
                    ir = await execute_install_command(cmd)
                    self._install_results.append(ir)
                self.env_info = collect_env_info()
                return await self.heartbeat(_depth + 1)
            self._last_heartbeat = time.time()
            return True
        return False

    async def report_results(self, checker_id: str, results: list[dict]):
        """回报检测结果"""
        if not results:
            return
        payload = {
            "node_id": self.node_id,
            "checker_id": checker_id,
            "results": results,
        }
        result = await self._api_post("/api/node/result", payload)
        if result:
            log(f"[Report] checker {checker_id} 回报 {len(results)} 条结果")

    def _get_project(self, project_name: str) -> dict | None:
        for p in self.projects:
            if p["name"] == project_name:
                return p
        return None

    async def execute_task(self, task: dict):
        """执行单个 checker 任务"""
        checker_id = task["id"]
        checker_type = task.get("type", "sync")

        # 手动暂停的任务跳过执行
        if task.get("manually_paused"):
            return

        cfg = task.get("config", {})
        assigned = task.get("assigned_projects", [])
        if not assigned:
            assigned = list(self.projects)

        now = time.time()
        state = self._checker_states.setdefault(checker_id, {"last_run": 0})
        interval_min = task.get("interval_min", 5) * 60
        interval_max = task.get("interval_max", 15) * 60
        # 随机化下次执行时间，避免所有 checker 同时集中执行
        next_interval = random.uniform(interval_min, max(interval_min, interval_max))

        if state["last_run"] > 0:
            elapsed = now - state["last_run"]
            if elapsed < next_interval:
                return

        # browser 类型检查环境
        if checker_type == "browser" and not self.env_info["capabilities"].get("chromium_installed"):
            log(f"[Task] {checker_id}: Chromium 未安装，跳过浏览器检测")
            return

        log(f"[Task] 执行 {checker_type} checker: {task.get('name', checker_id)} ({len(assigned)} 个项目)")
        results = []

        # browser 类型：复用一个浏览器实例处理所有项目，避免重复启动
        browser_ctx = None
        if checker_type == "browser":
            try:
                from playwright.async_api import async_playwright
                headless = cfg.get("headless", True)
                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                browser_ctx = {"pw": pw, "browser": browser, "cfg": cfg}
                log(f"  [Browser] Chromium 已启动（headless={headless}）")
            except ImportError:
                log(f"  [Browser] Playwright 未安装，跳过 {len(assigned)} 个项目")
                state["last_run"] = time.time()
                return
            except Exception as e:
                log(f"  [Browser] 启动失败: {e}")
                state["last_run"] = time.time()
                return

        try:
            for project in assigned:
                try:
                    if checker_type == "sync":
                        r = await run_sync_check(project, cfg)
                    elif checker_type == "async":
                        engine = cfg.get("search_engine", "baidu")
                        kw = cfg.get("keywords", {}).get(project["name"], [project["name"]])
                        r = await run_async_check(project, cfg, engine, kw)
                    elif checker_type == "browser":
                        r = await run_browser_check(project, cfg, browser_ctx=browser_ctx)
                    else:
                        continue
                    r["checker_id"] = checker_id
                    r["checker_name"] = task.get("name", "")
                    r["node_id"] = self.node_id
                    results.append(r)
                    status_icon = "✓" if r.get("status") == "online" else ("⚠" if r.get("status") == "slow" else "✗")
                    log(f"  {status_icon} {project['name']}: {r.get('status', '?')}")
                except Exception as e:
                    log(f"  ✗ {project['name']}: 异常 {e}")
                    results.append({
                        "checker_id": checker_id,
                        "node_id": self.node_id,
                        "project_name": project["name"],
                        "project_url": project.get("url", ""),
                        "name": project["name"],
                        "url": project.get("url", ""),
                        "checker_type": checker_type,
                        "status": "offline",
                        "error": str(e)[:120],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                await asyncio.sleep(random.uniform(1, 3))
        finally:
            if browser_ctx:
                try:
                    await browser_ctx["browser"].close()
                    await browser_ctx["pw"].stop()
                    log("  [Browser] Chromium 已关闭")
                except Exception:
                    pass

        if results:
            await self.report_results(checker_id, results)
        state["last_run"] = time.time()

    async def run(self):
        """主运行循环"""
        # 注册
        registered = False
        while not registered and self.running:
            registered = await self.register()
            if not registered:
                log("[Run] 注册失败，10秒后重试...")
                await asyncio.sleep(10)

        # 首次心跳
        await self.heartbeat()

        # 主循环
        last_hb = 0
        while self.running:
            try:
                now = time.time()
                # 心跳
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    await self.heartbeat()
                    last_hb = now

                # 执行任务
                for task in self.tasks:
                    if not self.running:
                        break
                    if task.get("enabled", True):
                        await self.execute_task(task)

                # 短暂等待
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[Run] 主循环异常: {e}")
                await asyncio.sleep(10)

        log("[Run] 节点 Agent 已停止")


def main():
    parser = argparse.ArgumentParser(description="AI Health Checker 本地节点 Agent")
    parser.add_argument("--server", default="http://47.113.216.237:8700",
                        help="服务器面板地址（默认 http://47.113.216.237:8700）")
    parser.add_argument("--name", default="", help="节点名称（可选，默认自动生成）")
    args = parser.parse_args()

    agent = NodeAgent(server=args.server, name=args.name)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log("\n用户中断，正在退出...")
        agent.running = False


if __name__ == "__main__":
    main()
