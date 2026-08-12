"""异步 Checker 引擎 - 通过搜索引擎搜索关键词后访问目标网站"""
import asyncio
import json
import re
import time
import random
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import httpx

from config import (
    ASYNC_CHECKER_IDENTITIES,
    PROJECTS,
    REQUEST_TIMEOUT,
    SLOW_THRESHOLD,
    get_random_ip,
)


# 每个异步Checker的独立IP池
def _gen_async_ip_pool(base_id: int) -> list:
    """生成异步Checker的IP池"""
    octets = [
        10 + (base_id * 7) % 240,
        20 + (base_id * 13) % 230,
        30 + (base_id * 17) % 220,
        40 + (base_id * 23) % 210,
        50 + (base_id * 29) % 200,
    ]
    return [f"{100 + base_id}.{octets[i % 5]}.{octets[(i+2) % 5]}.{octets[(i+3) % 5]}" for i in range(5)]


class AsyncChecker:
    """异步 Checker - 通过搜索引擎搜索关键词后访问目标网站，模拟真实搜索流量"""

    def __init__(self, checker_id: int, projects: list[dict]):
        self.id = checker_id
        identity = ASYNC_CHECKER_IDENTITIES[(checker_id - 1) % len(ASYNC_CHECKER_IDENTITIES)]
        self.user_agent = identity["user_agent"]
        self.type = identity["type"]
        self.name = f"Async-{checker_id:02d}"
        self.ip_pool = _gen_async_ip_pool(checker_id)
        self.projects = projects

        self.running = False
        self.task = None
        self.check_count = 0
        self.current_task = "空闲"
        self.last_check_time = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()  # 暂停事件（set表示运行中，clear表示暂停）
        self._pause_event.set()  # 默认不暂停

        # 导入运行时配置（延迟导入避免循环引用）
        from config import RuntimeConfig
        self._config = RuntimeConfig.get_instance()

    def _build_headers(self, referer: str = "") -> dict:
        ip = get_random_ip(self.ip_pool)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "X-Forwarded-For": ip,
            "X-Real-IP": ip,
        }
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "cross-site"
        return headers

    async def _search_baidu(self, keyword: str, client: httpx.AsyncClient) -> list[dict]:
        """百度搜索，返回结果列表 [{title, url}]"""
        search_url = f"https://www.baidu.com/s?wd={quote(keyword)}&rn=10"
        headers = self._build_headers()
        try:
            resp = await client.get(search_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return []
            html = resp.text

            results = []
            # 百度搜索结果匹配（简化版）
            # 匹配 class="result" 的条目
            pattern = re.compile(
                r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            matches = pattern.findall(html)
            for href, title in matches:
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                if clean_title and href.startswith("http"):
                    results.append({"title": clean_title, "url": href})

            return results[:10]
        except Exception as e:
            print(f"[AsyncChecker-{self.id}] 百度搜索失败: {e}")
            return []

    async def _search_bing(self, keyword: str, client: httpx.AsyncClient) -> list[dict]:
        """必应搜索"""
        search_url = f"https://www.bing.com/search?q={quote(keyword)}&count=10"
        headers = self._build_headers()
        try:
            resp = await client.get(search_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return []
            html = resp.text

            results = []
            pattern = re.compile(
                r'<li[^>]*class="b_algo"[^>]*>.*?<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            matches = pattern.findall(html)
            for href, title in matches:
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                if clean_title and href.startswith("http"):
                    results.append({"title": clean_title, "url": href})

            return results[:10]
        except Exception as e:
            print(f"[AsyncChecker-{self.id}] 必应搜索失败: {e}")
            return []

    async def _search_google(self, keyword: str, client: httpx.AsyncClient) -> list[dict]:
        """Google搜索"""
        search_url = f"https://www.google.com/search?q={quote(keyword)}&num=10"
        headers = self._build_headers()
        try:
            resp = await client.get(search_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return []
            html = resp.text

            results = []
            # Google 搜索结果 href="/url?q=xxx&..."
            pattern = re.compile(
                r'<a[^>]*href="/url\?q=([^"&]+)&[^"]*"[^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            matches = pattern.findall(html)
            from urllib.parse import unquote
            for href_enc, title in matches:
                href = unquote(href_enc)
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                if clean_title and href.startswith("http") and "google" not in href.lower():
                    results.append({"title": clean_title, "url": href})

            return results[:10]
        except Exception as e:
            print(f"[AsyncChecker-{self.id}] Google搜索失败: {e}")
            return []

    async def search(self, keyword: str, engine: str = "baidu") -> list[dict]:
        """执行搜索引擎搜索"""
        async with httpx.AsyncClient(
            follow_redirects=True,
            verify=True,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            if engine == "baidu":
                return await self._search_baidu(keyword, client)
            elif engine == "bing":
                return await self._search_bing(keyword, client)
            elif engine == "google":
                return await self._search_google(keyword, client)
            else:
                return await self._search_baidu(keyword, client)

    def _match_target_url(self, search_results: list[dict], target_url: str) -> dict | None:
        """从搜索结果中匹配目标网站的链接（优先匹配域名）"""
        try:
            target_domain = urlparse(target_url).netloc.lower()
        except Exception:
            return None

        for result in search_results:
            try:
                result_domain = urlparse(result["url"]).netloc.lower()
                if result_domain == target_domain or target_domain in result_domain:
                    return result
            except Exception:
                continue
        # 没找到完全匹配，返回第一个结果
        return search_results[0] if search_results else None

    async def _visit_page(self, url: str, referer: str = "", client: httpx.AsyncClient | None = None) -> dict:
        """访问页面，返回检测结果（核心检测逻辑，与同步Checker类似但来源标注不同）"""
        headers = self._build_headers(referer)
        start_time = time.time()
        result = {
            "status_code": None,
            "response_time_ms": None,
            "content_length": 0,
            "title_text": "",
            "error": None,
        }

        async def do_visit(c):
            nonlocal result
            resp = await c.get(url)
            elapsed = time.time() - start_time
            result["status_code"] = resp.status_code
            result["response_time_ms"] = round(elapsed * 1000, 2)
            html = resp.text
            result["content_length"] = len(html)
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                result["title_text"] = title_match.group(1).strip()[:100]

        try:
            if client:
                await do_visit(client)
            else:
                async with httpx.AsyncClient(
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                    verify=True,
                ) as c:
                    await do_visit(c)
        except httpx.TimeoutException:
            elapsed = time.time() - start_time
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = "请求超时"
        except httpx.ConnectError as e:
            elapsed = time.time() - start_time
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"连接失败: {str(e)[:80]}"
        except Exception as e:
            elapsed = time.time() - start_time
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"未知错误: {str(e)[:80]}"

        return result

    async def check_project(self, project: dict) -> dict:
        """通过搜索引擎+关键词访问方式检测项目"""
        self.current_task = f"搜索访问: {project['name']}"

        result = {
            "project_name": project["name"],
            "project_url": project["url"],
            "category": project.get("category", ""),
            "checker_id": self.id,
            "checker_name": self.name,
            "checker_type": "async_search",
            "device_type": self.type,
            "source_ip": get_random_ip(self.ip_pool),
            "status": "unknown",
            "status_code": None,
            "response_time_ms": None,
            "search_keyword": "",
            "search_engine": self._config.search_engine,
            "search_result_count": 0,
            "clicked_url": "",
            "content_check": {},
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 选择搜索关键词
        keywords = self._config.get_project_keywords(project["name"])
        if not keywords:
            keywords = [project["name"]]
        keyword = random.choice(keywords)
        result["search_keyword"] = keyword

        try:
            # 第一步：搜索引擎搜索
            search_results = await self.search(keyword, self._config.search_engine)
            result["search_result_count"] = len(search_results)

            if not search_results:
                result["status"] = "offline"
                result["error"] = f"搜索引擎未返回结果 ({self._config.search_engine})"
                self.check_count += 1
                self.last_check_time = datetime.now(timezone.utc).isoformat()
                self.current_task = "空闲"
                return result

            # 第二步：匹配目标链接并点击访问
            matched = self._match_target_url(search_results, project["url"])
            if not matched:
                # 没匹配到目标网站，访问第一个结果
                matched = search_results[0]
                result["error"] = "搜索结果中未匹配到目标域名，已访问首条结果"

            click_url = matched["url"]
            result["clicked_url"] = click_url

            # 第三步：访问目标页面（带搜索来源Referer）
            search_url_map = {
                "baidu": "https://www.baidu.com/",
                "bing": "https://www.bing.com/",
                "google": "https://www.google.com/",
            }
            referer = search_url_map.get(self._config.search_engine, "https://www.baidu.com/")

            async with httpx.AsyncClient(
                headers=self._build_headers(referer),
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as client:
                visit_result = await self._visit_page(click_url, referer, client)

            result["status_code"] = visit_result["status_code"]
            result["response_time_ms"] = visit_result["response_time_ms"]
            result["content_check"] = {
                "content_length": visit_result["content_length"],
                "title_text": visit_result["title_text"],
                "has_title": bool(visit_result["title_text"]),
            }

            if visit_result["error"]:
                result["status"] = "offline"
                result["error"] = visit_result["error"]
            elif visit_result["status_code"] == 200:
                if visit_result["response_time_ms"] and visit_result["response_time_ms"] > SLOW_THRESHOLD * 1000:
                    result["status"] = "slow"
                else:
                    result["status"] = "online"
            else:
                result["status"] = "offline"

        except Exception as e:
            result["status"] = "offline"
            result["error"] = f"搜索访问异常: {str(e)[:80]}"

        self.check_count += 1
        self.last_check_time = datetime.now(timezone.utc).isoformat()
        self.current_task = "空闲"

        return result

    async def run_loop(self):
        """异步Checker主循环"""
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()

        while self.running:
            # 检查是否需要暂停
            await self._pause_event.wait()
            if not self.running:
                break

            for project in self.projects:
                if not self.running:
                    break
                # 检查暂停状态
                if not self._pause_event.is_set():
                    break

                # 多轮检测（每轮检查 rounds_per_inspection 次）
                rounds = max(1, min(10, self._config.rounds_per_inspection))
                for round_i in range(rounds):
                    if not self.running:
                        break
                    if not self._pause_event.is_set():
                        break

                    try:
                        result = await self.check_project(project)
                        from checker import CheckerManager
                        await CheckerManager.save_async_result(result)
                    except Exception as e:
                        print(f"[AsyncChecker-{self.id}] 检测 {project['name']} 异常: {e}")

                    # 轮内间隔
                    if round_i < rounds - 1:
                        interval = self._config.rounds_interval_seconds
                        await self._sleep_interruptible(interval)

                # 项目间间隔（短暂停留，模拟浏览）
                if self.running and self._pause_event.is_set():
                    await self._sleep_interruptible(random.uniform(2, 5))

            # 等待下一轮巡检
            if self.running and self._pause_event.is_set():
                interval = self._config.get_interval_seconds()
                await self._sleep_interruptible(interval)

        self.running = False

    async def _sleep_interruptible(self, seconds: float):
        """可中断的睡眠（被stop或pause时立即唤醒）"""
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.sleep(seconds)),
                timeout=0.1,
            )
        except asyncio.TimeoutError:
            # 分段睡眠，定期检查状态
            elapsed = 0
            step = min(1.0, seconds / 10)
            while elapsed < seconds and self.running and self._pause_event.is_set():
                await asyncio.sleep(min(step, seconds - elapsed))
                elapsed += step

    def stop(self):
        """停止Checker"""
        self.running = False
        self._stop_event.set()
        self._pause_event.set()  # 解除暂停阻塞

    def pause(self):
        """暂停巡检（不结束进程）"""
        self._pause_event.clear()

    def resume(self):
        """恢复巡检"""
        self._pause_event.set()

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": "async_search",
            "device_type": self.type,
            "user_agent": self.user_agent,
            "running": self.running,
            "paused": self.paused,
            "check_count": self.check_count,
            "current_task": self.current_task,
            "last_check_time": self.last_check_time,
            "project_count": len(self.projects),
            "projects": [p["name"] for p in self.projects],
            "ip_sample": self.ip_pool[0],
        }


class AsyncCheckerManager:
    """异步 Checker 管理器 - 动态增减"""

    _checkers: dict[int, AsyncChecker] = {}
    _results: dict[str, list[dict]] = {}
    _latest: dict[str, dict] = {}
    _lock = asyncio.Lock()
    _next_id = 100  # 异步Checker ID 从100开始，避免与同步Checker冲突

    @classmethod
    async def initialize(cls):
        """初始化（根据配置创建异步Checker）"""
        from config import RuntimeConfig
        config = RuntimeConfig.get_instance()
        await cls._adjust_checkers(config.async_checker_count)
        print(f"[AsyncCheckerManager] 已初始化，异步Checker数量: {len(cls._checkers)}")

    @classmethod
    async def _adjust_checkers(cls, target_count: int):
        """调整异步Checker数量到目标值"""
        current = len(cls._checkers)
        if current == target_count:
            return

        if target_count > current:
            # 增加
            projects = list(PROJECTS)
            for i in range(target_count - current):
                checker_id = cls._next_id
                cls._next_id += 1
                # 轮询分配项目
                idx = (checker_id - 100) % len(projects)
                assigned_projects = [projects[idx]]  # 每个异步Checker先分配1个项目
                checker = AsyncChecker(checker_id, assigned_projects)
                cls._checkers[checker_id] = checker
                checker.task = asyncio.create_task(checker.run_loop())
                print(f"[AsyncCheckerManager] 新增异步Checker #{checker_id}")
        else:
            # 减少（从最后一个开始删）
            ids_sorted = sorted(cls._checkers.keys(), reverse=True)
            to_remove = ids_sorted[:current - target_count]
            for cid in to_remove:
                checker = cls._checkers.pop(cid)
                checker.stop()
                if checker.task:
                    checker.task.cancel()
                print(f"[AsyncCheckerManager] 移除异步Checker #{cid}")

        # 重新分配项目（尽量均匀）
        await cls._redistribute_projects()

    @classmethod
    async def _redistribute_projects(cls):
        """将项目均匀分配给所有异步Checker"""
        if not cls._checkers:
            return
        checker_list = list(cls._checkers.values())
        projects = list(PROJECTS)
        # 轮询分配
        for i, checker in enumerate(checker_list):
            assigned = []
            for j in range(i, len(projects), len(checker_list)):
                assigned.append(projects[j])
            checker.projects = assigned if assigned else [projects[i % len(projects)]]

    @classmethod
    async def set_count(cls, count: int):
        """设置异步Checker数量"""
        count = max(0, min(20, count))
        async with cls._lock:
            await cls._adjust_checkers(count)

    @classmethod
    async def start_all(cls):
        """启动所有异步Checker"""
        for checker in cls._checkers.values():
            if not checker.running:
                checker.task = asyncio.create_task(checker.run_loop())

    @classmethod
    async def stop_all(cls):
        """停止所有异步Checker"""
        for checker in cls._checkers.values():
            if checker.running:
                checker.stop()
                if checker.task:
                    checker.task.cancel()

    @classmethod
    def pause_all(cls):
        """暂停所有异步Checker"""
        for checker in cls._checkers.values():
            checker.pause()

    @classmethod
    def resume_all(cls):
        """恢复所有异步Checker"""
        for checker in cls._checkers.values():
            checker.resume()

    @classmethod
    def get_checkers_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._checkers.values()]

    @classmethod
    def get_all_latest(cls) -> dict[str, dict]:
        return dict(cls._latest)

    @classmethod
    def get_count(cls) -> int:
        return len(cls._checkers)
