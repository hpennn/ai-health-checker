"""异步 Checker - 搜索引擎关键词检测，可在 builtin 或本地节点运行"""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx

from config import REQUEST_TIMEOUT, SLOW_THRESHOLD, load_projects

logger = logging.getLogger("health_checker")

ASYNC_RESULT_MAX_PER_PROJECT = 100

# 搜索 UA 池
ASYNC_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.119 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]


def _build_search_url(engine: str, keyword: str) -> str:
    if engine == "baidu":
        return f"https://www.baidu.com/s?wd={quote_plus(keyword)}"
    elif engine == "bing":
        return f"https://www.bing.com/search?q={quote_plus(keyword)}"
    else:
        return f"https://www.google.com/search?q={quote_plus(keyword)}"


def _extract_links(html: str, engine: str, target_domain: str) -> list[dict]:
    """从搜索结果页提取链接"""
    results = []
    if engine == "baidu":
        # 百度结果链接在 <a href="..."> 中，通常是百度跳转链接
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]
            if title and 'baidu.com' not in url:
                results.append({"url": url, "title": title})
    elif engine == "bing":
        for m in re.finditer(r'<h2><a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a></h2>', html, re.I | re.S):
            url = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]
            results.append({"url": url, "title": title})
    else:
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()[:100]
            if title and 'google' not in url and 'gstatic' not in url:
                results.append({"url": url, "title": title})
    return results[:20]


def _check_seo_meta(html: str) -> dict:
    return {
        "has_title": bool(re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)),
        "has_description": bool(re.search(r'<meta[^>]+name=["\']description["\']', html, re.I)),
        "has_viewport": bool(re.search(r'<meta[^>]+name=["\']viewport["\']', html, re.I)),
        "has_og_tags": bool(re.search(r'<meta[^>]+property=["\']og:', html, re.I)),
    }


class AsyncChecker:
    """异步搜索引擎检测 Checker"""

    def __init__(self, checker_id: str, name: str, projects: list[dict],
                 search_engine: str = "baidu",
                 keywords: dict | None = None,
                 interval_min: int = 30, interval_max: int = 60):
        self.id = checker_id
        self.name = name
        self.projects = projects
        self.search_engine = search_engine
        self.keywords = keywords or {}
        self.interval_min = interval_min * 60
        self.interval_max = interval_max * 60
        self.running = False
        self.paused = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self.check_count = 0
        self.failed_count = 0
        self.last_check_time: Optional[str] = None
        self.current_task = "空闲"
        self.user_agent = random.choice(ASYNC_USER_AGENTS)

    def _build_headers(self, referer: str = "") -> dict:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if referer:
            h["Referer"] = referer
        return h

    async def check_project(self, project: dict) -> dict:
        result = {
            "checker_id": self.id,
            "checker_name": self.name,
            "checker_type": "async",
            "project_name": project["name"],
            "project_url": project["url"],
            "name": project["name"],
            "url": project["url"],
            "category": project.get("category", ""),
            "search_engine": self.search_engine,
            "status": "unknown",
            "keyword": "",
            "matched_target": False,
            "search_status_code": None,
            "visit_status_code": None,
            "response_time_ms": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "seo_check": None,
            "error": None,
        }
        self.current_task = f"搜索检测 {project['name']}"
        try:
            keywords = self.keywords.get(project["name"], [project["name"]])
            keyword = random.choice(keywords)
            result["keyword"] = keyword
            search_url = _build_search_url(self.search_engine, keyword)
            target_domain = urlparse(project["url"]).netloc

            # 搜索
            async with httpx.AsyncClient(
                headers=self._build_headers(), timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            ) as client:
                resp = await client.get(search_url)
                result["search_status_code"] = resp.status_code
                if resp.status_code != 200:
                    result["status"] = "offline"
                    result["error"] = f"搜索引擎返回 {resp.status_code}"
                    self.failed_count += 1
                    return result

                links = _extract_links(resp.text, self.search_engine, target_domain)
                # 查找目标站点
                click_url = None
                for link in links:
                    if target_domain in link["url"]:
                        click_url = link["url"]
                        result["matched_target"] = True
                        break
                if not click_url and links:
                    click_url = links[0]["url"]

                # 访问
                if click_url:
                    visit_start = time.time()
                    try:
                        visit_resp = await client.get(
                            click_url, headers=self._build_headers(referer=search_url),
                        )
                        result["visit_status_code"] = visit_resp.status_code
                        result["response_time_ms"] = round((time.time() - visit_start) * 1000, 2)
                        if visit_resp.status_code == 200:
                            html = visit_resp.text
                            result["seo_check"] = _check_seo_meta(html)
                            result["status"] = "online"
                        else:
                            result["status"] = "offline"
                            self.failed_count += 1
                    except Exception as ve:
                        result["status"] = "offline"
                        result["error"] = f"访问失败: {str(ve)[:80]}"
                        self.failed_count += 1
                else:
                    # 没找到链接，直接访问首页
                    homepage = project["url"].rstrip("/") + "/"
                    visit_start = time.time()
                    visit_resp = await client.get(homepage, headers=self._build_headers())
                    result["visit_status_code"] = visit_resp.status_code
                    result["response_time_ms"] = round((time.time() - visit_start) * 1000, 2)
                    if visit_resp.status_code == 200:
                        result["seo_check"] = _check_seo_meta(visit_resp.text)
                        result["status"] = "online"
                    else:
                        result["status"] = "offline"
                        self.failed_count += 1

                if result["status"] == "online" and result["response_time_ms"] and \
                   result["response_time_ms"] > SLOW_THRESHOLD * 1000:
                    result["status"] = "slow"
        except Exception as e:
            result["status"] = "offline"
            result["error"] = f"搜索检测异常: {str(e)[:80]}"
            self.failed_count += 1
            logger.error(f"[AsyncChecker-{self.id}] {project['name']} 异常: {e}")

        self.check_count += 1
        self.last_check_time = result["timestamp"]
        self.current_task = "空闲"
        return result

    async def run_loop(self):
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()
        logger.info(f"[AsyncChecker-{self.id}] {self.name} 启动")
        while self.running:
            try:
                await self._pause_event.wait()
                if not self.running:
                    break
                all_projects = load_projects()
                if self.projects:
                    target_names = {p["name"] for p in self.projects}
                    check_list = [p for p in all_projects if p["name"] in target_names]
                else:
                    check_list = all_projects
                for project in check_list:
                    if not self.running or not self._pause_event.is_set():
                        break
                    try:
                        result = await self.check_project(project)
                        from checker import CheckerManager
                        await CheckerManager.save_async_result(result)
                    except Exception as e:
                        logger.error(f"[AsyncChecker-{self.id}] 检测 {project['name']} 异常: {e}")
                    await asyncio.sleep(random.uniform(2, 5))
                if self.running and self._pause_event.is_set():
                    wait = random.uniform(self.interval_min, self.interval_max)
                    await self._sleep_interruptible(wait)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AsyncChecker-{self.id}] 主循环异常: {e}")
                await asyncio.sleep(10)
        self.running = False
        logger.info(f"[AsyncChecker-{self.id}] {self.name} 已停止")

    async def _sleep_interruptible(self, seconds: float):
        elapsed = 0
        while elapsed < seconds and self.running and self._pause_event.is_set():
            await asyncio.sleep(min(1.0, seconds - elapsed))
            elapsed += 1.0

    def start(self):
        if not self.running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.running = False
        self._stop_event.set()
        self._pause_event.set()
        if self._task:
            self._task.cancel()

    def pause(self):
        self._pause_event.clear()
        self.paused = True

    def resume(self):
        self._pause_event.set()
        self.paused = False

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": "async",
            "search_engine": self.search_engine,
            "running": self.running,
            "paused": self.paused,
            "check_count": self.check_count,
            "failed_count": self.failed_count,
            "current_task": self.current_task,
            "last_check_time": self.last_check_time,
            "project_count": len(self.projects),
        }


