"""本地 Visitor 客户端 - 在用户本地 Windows 电脑上运行，使用真实浏览器模拟访问

功能：
1. 连接服务器 API 获取项目列表和配置
2. 使用 Playwright 启动真实浏览器（Chromium）
3. 按 visit_count 权重随机选择项目，用真实浏览器访问
4. 模拟真实用户行为（随机UA、滚动、点击内链、停留时间）
5. 访问间隔可配置
6. 将访问结果回报给服务器 API
"""

import argparse
import asyncio
import json
import os
import platform
import random
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright


# ========== User-Agent 池 ==========
USER_AGENTS = [
    # 桌面端
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "type": "desktop",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "type": "desktop",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "type": "desktop",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "type": "desktop",
    },
    # 移动端
    {
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.118 Mobile/15E148 Safari/604.1",
        "type": "mobile",
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.119 Mobile Safari/537.36",
        "type": "mobile",
    },
    {
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "type": "mobile",
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; SM-S928U1) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/124.0.6367.113 Mobile Safari/537.36",
        "type": "mobile",
    },
    # 平板
    {
        "ua": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "type": "tablet",
    },
]


def get_client_id() -> str:
    """获取或生成本地客户端唯一标识（持久化到本地文件）"""
    client_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_id.txt")
    if os.path.exists(client_file):
        try:
            with open(client_file, "r", encoding="utf-8") as f:
                cid = f.read().strip()
                if cid:
                    return cid
        except Exception:
            pass
    cid = f"local-{uuid.uuid4().hex[:12]}"
    try:
        with open(client_file, "w", encoding="utf-8") as f:
            f.write(cid)
    except Exception:
        pass
    return cid


