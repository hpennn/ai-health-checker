"""Browser Checker - 基于 Playwright 的深度浏览器巡检模块

模拟真实用户行为：访问首页 → 等待渲染 → 检查 JS 错误 → 检查 Service Worker → 点击交互 → 截图
"""
import asyncio
import logging
import os
import base64
import random
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("health_checker")

# Playwright 延迟导入，避免未安装时报错
_playwright_available = None


def _check_playwright():
    """检查 Playwright 是否可用"""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        import playwright
        _playwright_available = True
    except ImportError:
        _playwright_available = False
        logger.warning("[BrowserChecker] Playwright 未安装，浏览器巡检功能不可用")
    return _playwright_available


class BrowserChecker:
    """基于 Playwright 的浏览器巡检器
    
    复用浏览器实例，节省内存。支持并发控制（最多同时 N 个页面）。
    """

    def __init__(self, screenshot_dir: str = None, max_concurrent_pages: int = 1):
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self._max_pages = max_concurrent_pages
        self._semaphore = asyncio.Semaphore(max_concurrent_pages)
        self._screenshot_dir = screenshot_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "screenshots"
        )
        self._reconnect_lock = asyncio.Lock()
        self._consecutive_failures = 0
        # 独立浏览器巡检循环控制
        self._inspection_running = False
        self._inspection_stop_event = asyncio.Event()
        self._inspection_pause_event = asyncio.Event()
        self._inspection_pause_event.set()  # set=运行，clear=暂停
        self._inspection_task = None
        os.makedirs(self._screenshot_dir, exist_ok=True)

    async def initialize(self):
        """初始化浏览器实例（全局只需一次）"""
        if self._initialized:
            return
        if not _check_playwright():
            return

        async with self._lock:
            if self._initialized:
                return
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",  # Docker 中避免 /dev/shm 空间不足
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--no-first-run",
                        "--disable-translate",
                        "--single-process",
                        "--disable-software-rasterizer",
                        "--disable-features=site-per-process",
                        "--js-flags=--max-old-space-size=256",
                    ],
                )
                self._initialized = True
                logger.info("[BrowserChecker] Chromium 浏览器已启动（headless 模式）")
            except Exception as e:
                logger.error(f"[BrowserChecker] 浏览器启动失败: {e}")
                self._initialized = False

    async def shutdown(self):
        """关闭浏览器实例"""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._initialized = False
            logger.info("[BrowserChecker] 浏览器已关闭")

    async def run_browser_inspection_loop(
        self,
        projects: list[dict],
        result_callback=None,
        per_project_sleep_range: tuple = (5, 10),
        full_round_sleep: int = 60,
        checker_identities: list[dict] | None = None,
    ):
        """独立的浏览器巡检循环 - 按需启动-检查-销毁模式

        遍历所有项目，每个项目：启动 Chromium → new_context → 访问页面
        → 收集结果 → close_context → 关闭浏览器
        每次检查完后彻底销毁浏览器实例，避免常驻内存导致崩溃。

        Args:
            projects: 项目配置列表
            result_callback: 每个项目检查完后的回调函数 async fn(result)
            per_project_sleep_range: 每个项目检查后的 sleep 区间（秒），等待内存释放
            full_round_sleep: 一轮全部检查完后的 sleep（秒）
        """
        if not _check_playwright():
            logger.warning("[BrowserInspection] Playwright 不可用，浏览器巡检循环不启动")
            return

        self._inspection_running = True
        self._inspection_stop_event.clear()
        self._inspection_pause_event.set()

        # UA/IP 轮换身份池
        identities = checker_identities or []
        identity_idx = 0

        logger.info(
            f"[BrowserInspection] 独立浏览器巡检循环启动，"
            f"共 {len(projects)} 个项目，"
            f"每轮间隔 {full_round_sleep}s，"
            f"UA 身份池: {len(identities)} 个"
        )

        while self._inspection_running and not self._inspection_stop_event.is_set():
            # 等待暂停解除
            await self._inspection_pause_event.wait()
            if not self._inspection_running or self._inspection_stop_event.is_set():
                break

            for idx, project in enumerate(projects):
                if not self._inspection_running or self._inspection_stop_event.is_set():
                    break
                if not self._inspection_pause_event.is_set():
                    break

                name = project.get("name", "unknown")
                try:
                    logger.info(f"[BrowserInspection] 开始检查 ({idx + 1}/{len(projects)}): {name}")

                    # ===== 按需启动浏览器 =====
                    launch_ok = await self._launch_ephemeral_browser()
                    if not launch_ok:
                        logger.error(f"[BrowserInspection] {name} 浏览器启动失败，跳过")
                        # 回调一个错误结果
                        if result_callback:
                            err_result = self._empty_result(error="浏览器启动失败")
                            err_result["project_name"] = name
                            err_result["project_url"] = project.get("url", "")
                            err_result["category"] = project.get("category", "")
                            err_result["timestamp"] = datetime.now(timezone.utc).isoformat()
                            try:
                                await result_callback(err_result)
                            except Exception as e:
                                logger.error(f"[BrowserInspection] 结果回调失败: {e}")
                        await self._inspection_sleep(min(per_project_sleep_range), max(per_project_sleep_range))
                        continue

                    # ===== 执行检查（轮换 UA + 模拟 IP）=====
                    current_identity = identities[identity_idx % len(identities)] if identities else None
                    identity_idx += 1
                    result = await self.check_project(project, take_screenshot=True, identity=current_identity)

                    # ===== 彻底销毁浏览器 =====
                    await self._destroy_browser()

                    # ===== 回调结果 =====
                    if result_callback:
                        try:
                            await result_callback(result)
                        except Exception as e:
                            logger.error(f"[BrowserInspection] 结果回调失败: {e}")

                    logger.info(
                        f"[BrowserInspection] {name} 完成: "
                        f"ok={result.get('browser_ok')}, "
                        f"js_errors={result.get('js_error_count', 0)}"
                    )

                except Exception as e:
                    logger.error(f"[BrowserInspection] {name} 检查异常: {e}")
                    # 确保浏览器被销毁
                    await self._destroy_browser()

                # 项目间等待，给系统回收内存的时间
                if (idx < len(projects) - 1
                        and self._inspection_running
                        and not self._inspection_stop_event.is_set()
                        and self._inspection_pause_event.is_set()):
                    await self._inspection_sleep(
                        min(per_project_sleep_range),
                        max(per_project_sleep_range),
                    )

            # 一轮结束，等待下一轮
            if self._inspection_running and not self._inspection_stop_event.is_set():
                logger.info(f"[BrowserInspection] 一轮巡检完成，{full_round_sleep}s 后开始下一轮")
                await self._inspection_sleep(full_round_sleep, full_round_sleep)

        # 确保最终销毁
        await self._destroy_browser()
        self._inspection_running = False
        logger.info("[BrowserInspection] 独立浏览器巡检循环已停止")

    async def _launch_ephemeral_browser(self) -> bool:
        """按需启动一个全新的浏览器实例（用于独立巡检循环）

        Returns:
            True 表示启动成功
        """
        # 先确保旧的已清理
        await self._destroy_browser()

        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--no-first-run",
                    "--disable-translate",
                    "--single-process",
                    "--disable-software-rasterizer",
                    "--disable-features=site-per-process",
                    "--js-flags=--max-old-space-size=256",
                ],
            )
            self._initialized = True
            self._consecutive_failures = 0
            logger.debug("[BrowserInspection] 临时浏览器已启动")
            return True
        except Exception as e:
            logger.error(f"[BrowserInspection] 临时浏览器启动失败: {e}")
            await self._destroy_browser()
            return False

    async def _destroy_browser(self):
        """彻底销毁浏览器实例（browser.close + playwright.stop + 全部置 None）"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._initialized = False

    async def _inspection_sleep(self, min_sec: float, max_sec: float):
        """可中断睡眠（被 stop 或 pause 时立即唤醒）"""
        import random
        total = random.uniform(min_sec, max_sec)
        elapsed = 0
        step = min(1.0, max(0.5, total / 30))
        while (elapsed < total
               and self._inspection_running
               and not self._inspection_stop_event.is_set()
               and self._inspection_pause_event.is_set()):
            await asyncio.sleep(min(step, total - elapsed))
            elapsed += step

    def pause_inspection(self):
        """暂停独立浏览器巡检循环"""
        self._inspection_pause_event.clear()
        logger.info("[BrowserInspection] 浏览器巡检已暂停")

    def resume_inspection(self):
        """恢复独立浏览器巡检循环"""
        self._inspection_pause_event.set()
        logger.info("[BrowserInspection] 浏览器巡检已恢复")

    def stop_inspection(self):
        """停止独立浏览器巡检循环"""
        self._inspection_running = False
        self._inspection_stop_event.set()
        self._inspection_pause_event.set()  # 解除暂停阻塞

    @property
    def inspection_running(self) -> bool:
        """独立浏览器巡检是否在运行"""
        return self._inspection_running

    @property
    def inspection_paused(self) -> bool:
        """独立浏览器巡检是否暂停"""
        return not self._inspection_pause_event.is_set()

    @property
    def available(self) -> bool:
        """浏览器是否可用"""
        return self._initialized and self._browser is not None

    async def _ensure_browser(self) -> bool:
        """确保浏览器实例存活，崩溃则彻底重建"""
        if self.available:
            try:
                if self._browser.is_connected():
                    return True
            except Exception:
                pass

        # 浏览器已死，彻底重建
        async with self._reconnect_lock:
            logger.warning("[BrowserChecker] 浏览器不可用，正在彻底重建...")
            # 彻底清理旧的 browser 和 playwright 实例
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._browser = None
            self._playwright = None
            self._initialized = False
            # 等 2 秒让 OS 回收内存和进程
            await asyncio.sleep(2)
            # 全新创建 playwright 实例
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--no-first-run",
                        "--disable-translate",
                        "--single-process",
                        "--disable-software-rasterizer",
                        "--disable-features=site-per-process",
                        "--js-flags=--max-old-space-size=256",
                    ],
                )
                self._initialized = True
                self._consecutive_failures = 0
                logger.info("[BrowserChecker] 浏览器重建成功")
                return True
            except Exception as e:
                logger.error(f"[BrowserChecker] 浏览器重建失败: {e}")
                self._browser = None
                self._playwright = None
                self._initialized = False
                return False


    async def check_project(self, project: dict, take_screenshot: bool = True, identity: dict | None = None) -> dict:
        """对单个项目执行浏览器巡检
        
        Args:
            project: 项目配置 {"name": ..., "url": ..., "category": ...}
            take_screenshot: 是否截图
        
        Returns:
            浏览器检查结果 dict
        """
        # 确保浏览器存活（自动重连）
        if not await self._ensure_browser():
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.error(f"[BrowserChecker] 浏览器连续失败 {self._consecutive_failures} 次，暂停浏览器检查")
            return self._empty_result(error="浏览器不可用，重连失败")

        url = project["url"]
        name = project["name"]
        result = self._empty_result()
        result["project_name"] = name
        result["project_url"] = url
        result["category"] = project.get("category", "")
        result["timestamp"] = datetime.now(timezone.utc).isoformat()

        async with self._semaphore:
            context = None
            page = None
            try:
                # 创建隔离的浏览器上下文（每个检查独立，避免 Cookie/缓存干扰）
                # 使用传入的 identity（轮换 UA + 模拟 IP），若未指定则使用默认 Chrome UA
                if identity:
                    ua = identity.get("user_agent", "")
                    ip_pool = identity.get("ip_pool", [])
                    simulated_ip = random.choice(ip_pool) if ip_pool else ""
                    identity_name = identity.get("name", "unknown")
                    logger.info(f"[BrowserChecker] {name} 使用身份: {identity_name}, 模拟IP: {simulated_ip}")
                else:
                    ua = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    )
                    simulated_ip = ""

                extra_headers = {}
                if simulated_ip:
                    extra_headers["X-Forwarded-For"] = simulated_ip
                    extra_headers["X-Real-IP"] = simulated_ip

                context = await self._browser.new_context(
                    viewport={"width": 1024, "height": 600},  # 缩小视口节省内存
                    user_agent=ua,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    bypass_csp=True,
                    extra_http_headers=extra_headers if extra_headers else None,
                )

                # 设置超时
                context.set_default_timeout(20000)  # 20 秒
                context.set_default_navigation_timeout(25000)  # 导航 25 秒

                page = await context.new_page()

                # ===== 1. 收集 Console 消息和 JS 错误 =====
                console_errors = []
                js_errors = []
                sw_events = []

                def on_console(msg):
                    if msg.type == "error":
                        console_errors.append({
                            "text": msg.text[:500],
                            "type": msg.type,
                        })

                def on_pageerror(error):
                    js_errors.append({
                        "text": str(error)[:500],
                        "source": "pageerror",
                    })

                page.on("console", on_console)
                page.on("pageerror", on_pageerror)

                # ===== 2. 访问首页 =====
                start_time = time.time()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                navigation_time = time.time() - start_time

                result["http_status"] = response.status if response else None
                result["navigation_time_ms"] = round(navigation_time * 1000, 1)

                if response and response.status >= 400:
                    result["browser_ok"] = False
                    result["error"] = f"HTTP {response.status}"
                    if take_screenshot:
                        result["screenshot"] = await self._take_screenshot(page, name, "error")
                    return result

                # ===== 3. 等待页面基本渲染完成 =====
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass  # networkidle 超时不算致命错误

                # ===== 4. 检查页面是否正常渲染 =====
                render_check = await self._check_rendering(page)
                result["page_rendered"] = render_check["rendered"]
                result["visible_text_length"] = render_check["text_length"]
                result["visible_elements_count"] = render_check["elements_count"]
                result["has_meaningful_content"] = render_check["has_content"]

                if not render_check["rendered"]:
                    result["browser_ok"] = False
                    result["error"] = "页面未正常渲染（空白或无有效内容）"

                # ===== 5. 检查 Service Worker 状态 =====
                sw_status = await self._check_service_worker(page)
                result["sw_status"] = sw_status

                # ===== 6. 尝试页面交互 =====
                interactions = await self._check_interactions(page)
                result["interactions_checked"] = interactions

                # ===== 7. 截图 =====
                if take_screenshot:
                    result["screenshot"] = await self._take_screenshot(page, name, "normal")

                # ===== 汇总 =====
                result["browser_ok"] = result.get("browser_ok", True)
                result["console_errors"] = console_errors
                result["console_error_count"] = len(console_errors)
                result["js_errors"] = js_errors
                result["js_error_count"] = len(js_errors)

                # 如果有严重 JS 错误，标记为异常
                if len(js_errors) > 5:
                    result["browser_ok"] = False
                    if not result.get("error"):
                        result["error"] = f"页面存在 {len(js_errors)} 个 JS 错误"

            except Exception as e:
                error_msg = str(e)[:200]
                result["browser_ok"] = False
                result["error"] = f"浏览器检查异常: {error_msg}"
                logger.warning(f"[BrowserChecker] {name} 检查异常: {e}")

                # 检测浏览器是否崩溃
                if "has been closed" in error_msg or "Target page" in error_msg or "NoneType" in error_msg:
                    logger.warning("[BrowserChecker] 检测到浏览器崩溃，标记需要重建")
                    self._initialized = False
                    try:
                        if self._browser:
                            await self._browser.close()
                    except Exception:
                        pass
                    self._browser = None

                # 尝试截图（如果页面还活着）
                if page and take_screenshot:
                    try:
                        result["screenshot"] = await self._take_screenshot(page, name, "exception")
                    except Exception:
                        pass
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

        logger.info(
            f"[BrowserChecker] {name}: "
            f"rendered={result['page_rendered']}, "
            f"js_errors={result['js_error_count']}, "
            f"console_errors={result['console_error_count']}, "
            f"sw={result['sw_status']}"
        )
        return result

    async def _check_rendering(self, page) -> dict:
        """检查页面是否正常渲染"""
        try:
            # 获取可见文本内容长度和关键元素数量
            result = await page.evaluate("""() => {
                const body = document.body;
                if (!body) return { textLength: 0, elementsCount: 0, hasContent: false };
                
                // 可见文本长度
                const text = body.innerText || body.textContent || '';
                const textLength = text.trim().length;
                
                // 有意义的可见元素数量（排除 script/style）
                const meaningfulSelectors = [
                    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                    'p', 'a[href]', 'button', 'input', 'img',
                    'nav', 'header', 'main', 'footer', 'section',
                    'div[class]', 'span[class]', 'li'
                ];
                let elementsCount = 0;
                for (const sel of meaningfulSelectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                elementsCount++;
                            }
                        }
                    } catch(e) {}
                }
                
                return {
                    textLength: textLength,
                    elementsCount: elementsCount,
                    hasContent: textLength > 50 || elementsCount > 3,
                };
            }""")

            return {
                "rendered": result["hasContent"],
                "text_length": result["textLength"],
                "elements_count": result["elementsCount"],
                "has_content": result["hasContent"],
            }
        except Exception as e:
            return {
                "rendered": False,
                "text_length": 0,
                "elements_count": 0,
                "has_content": False,
            }

    async def _check_service_worker(self, page) -> str:
        """检查 Service Worker 状态"""
        try:
            sw_status = await page.evaluate("""() => {
                if (!('serviceWorker' in navigator)) return 'unsupported';
                return navigator.serviceWorker.controller 
                    ? 'registered' 
                    : (navigator.serviceWorker.ready 
                        ? 'registered_no_controller' 
                        : 'none');
            }""")

            # 监听 SW 注册错误
            sw_error = await page.evaluate("""() => {
                return new Promise((resolve) => {
                    if (!('serviceWorker' in navigator)) {
                        resolve(null);
                        return;
                    }
                    // 检查是否有已注册的 SW
                    navigator.serviceWorker.getRegistrations().then((regs) => {
                        if (regs.length > 0) {
                            resolve('active');
                        } else {
                            resolve(null);
                        }
                    }).catch(() => resolve(null));
                    
                    // 3秒超时
                    setTimeout(() => resolve(null), 3000);
                });
            }""")

            if sw_error == 'active':
                return 'registered_active'
            return sw_status

        except Exception:
            return "check_failed"

    async def _check_interactions(self, page) -> list[dict]:
        """尝试页面交互检查"""
        interactions = []

        # 1. 尝试点击导航链接
        try:
            nav_links = await page.query_selector_all("nav a, header a, .nav a, .menu a, [role='navigation'] a")
            if nav_links:
                # 只测试前 2 个链接
                for link in nav_links[:2]:
                    try:
                        href = await link.get_attribute("href")
                        text = (await link.inner_text()).strip()[:50]
                        is_visible = await link.is_visible()
                        if is_visible and href and href != "#" and not href.startswith("javascript:"):
                            interactions.append({
                                "type": "nav_link",
                                "text": text,
                                "href": href[:100],
                                "clickable": True,
                                "status": "ok",
                            })
                    except Exception:
                        pass
        except Exception:
            pass

        # 2. 尝试点击按钮
        try:
            buttons = await page.query_selector_all("button:not([disabled]), .btn, [role='button']")
            if buttons:
                for btn in buttons[:2]:
                    try:
                        text = (await btn.inner_text()).strip()[:50]
                        is_visible = await btn.is_visible()
                        if is_visible and text:
                            interactions.append({
                                "type": "button",
                                "text": text,
                                "clickable": True,
                                "status": "ok",
                            })
                    except Exception:
                        pass
        except Exception:
            pass

        # 3. 检查页面是否有明显的死链或错误
        try:
            broken = await page.evaluate("""() => {
                const images = document.querySelectorAll('img');
                let brokenImages = 0;
                for (const img of images) {
                    if (img.naturalWidth === 0 && img.src) brokenImages++;
                }
                return { brokenImages };
            }""")
            if broken.get("brokenImages", 0) > 0:
                interactions.append({
                    "type": "broken_images",
                    "count": broken["brokenImages"],
                    "status": "warning",
                })
        except Exception:
            pass

        return interactions

    async def _take_screenshot(self, page, name: str, tag: str = "normal") -> str | None:
        """截图并保存，返回截图文件路径"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
            filename = f"{safe_name}_{tag}_{ts}.png"
            filepath = os.path.join(self._screenshot_dir, filename)

            await page.screenshot(path=filepath, full_page=False)
            logger.debug(f"[BrowserChecker] 截图已保存: {filepath}")
            return filepath
        except Exception as e:
            logger.warning(f"[BrowserChecker] 截图失败: {e}")
            return None

    @staticmethod
    def _empty_result(error: str = None) -> dict:
        return {
            "browser_ok": error is None,
            "page_rendered": False,
            "js_errors": [],
            "js_error_count": 0,
            "console_errors": [],
            "console_error_count": 0,
            "sw_status": "unknown",
            "interactions_checked": [],
            "screenshot": None,
            "visible_text_length": 0,
            "visible_elements_count": 0,
            "has_meaningful_content": False,
            "http_status": None,
            "navigation_time_ms": None,
            "error": error,
        }


# ========== 全局单例 ==========
_browser_checker: BrowserChecker | None = None


def get_browser_checker() -> BrowserChecker:
    """获取全局 BrowserChecker 单例"""
    global _browser_checker
    if _browser_checker is None:
        _browser_checker = BrowserChecker()
    return _browser_checker


async def init_browser_checker() -> BrowserChecker:
    """初始化并返回全局 BrowserChecker"""
    bc = get_browser_checker()
    await bc.initialize()
    return bc


async def shutdown_browser_checker():
    """关闭全局 BrowserChecker"""
    global _browser_checker
    if _browser_checker:
        await _browser_checker.shutdown()
