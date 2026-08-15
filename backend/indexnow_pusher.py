"""IndexNow 推送模块 - 主动向搜索引擎推送站点 URL，加速收录

IndexNow 协议支持 Bing、Yandex、Seznam 等搜索引擎。
通过定期推送项目 URL，提升搜索引擎抓取效率。
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger("health_checker")


class IndexNowPusher:
    """IndexNow 推送器 - 管理 URL 提交到 IndexNow 协议搜索引擎"""

    # 支持的搜索引擎端点
    SEARCH_ENGINES = {
        "bing": "https://www.bing.com/indexnow",
        "yandex": "https://yandex.com/indexnow",
        "seznam": "https://search.seznam.cz/indexnow",
        "indexnow": "https://api.indexnow.org/indexnow",
    }

    def __init__(self, projects: list[dict], key: str | None = None):
        self.projects = projects
        # 生成 IndexNow 验证密钥（若未指定则自动生成）
        self.key = key or uuid.uuid4().hex
        self.key_location = None  # 验证文件 URL 路径（运行时计算）

        self.interval_hours = 24  # 默认推送间隔（小时）
        self.running = False
        self._task = None
        self._stop_event = asyncio.Event()

        # 推送历史
        self._history: list[dict] = []
        self._max_history = 50

    # ========== 验证文件 ==========
    @property
    def key_filename(self) -> str:
        """IndexNow 验证文件名（密钥.txt）"""
        return f"{self.key}.txt"

    @property
    def key_file_content(self) -> str:
        """验证文件内容（即密钥本身）"""
        return self.key

    def get_verify_info(self) -> dict:
        """获取验证信息，供用户在网站根目录放置验证文件"""
        instructions = [
            f"1. 在网站根目录创建文件：{self.key_filename}",
            f"2. 文件内容为：{self.key}",
            "3. 确保通过 http(s)://你的域名/{key_filename} 可以访问到该文件",
            "4. 配置完成后即可开始推送 URL",
        ]
        last_push = None
        if self._history:
            last_push = self._history[-1].get("timestamp", "")
        return {
            "key": self.key,
            "api_key": self.key,
            "key_filename": self.key_filename,
            "key_content": self.key,
            "interval_hours": self.interval_hours,
            "last_push": last_push,
            "instructions": instructions,
            "supported_engines": list(self.SEARCH_ENGINES.keys()),
        }

    # ========== URL 收集 ==========
    def _collect_urls(self) -> list[str]:
        """从项目配置中收集需要推送的 URL"""
        urls = []
        for project in self.projects:
            base_url = project.get("url", "")
            if not base_url:
                continue
            # 项目首页
            urls.append(base_url.rstrip("/") + "/")

            # 如果配置了额外路径，也一起推送
            extra_paths = project.get("indexnow_paths", [])
            for path in extra_paths:
                urls.append(urljoin(base_url, path))

        # 去重
        return list(dict.fromkeys(urls))

    # ========== 推送逻辑 ==========
    async def push_urls(self, urls: list[str] | None = None, engine: str = "indexnow") -> dict:
        """推送 URL 到指定搜索引擎

        Args:
            urls: 要推送的 URL 列表，为 None 时自动收集项目 URL
            engine: 搜索引擎名称（bing/yandex/seznam/indexnow）

        Returns:
            推送结果字典
        """
        if urls is None:
            urls = self._collect_urls()

        if not urls:
            return {"success": False, "error": "没有可推送的 URL", "urls_count": 0}

        endpoint = self.SEARCH_ENGINES.get(engine)
        if not endpoint:
            return {
                "success": False,
                "error": f"不支持的搜索引擎: {engine}",
                "urls_count": len(urls),
            }

        # 计算 keyLocation（使用第一个 URL 的域名）
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
            "engine": engine,
            "endpoint": endpoint,
            "urls_count": len(urls),
            "urls": urls,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": False,
            "status_code": None,
            "error": None,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=payload)
                result["status_code"] = resp.status_code

                if resp.status_code == 200:
                    result["success"] = True
                    logger.info(
                        f"[IndexNow] 成功推送 {len(urls)} 个 URL 到 {engine} "
                        f"(HTTP {resp.status_code})"
                    )
                elif resp.status_code == 202:
                    result["success"] = True
                    result["note"] = "已接受，稍后处理"
                    logger.info(
                        f"[IndexNow] 推送已接受（{engine}），{len(urls)} 个 URL "
                        f"(HTTP {resp.status_code})"
                    )
                else:
                    result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning(
                        f"[IndexNow] 推送到 {engine} 失败: HTTP {resp.status_code} - {resp.text[:200]}"
                    )
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[IndexNow] 推送到 {engine} 异常: {e}")

        # 记录历史
        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        return result

    async def push_all_engines(self, urls: list[str] | None = None) -> list[dict]:
        """推送到所有支持的搜索引擎"""
        results = []
        for engine in self.SEARCH_ENGINES:
            result = await self.push_urls(urls, engine)
            results.append(result)
            # 引擎间轻微延迟，避免请求过于集中
            await asyncio.sleep(1)
        return results

    # ========== 定时推送 ==========
    async def _periodic_push_loop(self):
        """定时推送循环"""
        logger.info(
            f"[IndexNow] 定时推送任务启动，间隔 {self.interval_hours} 小时，"
            f"监控 {len(self.projects)} 个项目"
        )
        while self.running and not self._stop_event.is_set():
            try:
                # 等待间隔时间（可被中断）
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.interval_hours * 3600,
                    )
                except asyncio.TimeoutError:
                    pass

                if not self.running or self._stop_event.is_set():
                    break

                # 执行推送
                logger.info("[IndexNow] 开始定时推送...")
                results = await self.push_all_engines()
                success_count = sum(1 for r in results if r["success"])
                logger.info(
                    f"[IndexNow] 定时推送完成：{success_count}/{len(results)} 个引擎成功"
                )

            except Exception as e:
                logger.error(f"[IndexNow] 定时推送循环异常: {e}")
                # 异常后等待一小段时间再继续
                if not self._stop_event.is_set():
                    await asyncio.sleep(60)

        logger.info("[IndexNow] 定时推送任务已停止")

    def start(self):
        """启动定时推送"""
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._periodic_push_loop())

    async def stop(self):
        """停止定时推送"""
        self.running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def set_interval(self, hours: int) -> dict:
        """设置推送间隔（小时）"""
        hours = max(1, min(168, hours))  # 1小时到1周
        old_interval = self.interval_hours
        self.interval_hours = hours

        # 如果正在运行，通过重启task使新间隔生效（简单方案）
        was_running = self.running
        if was_running:
            self._stop_event.set()  # 唤醒当前等待

        logger.info(f"[IndexNow] 推送间隔已更新：{old_interval}h → {hours}h")
        return {
            "old_interval_hours": old_interval,
            "new_interval_hours": hours,
        }

    # ========== 历史记录 ==========
    def get_history(self, limit: int = 20) -> list[dict]:
        """获取推送历史（前端友好格式）"""
        limit = max(1, min(self._max_history, limit))
        records = []
        for r in reversed(self._history[-limit:]):
            records.append({
                "domain": r.get("engine", "unknown"),
                "url_count": r.get("urls_count", 0),
                "status": "success" if r.get("success") else "failed",
                "time": r.get("timestamp", "")[:19].replace("T", " "),
            })
        return records

    def get_status(self) -> dict:
        """获取当前状态"""
        return {
            "running": self.running,
            "interval_hours": self.interval_hours,
            "projects_count": len(self.projects),
            "urls_to_push": len(self._collect_urls()),
            "key": self.key,
            "key_filename": self.key_filename,
            "history_count": len(self._history),
            "supported_engines": list(self.SEARCH_ENGINES.keys()),
        }