class LocalVisitor:
    """本地 Visitor 客户端"""

    def __init__(self, server: str, interval_min: int = 5, interval_max: int = 15,
                 headless: bool = False, max_visits: int = 0):
        self.server = server.rstrip("/")
        self.interval_min = interval_min
        self.interval_max = interval_max
        self.headless = headless
        self.max_visits = max_visits  # 0=无限
        self.client_id = get_client_id()
        self.projects: list[dict] = []
        self.visit_counts: dict[str, int] = {}
        self.total_visits = 0
        self.success_visits = 0
        self.failed_visits = 0

        print(f"[LocalVisitor] 客户端 ID: {self.client_id}")
        print(f"[LocalVisitor] 服务器: {self.server}")
        print(f"[LocalVisitor] 访问间隔: {interval_min}-{interval_max} 分钟")
        print(f"[LocalVisitor] 浏览器模式: {'无头' if headless else '有头'}")
        print(f"[LocalVisitor] 最大访问次数: {max_visits if max_visits > 0 else '无限'}")

    async def fetch_config(self) -> bool:
        """从服务器获取项目列表和访问权重配置"""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self.server}/api/status")
                resp.raise_for_status()
                data = resp.json()

            projects_data = data.get("projects", {})
            self.projects = []
            self.visit_counts = {}

            for pname, pdata in projects_data.items():
                project = {
                    "name": pdata.get("project_name", pname),
                    "url": pdata.get("project_url", ""),
                    "category": pdata.get("category", ""),
                }
                self.projects.append(project)
                self.visit_counts[pname] = pdata.get("visit_count", 5)

            print(f"[Config] 已加载 {len(self.projects)} 个项目配置")
            return True
        except Exception as e:
            print(f"[Config] 获取服务器配置失败: {e}")
            return False

    def _pick_project_by_weight(self) -> dict | None:
        """按 visit_count 权重随机选择项目"""
        if not self.projects:
            return None
        weights = [self.visit_counts.get(p["name"], 5) for p in self.projects]
        total = sum(weights)
        if total <= 0:
            return random.choice(self.projects)
        r = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return self.projects[i]
        return self.projects[-1]

    def _pick_random_ua(self) -> dict:
        """随机选择 User-Agent"""
        return random.choice(USER_AGENTS)

    async def _random_scroll(self, page):
        """模拟随机滚动页面"""
        try:
            scroll_count = random.randint(1, 4)
            for _ in range(scroll_count):
                await asyncio.sleep(random.uniform(0.5, 1.5))
                scroll_height = await page.evaluate("() => document.body.scrollHeight")
                target_y = random.randint(0, max(100, scroll_height - 500))
                await page.evaluate(f"window.scrollTo({{ top: {target_y}, behavior: 'smooth' }})")
                await asyncio.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

    async def _find_internal_links(self, page, base_url: str) -> list[str]:
        """提取页面内链"""
        try:
            links = await page.evaluate("""(baseUrl) => {
                const base = new URL(baseUrl);
                const links = Array.from(document.querySelectorAll('a[href]'));
                const internal = new Set();
                for (const a of links) {
                    try {
                        const url = new URL(a.href, baseUrl);
                        if (url.hostname === base.hostname && url.protocol.startsWith('http')) {
                            // 排除锚点、mailto、javascript 等
                            if (!url.hash && !url.href.startsWith('mailto:') && !url.href.startsWith('javascript:')) {
                                internal.add(url.href);
                            }
                        }
                    } catch(e) {}
                }
                return Array.from(internal);
            }""", base_url)
            return links
        except Exception:
            return []

    async def visit_project(self, project: dict, browser) -> dict:
        """使用真实浏览器访问项目"""
        ua_info = self._pick_random_ua()
        result = {
            "client_id": self.client_id,
            "project_name": project["name"],
            "project_url": project["url"],
            "success": False,
            "pages_visited": 0,
            "duration_seconds": 0,
            "user_agent": ua_info["ua"],
            "device_type": ua_info["type"],
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        start_time = time.time()
        context = None

        try:
            # 创建上下文（模拟独立浏览器会话）
            viewport = None
            if ua_info["type"] == "mobile":
                viewport = {"width": 390, "height": 844, "isMobile": True,
                            "hasTouch": True, "deviceScaleFactor": 3}
            elif ua_info["type"] == "tablet":
                viewport = {"width": 768, "height": 1024, "isMobile": True,
                            "hasTouch": True, "deviceScaleFactor": 2}
            else:
                viewport = {"width": 1366, "height": 768}

            context = await browser.new_context(
                user_agent=ua_info["ua"],
                viewport=viewport,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                ignore_https_errors=True,
            )

            page = await context.new_page()

            # 访问首页
            print(f"  → 访问首页: {project['name']} ({project['url']})")
            await page.goto(project["url"], wait_until="domcontentloaded", timeout=30000)

            # 等待页面加载
            await asyncio.sleep(random.uniform(2, 5))

            # 随机滚动
            await self._random_scroll(page)

            result["pages_visited"] = 1
            result["success"] = True

            # 提取内链并随机点击
            internal_links = await self._find_internal_links(page, project["url"])
            if internal_links:
                num_clicks = min(random.randint(1, 3), len(internal_links))
                sampled_links = random.sample(internal_links, num_clicks)

                for link_url in sampled_links:
                    try:
                        print(f"    → 内页: {link_url[:80]}")
                        await page.goto(link_url, wait_until="domcontentloaded", timeout=20000)
                        # 页面停留
                        await asyncio.sleep(random.uniform(3, 10))
                        # 滚动
                        await self._random_scroll(page)
                        result["pages_visited"] += 1
                    except Exception as e:
                        print(f"    ⚠ 内页访问失败: {link_url[:50]} - {e}")
                        continue

            await page.close()

        except Exception as e:
            result["success"] = False
            result["error"] = str(e)[:200]
            print(f"  ✗ 访问失败: {e}")
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        result["duration_seconds"] = round(time.time() - start_time, 2)
        return result

    async def report_result(self, result: dict) -> bool:
        """上报访问结果到服务器"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.server}/api/local-visit-result",
                    json=result,
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            print(f"  [上报失败] {e}")
            return False

    async def run(self):
        """主运行循环"""
        print(f"\n[LocalVisitor] 启动本地浏览器模拟访问客户端")
        print("=" * 60)

        # 首次获取配置
        if not await self.fetch_config():
            print("[Error] 无法获取服务器配置，请检查服务器地址是否正确")
            sys.exit(1)

        async with async_playwright() as p:
            # 启动浏览器
            print(f"\n[Browser] 启动 Chromium 浏览器（{'无头' if self.headless else '有头'}模式）...")
            browser = await p.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            print("[Browser] 浏览器已启动\n")

            try:
                while True:
                    # 检查是否达到最大访问次数
                    if self.max_visits > 0 and self.total_visits >= self.max_visits:
                        print(f"\n[完成] 已达到最大访问次数 {self.max_visits}，程序退出")
                        break

                    # 定期刷新配置（每 10 次访问刷新一次）
                    if self.total_visits > 0 and self.total_visits % 10 == 0:
                        print("\n[Config] 刷新项目配置...")
                        await self.fetch_config()

                    # 按权重选择项目
                    project = self._pick_project_by_weight()
                    if not project:
                        print("[Error] 没有可访问的项目")
                        await asyncio.sleep(60)
                        continue

                    self.total_visits += 1
                    print(f"\n[{self.total_visits}] 第 {self.total_visits} 次访问")
                    print(f"  项目: {project['name']}")
                    print(f"  URL: {project['url']}")

                    # 执行访问
                    result = await self.visit_project(project, browser)

                    if result["success"]:
                        self.success_visits += 1
                        print(f"  ✓ 成功（{result['pages_visited']} 页，{result['duration_seconds']} 秒）")
                    else:
                        self.failed_visits += 1
                        print(f"  ✗ 失败: {result.get('error', '未知错误')}")

                    # 上报结果
                    await self.report_result(result)

                    # 打印统计
                    print(f"  统计: 成功 {self.success_visits} / 失败 {self.failed_visits} / 总计 {self.total_visits}")

                    # 随机间隔
                    if self.max_visits <= 0 or self.total_visits < self.max_visits:
                        interval_minutes = random.uniform(self.interval_min, self.interval_max)
                        interval_seconds = int(interval_minutes * 60)
                        print(f"  下次访问: {interval_minutes:.1f} 分钟后 ({interval_seconds} 秒)")
                        await asyncio.sleep(interval_seconds)

            except KeyboardInterrupt:
                print("\n\n[中断] 用户手动停止")
            finally:
                print("\n[Browser] 关闭浏览器...")
                await browser.close()
                print("[Browser] 浏览器已关闭")

        print(f"\n[LocalVisitor] 运行结束")
        print(f"  总计: {self.total_visits} 次")
        print(f"  成功: {self.success_visits} 次")
        print(f"  失败: {self.failed_visits} 次")


def main():
    parser = argparse.ArgumentParser(description="本地 Visitor 客户端 - 真实浏览器模拟访问")
    parser.add_argument(
        "--server",
        default="http://47.113.216.237:8700",
        help="服务器地址（默认 http://47.113.216.237:8700）",
    )
    parser.add_argument(
        "--interval-min",
        type=int,
        default=5,
        help="最小访问间隔（分钟，默认 5）",
    )
    parser.add_argument(
        "--interval-max",
        type=int,
        default=15,
        help="最大访问间隔（分钟，默认 15）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（默认有头模式，方便看到浏览器）",
    )
    parser.add_argument(
        "--max-visits",
        type=int,
        default=0,
        help="最大访问次数，0 表示无限（默认 0）",
    )

    args = parser.parse_args()

    # 校验参数
    if args.interval_min < 1:
        args.interval_min = 1
    if args.interval_max < args.interval_min:
        args.interval_max = args.interval_min

    visitor = LocalVisitor(
        server=args.server,
        interval_min=args.interval_min,
        interval_max=args.interval_max,
        headless=args.headless,
        max_visits=args.max_visits,
    )

    try:
        asyncio.run(visitor.run())
    except KeyboardInterrupt:
        print("\n程序已停止")
        sys.exit(0)


if __name__ == "__main__":
    main()
