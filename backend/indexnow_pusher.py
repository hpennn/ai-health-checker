"""IndexNow 推送模块 - 主动向搜索引擎推送站点 URL，加速收录"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger("health_checker")


class IndexNowPusher:
    """IndexNow 推送器"""

    SEARCH_ENGINES = {
        "bing": "https://www.bing.com/indexnow",
        "yandex": "https://yandex.com/indexnow",
        "seznam": "https://search.seznam.cz/indexnow",
        "indexnow": "https://api.indexnow.org/indexnow",
    }

    def __init__(self, projects: list[dict], key: str | None = None):
        # projects 可为 list（初始快照）或 callable（动态获取）
        self._projects = projects
        self.key = key or uuid.uuid4().hex
        self.key_location = None
        self.interval_hours = 24
        self.running = False
        self._task = None
        self._stop_event = asyncio.Event()
        self._history: list[dict] = []
        self._max_history = 50

    @property
    def projects(self) -> list[dict]:
        """动态获取项目列表（支持项目增删后自动同步）"""
        if callable(self._projects):
            try:
                return self._projects()
            except Exception:
                return []
        return self._projects or []

    @property
    def key_filename(self) -> str:
        return f"{self.key}.txt"

    @property
    def key_file_content(self) -> str:
        return self.key

    def get_verify_info(self) -> dict:
        instructions = [
            f"1. 在网站根目录创建文件：{self.key_filename}",
            f"2. 文件内容为：{self.key}",
            f"3. 确保通过 http(s)://你的域名/{self.key_filename} 可以访问",
            "4. 配置完成后即可开始推送 URL",
        ]
        last_push = self._history[-1].get("timestamp", "") if self._history else None
        return {
            "key": self.key,
            "key_filename": self.key_filename,
            "key_content": self.key,
            "interval_hours": self.interval_hours,
            "last_push": last_push,
            "instructions": instructions,
            "supported_engines": list(self.SEARCH_ENGINES.keys()),
        }

    def _collect_urls(self) -> list[str]:
        urls = []
        for project in self.projects:
            base_url = project.get("url", "")
            if not base_url or project.get("skip_indexnow"):
                continue
            urls.append(base_url.rstrip("/") + "/")
            for path in project.get("indexnow_paths", []):
                urls.append(urljoin(base_url, path))
        return list(dict.fromkeys(urls))

    async def push_urls(self, urls: list[str] | None = None, engine: str = "indexnow") -> dict:
        if urls is None:
            urls = self._collect_urls()
        if not urls:
            return {"success": False, "error": "没有可推送的 URL", "urls_count": 0}
        endpoint = self.SEARCH_ENGINES.get(engine)
        if not endpoint:
            return {"success": False, "error": f"不支持的搜索引擎: {engine}", "urls_count": len(urls)}

        if urls:
            parsed = urlparse(urls[0])
            self.key_location = f"{parsed.scheme}://{parsed.netloc}/{self.key_filename}"

        payload = {
            "host": urlparse(urls[0]).netloc,
            "key": self.key,
            "keyLocation": self.key_location,
            "urlList": urls,
        }
        result = {
            "engine": engine, "endpoint": endpoint,
            "urls_count": len(urls), "urls": urls,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": False, "status_code": None, "error": None,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=payload)
                result["status_code"] = resp.status_code
                if resp.status_code in (200, 202):
                    result["success"] = True
                    logger.info(f"[IndexNow] 成功推送 {len(urls)} URL 到 {engine}")
                else:
                    result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[IndexNow] 推送到 {engine} 异常: {e}")

        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        return result

    async def push_all_engines(self, urls: list[str] | None = None) -> list[dict]:
        results = []
        for engine in self.SEARCH_ENGINES:
            results.append(await self.push_urls(urls, engine))
            await asyncio.sleep(1)
        return results

    async def _periodic_push_loop(self):
        logger.info(f"[IndexNow] 定时推送启动，间隔 {self.interval_hours}h")
        while self.running and not self._stop_event.is_set():
            try:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_hours * 3600)
                except asyncio.TimeoutError:
                    pass
                if not self.running or self._stop_event.is_set():
                    break
                await self.push_all_engines()
            except Exception as e:
                logger.error(f"[IndexNow] 定时推送异常: {e}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(60)

    def start(self):
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._periodic_push_loop())

    async def stop(self):
        self.running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def set_interval(self, hours: int) -> dict:
        hours = max(1, min(168, hours))
        old = self.interval_hours
        self.interval_hours = hours
        # 重启定时循环以应用新间隔
        if self.running:
            self.running = False
            self._stop_event.set()
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
            self.start()
        return {"old_interval_hours": old, "new_interval_hours": hours}

    def get_history(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(self._max_history, limit))
        return [
            {"domain": r.get("engine", "?"), "url_count": r.get("urls_count", 0),
             "status": "success" if r.get("success") else "failed",
             "time": r.get("timestamp", "")[:19].replace("T", " ")}
            for r in reversed(self._history[-limit:])
        ]