class AsyncCheckerManager:
    """异步 Checker 管理器 - 管理 builtin 节点的 async checker 实例"""

    _checkers: dict[str, AsyncChecker] = {}

    @classmethod
    async def create_checker(cls, checker_config: dict) -> AsyncChecker:
        cid = checker_config["id"]
        projects_cfg = checker_config.get("projects", [])
        all_projects = load_projects()
        if projects_cfg:
            projects = [p for p in all_projects if p["name"] in projects_cfg]
        else:
            projects = list(all_projects)
        cfg = checker_config.get("config", {})
        checker = AsyncChecker(
            checker_id=cid,
            name=checker_config.get("name", cid),
            projects=projects,
            search_engine=cfg.get("search_engine", "baidu"),
            keywords=cfg.get("keywords", {}),
            interval_min=checker_config.get("interval_min", 30),
            interval_max=checker_config.get("interval_max", 60),
        )
        cls._checkers[cid] = checker
        return checker

    @classmethod
    async def remove_checker(cls, checker_id: str):
        checker = cls._checkers.pop(checker_id, None)
        if checker:
            checker.stop()

    @classmethod
    def get_checker(cls, checker_id: str) -> AsyncChecker | None:
        return cls._checkers.get(checker_id)

    @classmethod
    def start_all(cls):
        for c in cls._checkers.values():
            c.start()

    @classmethod
    def stop_all(cls):
        for c in cls._checkers.values():
            c.stop()

    @classmethod
    def pause_all(cls):
        for c in cls._checkers.values():
            c.pause()

    @classmethod
    def resume_all(cls):
        for c in cls._checkers.values():
            c.resume()

    @classmethod
    def get_checkers_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._checkers.values()]

    @classmethod
    def get_count(cls) -> int:
        return len(cls._checkers)

    @classmethod
    def get_running_count(cls) -> int:
        return sum(1 for c in cls._checkers.values() if c.running and not c.paused)
