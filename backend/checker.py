"""同步 Checker - HTTP 深度健康检测，在服务器 builtin 节点运行"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import (
    REQUEST_TIMEOUT, SLOW_THRESHOLD, HISTORY_MAX_SIZE,
    RESULTS_FILE, load_projects, _load_json, _save_json,
)
from deep_inspector import DeepInspector

logger = logging.getLogger("health_checker")


class Checker:
    """单个同步 HTTP 检查器"""

    def __init__(self, checker_id: str, name: str, projects: list[dict],
                 user_agent: str = "", deep_inspect: bool = True,
                 interval_min: int = 5, interval_max: int = 15):
        self.id = checker_id
        self.name = name
        self.projects = projects
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        self.deep_inspect = deep_inspect
        self.interval_min = interval_min * 60
        self.interval_max = interval_max * 60
        self.running = False
        self.paused = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._check_now_event = asyncio.Event()
        self.check_count = 0
        self.failed_count = 0
        self.last_check_time: Optional[str] = None
        self.current_task = "空闲"

    def trigger_now(self):
        """触发立即检查"""
        self._check_now_event.set()

    def _build_headers(self) -> dict:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }

    async def check_project(self, project: dict) -> dict:
        """检查单个项目"""
        result = {
            "checker_id": self.id,
            "checker_name": self.name,
            "checker_type": "sync",
            "project_name": project["name"],
            "project_url": project["url"],
            "category": project.get("category", ""),
            "name": project["name"],
            "url": project["url"],
            "status": "unknown",
            "status_code": None,
            "response_time_ms": None,
            "content_length": 0,
            "title": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "deep_inspect": None,
            "error": None,
        }
        self.current_task = f"检查 {project['name']}"
        start = time.time()
        try:
            headers = self._build_headers()
            if self.deep_inspect:
                deep = await DeepInspector.inspect(project["url"], headers)
                result["status_code"] = deep.get("status_code")
                result["response_time_ms"] = deep.get("response_time_ms")
                result["content_length"] = deep.get("content_length", 0)
                result["title"] = deep.get("title", "")
                result["status"] = deep.get("status", "unknown")
                result["error"] = deep.get("error")
                result["deep_inspect"] = {
                    "has_title": deep.get("has_title"),
                    "has_meta_description": deep.get("has_meta_description"),
                    "has_meta_viewport": deep.get("has_meta_viewport"),
                    "has_og_tags": deep.get("has_og_tags"),
                    "body_has_content": deep.get("body_has_content"),
                    "is_spa_shell": deep.get("is_spa_shell"),
                    "resources": deep.get("resources"),
                    "resource_check": deep.get("resource_check"),
                    "ssl_check": deep.get("ssl_check"),
                }
            else:
                async with httpx.AsyncClient(
                    headers=headers, timeout=REQUEST_TIMEOUT,
                    follow_redirects=True, verify=True,
                ) as client:
                    resp = await client.get(project["url"])
                    elapsed = round((time.time() - start) * 1000, 2)
                    result["status_code"] = resp.status_code
                    result["response_time_ms"] = elapsed
                    result["content_length"] = len(resp.text)
                    if resp.status_code == 200:
                        result["status"] = "slow" if elapsed > SLOW_THRESHOLD * 1000 else "online"
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

        self.check_count += 1
        if result["status"] == "offline":
            self.failed_count += 1
        self.last_check_time = result["timestamp"]
        self.current_task = "空闲"
        return result

    async def run_loop(self):
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()
        logger.info(f"[Checker-{self.id}] {self.name} 启动")
        while self.running:
            try:
                await self._pause_event.wait()
                if not self.running:
                    break
                # 重新加载项目（支持动态更新）
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
                        await CheckerManager.save_result(result)
                    except Exception as e:
                        logger.error(f"[Checker-{self.id}] 检查 {project['name']} 异常: {e}")
                    # 项目间短暂间隔
                    if self.running and self._pause_event.is_set():
                        await asyncio.sleep(random.uniform(1, 3))
                # 等待下一轮（可被 trigger_now 中断）
                if self.running and self._pause_event.is_set():
                    wait = random.uniform(self.interval_min, self.interval_max)
                    try:
                        await asyncio.wait_for(
                            self._check_now_event.wait(),
                            timeout=wait
                        )
                        self._check_now_event.clear()
                        logger.info(f"[Checker-{self.id}] 收到立即检查信号")
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Checker-{self.id}] 主循环异常: {e}")
                await asyncio.sleep(10)
        self.running = False
        logger.info(f"[Checker-{self.id}] {self.name} 已停止")

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
            "type": "sync",
            "running": self.running,
            "paused": self.paused,
            "check_count": self.check_count,
            "failed_count": self.failed_count,
            "current_task": self.current_task,
            "last_check_time": self.last_check_time,
            "project_count": len(self.projects),
        }


class CheckerManager:
    """同步 Checker 管理器 - 管理 builtin 节点的 sync checker 实例"""

    _checkers: dict[str, Checker] = {}
    _results: dict[str, list[dict]] = {}
    _latest: dict[str, dict] = {}
    _async_results: dict[str, list[dict]] = {}
    _async_latest: dict[str, dict] = {}
    _visit_latest: dict[str, dict] = {}
    _local_visits: list[dict] = []
    _ws_clients: list = []
    _recent_logs: list[dict] = []

    @classmethod
    async def initialize(cls):
        """初始化 - 由 CheckerDispatcher 调用，根据配置创建 sync checker"""
        pass  # 实际创建由 CheckerDispatcher 负责

    @classmethod
    async def create_checker(cls, checker_config: dict) -> Checker:
        """根据配置创建一个 sync checker 实例"""
        cid = checker_config["id"]
        projects_cfg = checker_config.get("projects", [])
        all_projects = load_projects()
        if projects_cfg:
            projects = [p for p in all_projects if p["name"] in projects_cfg]
        else:
            projects = list(all_projects)
        cfg = checker_config.get("config", {})
        checker = Checker(
            checker_id=cid,
            name=checker_config.get("name", cid),
            projects=projects,
            user_agent=cfg.get("user_agent", ""),
            deep_inspect=cfg.get("deep_inspect", True),
            interval_min=checker_config.get("interval_min", 5),
            interval_max=checker_config.get("interval_max", 15),
        )
        cls._checkers[cid] = checker
        return checker

    @classmethod
    async def remove_checker(cls, checker_id: str):
        checker = cls._checkers.pop(checker_id, None)
        if checker:
            checker.stop()

    @classmethod
    def get_checker(cls, checker_id: str) -> Checker | None:
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
    def trigger_now(cls, checker_id: str) -> bool:
        c = cls._checkers.get(checker_id)
        if c and c.running and not c.paused:
            c.trigger_now()
            return True
        return False

    @classmethod
    def get_checkers_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._checkers.values()]

    # ========== 结果存储 ==========
    @classmethod
    async def save_result(cls, result: dict):
        pname = result["project_name"]
        cls._latest[pname] = result
        cls._results.setdefault(pname, []).append(result)
        if len(cls._results[pname]) > HISTORY_MAX_SIZE:
            cls._results[pname] = cls._results[pname][-HISTORY_MAX_SIZE:]
        # 持久化最新结果
        try:
            _save_json(RESULTS_FILE, cls._latest)
        except Exception:
            pass
        await cls.ws_broadcast_result(result)

    @classmethod
    async def save_async_result(cls, result: dict):
        pname = result.get("project_name", "")
        cls._async_latest[pname] = result
        cls._async_results.setdefault(pname, []).append(result)
        if len(cls._async_results[pname]) > HISTORY_MAX_SIZE:
            cls._async_results[pname] = cls._async_results[pname][-HISTORY_MAX_SIZE:]
        await cls.ws_broadcast_result(result)

    @classmethod
    async def save_local_visit_result(cls, result: dict):
        pname = result.get("project_name", "")
        cls._visit_latest[pname] = result
        cls._local_visits.append(result)
        if len(cls._local_visits) > 500:
            cls._local_visits = cls._local_visits[-500:]

    @classmethod
    def get_all_status(cls) -> dict:
        return dict(cls._latest)

    @classmethod
    def get_async_status(cls) -> dict:
        return dict(cls._async_latest)

    @classmethod
    def get_visit_all_latest(cls) -> dict:
        return dict(cls._visit_latest)

    @classmethod
    def get_project_history(cls, project_name: str) -> list[dict]:
        return cls._results.get(project_name, [])[-50:]

    @classmethod
    def get_all_history(cls) -> list[dict]:
        all_records = []
        for records in cls._results.values():
            all_records.extend(records)
        all_records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return all_records[:200]

    @classmethod
    def get_all_latest(cls) -> dict:
        return dict(cls._latest)

    @classmethod
    def get_summary(cls) -> dict:
        total = len(cls._latest)
        online = sum(1 for r in cls._latest.values() if r.get("status") == "online")
        offline = sum(1 for r in cls._latest.values() if r.get("status") == "offline")
        slow = sum(1 for r in cls._latest.values() if r.get("status") == "slow")
        return {"total": total, "online": online, "offline": offline, "slow": slow}

    @classmethod
    async def check_project_now(cls, project_name: str) -> dict | None:
        project = None
        for p in load_projects():
            if p["name"] == project_name:
                project = p
                break
        if not project:
            return None
        # 用第一个可用 checker
        for checker in cls._checkers.values():
            result = await checker.check_project(project)
            await cls.save_result(result)
            return result
        return None

    @classmethod
    async def check_all_now(cls) -> list[dict]:
        results = []
        projects = load_projects()
        for project in projects:
            for checker in cls._checkers.values():
                result = await checker.check_project(project)
                await cls.save_result(result)
                results.append(result)
                break
        return results

    @classmethod
    def get_inspection_stats(cls) -> dict:
        total_checks = sum(c.check_count for c in cls._checkers.values())
        total_failed = sum(c.failed_count for c in cls._checkers.values())
        return {
            "total_checks": total_checks,
            "total_failed": total_failed,
            "success_rate": round((total_checks - total_failed) / total_checks * 100, 1) if total_checks else 0,
            "checker_count": len(cls._checkers),
        }

    @classmethod
    def get_checker_workload(cls) -> list[dict]:
        return [
            {
                "id": c.id, "name": c.name,
                "check_count": c.check_count, "failed_count": c.failed_count,
                "running": c.running, "paused": c.paused,
                "current_task": c.current_task,
            }
            for c in cls._checkers.values()
        ]

    @classmethod
    def get_health_info(cls) -> dict:
        try:
            import psutil
            mem = psutil.virtual_memory()
            return {
                "status": "ok",
                "service": "ai-health-checker",
                "uptime_seconds": int(time.time() - psutil.boot_time()),
                "memory": {
                    "total": mem.total, "used": mem.used,
                    "percent": mem.percent,
                },
                "cpu_percent": psutil.cpu_percent(interval=0.1),
            }
        except ImportError:
            return {"status": "ok"}

    @classmethod
    def get_local_visit_stats(cls) -> dict:
        total = len(cls._local_visits)
        success = sum(1 for v in cls._local_visits if v.get("success"))
        return {
            "total_visits": total,
            "success_visits": success,
            "failed_visits": total - success,
            "recent": cls._local_visits[-20:],
        }

    # ========== WebSocket ==========
    @classmethod
    async def add_ws_client(cls, ws):
        cls._ws_clients.append(ws)

    @classmethod
    async def remove_ws_client(cls, ws):
        if ws in cls._ws_clients:
            cls._ws_clients.remove(ws)

    @classmethod
    async def ws_broadcast(cls, message: dict):
        dead = []
        for ws in cls._ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in cls._ws_clients:
                cls._ws_clients.remove(ws)

    @classmethod
    async def ws_broadcast_result(cls, result: dict):
        await cls.ws_broadcast({"type": "check_result", "result": result})

    @classmethod
    async def ws_broadcast_control(cls, action: str):
        await cls.ws_broadcast({"type": "control", "action": action})

    @classmethod
    async def ws_broadcast_config(cls, config: dict):
        await cls.ws_broadcast({"type": "config_update", "config": config})

    @classmethod
    def add_log(cls, message: str, level: str = "info"):
        cls._recent_logs.append({
            "message": message, "level": level,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(cls._recent_logs) > 200:
            cls._recent_logs = cls._recent_logs[-200:]

    @classmethod
    def get_recent_logs(cls) -> list[dict]:
        return list(cls._recent_logs)


# ========== 视频访问检查器（简化版，保留功能） ==========
class VideoCheckerManager:
    _sync_running = False
    _async_running = False
    _results: dict[str, dict] = {}
    _sync_agents: list[dict] = []
    _async_agents: list[dict] = []

    @classmethod
    async def initialize(cls):
        pass

    @classmethod
    def get_all_status(cls) -> dict:
        return dict(cls._results)

    @classmethod
    def is_sync_running(cls) -> bool:
        return cls._sync_running

    @classmethod
    def is_async_running(cls) -> bool:
        return cls._async_running

    @classmethod
    async def start_sync(cls):
        cls._sync_running = True

    @classmethod
    async def stop_sync(cls):
        cls._sync_running = False

    @classmethod
    async def start_async(cls):
        cls._async_running = True

    @classmethod
    async def stop_async(cls):
        cls._async_running = False

    @classmethod
    def get_all_agents(cls) -> dict:
        return {"sync": cls._sync_agents, "async": cls._async_agents}
