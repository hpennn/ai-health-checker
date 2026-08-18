"""Checker 引擎 - 10个异步 Checker 子 agent 核心模块"""
import asyncio
import json
import os
import ssl
import time
import random
import re
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from browser_checker import get_browser_checker
from config import (
    CHECKER_IDENTITIES,
    PROJECTS,
    REQUEST_TIMEOUT,
    SLOW_THRESHOLD,
    HISTORY_MAX_SIZE,
    RESULTS_FILE,
    DATA_DIR,
    get_random_ip,
    assign_projects_to_checkers,
)

# ========== 日志配置 ==========
LOG_FILE = os.path.join(DATA_DIR, "checker.log")

def _setup_logger():
    """设置文件日志"""
    os.makedirs(DATA_DIR, exist_ok=True)
    logger = logging.getLogger("health_checker")
    logger.setLevel(logging.INFO)
    # 避免重复添加handler
    if logger.handlers:
        return logger
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    # 同时输出到控制台
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

logger = _setup_logger()


class Checker:
    """单个 Checker agent - 模拟真实用户访问"""

    def __init__(self, identity: dict, projects: list[dict]):
        self.id = identity["id"]
        self.name = identity["name"]
        self.user_agent = identity["user_agent"]
        self.ip_pool = identity["ip_pool"]
        self.type = identity["type"]
        self.projects = projects  # 负责检测的项目列表

        self.running = False
        self.task = None
        self.check_count = 0
        self.browser_check_count = 0  # 浏览器深度检查次数
        self._browser_round_counter = 0  # 用于计算浏览器检查间隔
        self.current_task = "空闲"
        self.last_check_time = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()  # set=运行，clear=暂停
        self._pause_event.set()

        # 延迟导入 RuntimeConfig
        self._config = None

    def _get_config(self):
        if self._config is None:
            from config import RuntimeConfig
            self._config = RuntimeConfig.get_instance()
        return self._config

    def _build_headers(self) -> dict:
        """构建请求头，模拟真实浏览器"""
        ip = get_random_ip(self.ip_pool)
        return {
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
            "X-Originating-IP": ip,
        }

    async def _check_ssl(self, url: str) -> dict:
        """检查 SSL 证书状态（仅 HTTPS）"""
        if not url.startswith("https://"):
            return {"ssl_valid": None, "ssl_expiry": None, "ssl_error": None}

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

            not_after_str = cert.get("notAfter", "")
            expiry = None
            if not_after_str:
                try:
                    expiry = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            return {
                "ssl_valid": True,
                "ssl_expiry": expiry.isoformat() if expiry else None,
                "ssl_error": None,
            }
        except Exception as e:
            return {
                "ssl_valid": False,
                "ssl_expiry": None,
                "ssl_error": str(e)[:100],
            }

    async def _check_api_endpoint(self, url: str, headers: dict) -> dict | None:
        """检测 /api 路径是否返回正常 JSON"""
        api_url = url.rstrip("/") + "/api"
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as client:
                resp = await client.get(api_url)
                try:
                    data = resp.json()
                    return {
                        "api_ok": True,
                        "api_status": resp.status_code,
                        "api_has_json": True,
                        "api_sample_keys": list(data.keys())[:5] if isinstance(data, dict) else [],
                    }
                except (json.JSONDecodeError, ValueError):
                    return {
                        "api_ok": True,
                        "api_status": resp.status_code,
                        "api_has_json": False,
                        "api_sample_keys": [],
                    }
        except Exception:
            return None

    async def check_project(self, project: dict) -> dict:
        """检测单个项目，返回完整结果"""
        self.current_task = f"检测: {project['name']}"
        url = project["url"]
        result = {
            "project_name": project["name"],
            "project_url": url,
            "category": project["category"],
            "checker_id": self.id,
            "checker_name": self.name,
            "checker_type": self.type,
            "source_ip": get_random_ip(self.ip_pool),
            "status": "unknown",  # online / offline / slow
            "status_code": None,
            "response_time_ms": None,
            "content_check": {},
            "ssl_check": {},
            "api_check": {},
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        headers = self._build_headers()
        start_time = time.time()

        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as client:
                resp = await client.get(url)
                elapsed = time.time() - start_time
                response_time_ms = round(elapsed * 1000, 2)

                result["status_code"] = resp.status_code
                result["response_time_ms"] = response_time_ms

                if resp.status_code == 200:
                    if response_time_ms > SLOW_THRESHOLD * 1000:
                        result["status"] = "slow"
                    else:
                        result["status"] = "online"
                else:
                    result["status"] = "offline"

                html = resp.text
                content_check = {
                    "has_title": bool(re.search(r"<title[^>]*>.*</title>", html, re.IGNORECASE | re.DOTALL)),
                    "has_script": bool(re.search(r"<script", html, re.IGNORECASE)),
                    "has_html_doctype": bool(re.search(r"<!DOCTYPE\s+html", html, re.IGNORECASE)),
                    "content_length": len(html),
                    "title_text": "",
                }
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if title_match:
                    content_check["title_text"] = title_match.group(1).strip()[:100]
                result["content_check"] = content_check

        except httpx.TimeoutException:
            elapsed = time.time() - start_time
            result["status"] = "offline"
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = "请求超时（超过10秒）"
            logger.warning(f"[Checker-{self.id}] {project['name']} 请求超时")
        except httpx.ConnectError as e:
            elapsed = time.time() - start_time
            result["status"] = "offline"
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"连接失败: {str(e)[:80]}"
            logger.warning(f"[Checker-{self.id}] {project['name']} 连接失败: {e}")
        except Exception as e:
            elapsed = time.time() - start_time
            result["status"] = "offline"
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"未知错误: {str(e)[:80]}"
            logger.error(f"[Checker-{self.id}] {project['name']} 检测异常: {e}")

        if url.startswith("https://"):
            result["ssl_check"] = await self._check_ssl(url)

        result["api_check"] = await self._check_api_endpoint(url, headers)

        # ===== 浏览器深度检查（第二层） =====
        # 注意：已迁移到独立浏览器巡检循环（browser_checker.run_browser_inspection_loop）
        # sync checker 仅保留 HTTP 检查，避免 Chromium 常驻内存导致 2GB 服务器崩溃
        # 以下代码保留用于向后兼容（当 BROWSER_CHECK_INTERVAL > 0 时仍可启用）
        self._browser_round_counter += 1
        should_browser_check = (
            CheckerManager.BROWSER_CHECK_INTERVAL > 0
            and self._browser_round_counter >= CheckerManager.BROWSER_CHECK_INTERVAL
        )

        if should_browser_check and result["status"] == "online":
            self._browser_round_counter = 0
            browser_result = await self._run_browser_check(project)
            result["browser_check"] = browser_result
            # 如果浏览器检查发现严重问题，降级状态
            if browser_result and not browser_result.get("browser_ok", True):
                if result["status"] == "online":
                    result["status"] = "slow"  # 降级为 slow 而非 offline
                    result["error"] = (result.get("error") or "") + \
                        f" [浏览器] {browser_result.get('error', '渲染异常')}"
        else:
            result["browser_check"] = None

        self.check_count += 1
        self.last_check_time = datetime.now(timezone.utc).isoformat()
        self.current_task = "空闲"

        # 状态变更时记录日志
        prev = CheckerManager._latest.get(project["name"], {})
        prev_status = prev.get("status")
        if prev_status and prev_status != result["status"]:
            logger.info(f"[状态变更] {project['name']}: {prev_status} → {result['status']} "
                        f"(响应时间: {result['response_time_ms']}ms)")

        return result

    async def _run_browser_check(self, project: dict) -> dict | None:
        """执行浏览器深度检查"""
        try:
            bc = get_browser_checker()
            if not bc.available:
                return None
            self.current_task = f"浏览器检测: {project['name']}"
            browser_result = await bc.check_project(project, take_screenshot=True)
            self.browser_check_count += 1
            logger.info(
                f"[Checker-{self.id}] {project['name']} 浏览器检查完成: "
                f"rendered={browser_result.get('page_rendered')}, "
                f"js_errors={browser_result.get('js_error_count', 0)}"
            )
            return browser_result
        except Exception as e:
            logger.warning(f"[Checker-{self.id}] {project['name']} 浏览器检查异常: {e}")
            return None

    async def browser_check_only(self, project: dict) -> dict:
        """仅执行浏览器检查（不走 HTTP 检查），用于手动触发"""
        self.current_task = f"浏览器检测: {project['name']}"
        try:
            bc = get_browser_checker()
            if not bc.available:
                await bc.initialize()
            if not bc.available:
                return {
                    "project_name": project["name"],
                    "project_url": project["url"],
                    "browser_ok": False,
                    "error": "Playwright 未安装或浏览器启动失败",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            result = await bc.check_project(project, take_screenshot=True)
            self.browser_check_count += 1
            return result
        except Exception as e:
            return {
                "project_name": project["name"],
                "project_url": project["url"],
                "browser_ok": False,
                "error": str(e)[:200],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        finally:
            self.current_task = "空闲"

    async def run_loop(self):
        """Checker 主循环 - 支持暂停/恢复和动态间隔"""
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()

        config = self._get_config()
        logger.info(f"[Checker-{self.id}] {self.name} 启动，负责 {len(self.projects)} 个项目")

        while self.running:
            # 等待暂停解除
            await self._pause_event.wait()
            if not self.running:
                break

            # 检查是否达到今日总巡检次数上限
            if not CheckerManager.can_run_inspection():
                # 今日已达上限，等待到明天再继续
                await self._sleep_interruptible(60)
                continue

            # 每轮巡检开始时，在区间内随机决定本轮检查次数
            rounds = config.get_random_rounds()
            rounds = max(1, min(10, rounds))

            # 记录本轮为一次完整巡检（仅在第一个checker上计数，避免重复）
            if self.id == 1:
                CheckerManager.increment_inspection_count()

            for project in self.projects:
                if not self.running:
                    break
                if not self._pause_event.is_set():
                    break

                for round_i in range(rounds):
                    if not self.running:
                        break
                    if not self._pause_event.is_set():
                        break

                    try:
                        result = await self.check_project(project)
                        await CheckerManager.save_result(result)
                    except Exception as e:
                        logger.error(f"[Checker-{self.id}] 检测 {project['name']} 异常: {e}")

                    # 轮内间隔
                    if round_i < rounds - 1:
                        await self._sleep_interruptible(config.rounds_interval_seconds)

                # 项目间短暂间隔
                if self.running and self._pause_event.is_set():
                    await self._sleep_interruptible(random.uniform(2, 5))

            # 等待下一轮巡检（使用配置的间隔）
            if self.running and self._pause_event.is_set():
                interval = config.get_interval_seconds()
                await self._sleep_interruptible(interval)

            # 如果没有项目，等待一下避免死循环
            if not self.projects and self.running and self._pause_event.is_set():
                await self._sleep_interruptible(60)

        logger.info(f"[Checker-{self.id}] {self.name} 已停止")
        self.running = False

    async def _sleep_interruptible(self, seconds: float):
        """可中断睡眠（被stop或pause时立即唤醒）"""
        elapsed = 0
        step = min(1.0, max(0.5, seconds / 30))
        while elapsed < seconds and self.running and self._pause_event.is_set():
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += step

    def stop(self):
        """停止 Checker"""
        self.running = False
        self._stop_event.set()
        self._pause_event.set()  # 解除暂停阻塞

    def pause(self):
        """暂停 Checker（保持进程）"""
        self._pause_event.clear()

    def resume(self):
        """恢复 Checker"""
        self._pause_event.set()

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    def get_status(self) -> dict:
        """获取 Checker 运行状态"""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "user_agent": self.user_agent,
            "ip_sample": self.ip_pool[0],
            "running": self.running,
            "paused": self.paused,
            "check_count": self.check_count,
            "browser_check_count": self.browser_check_count,
            "current_task": self.current_task,
            "last_check_time": self.last_check_time,
            "project_count": len(self.projects),
            "projects": [p["name"] for p in self.projects],
        }


class CheckerManager:
    """Checker 管理器 - 管理所有 Checker 和结果存储"""

    _checkers: dict[int, Checker] = {}
    _results: dict[str, list[dict]] = {}  # project_name -> [history]
    _latest: dict[str, dict] = {}  # project_name -> latest result
    _async_latest: dict[str, dict] = {}  # 异步Checker最新结果
    _lock = asyncio.Lock()
    _initialized = False
    _ws_clients: list = []  # WebSocket 客户端列表
    _start_time = None  # 服务启动时间

    # ===== 浏览器检查配置 =====
    # BROWSER_CHECK_INTERVAL = 3  # 旧：每 N 轮 HTTP 检查后执行 1 次浏览器检查
    BROWSER_CHECK_INTERVAL = 0  # 新：sync checker 不再做浏览器检查，交给独立浏览器巡检循环

    # ===== 浏览器检查历史 =====
    _browser_results: dict[str, list[dict]] = {}  # project_name -> [browser check history]
    _browser_latest: dict[str, dict] = {}  # project_name -> latest browser check result

    # ===== 总巡检次数追踪 =====
    _inspection_count = 0  # 今日已完成的完整巡检轮数
    _inspection_count_date = None  # 当前计数对应的日期（YYYY-MM-DD）
    _daily_inspection_limit = 0  # 今日总巡检上限（0=不限）
    _next_interval_minutes = None  # 下一次间隔的随机值（分钟，供前端显示）

    # ===== 实时日志（供前端展示）=====
    _recent_logs: list[dict] = []
    _max_recent_logs = 100

    @classmethod
    async def initialize(cls):
        """初始化所有 Checker"""
        if cls._initialized:
            return

        os.makedirs(DATA_DIR, exist_ok=True)
        await cls._load_results()
        cls._start_time = datetime.now(timezone.utc)

        assignments = assign_projects_to_checkers()
        for identity in CHECKER_IDENTITIES:
            checker = Checker(identity, assignments[identity["id"]])
            cls._checkers[checker.id] = checker

        # 初始化浏览器检查器
        try:
            from browser_checker import init_browser_checker
            bc = await init_browser_checker()
            if bc.available:
                logger.info("[CheckerManager] 浏览器深度检查已就绪")
            else:
                logger.warning("[CheckerManager] 浏览器深度检查不可用（Playwright 未安装）")
        except Exception as e:
            logger.warning(f"[CheckerManager] 浏览器检查器初始化失败: {e}")

        cls._initialized = True
        logger.info("[CheckerManager] 初始化完成，共 %d 个同步Checker", len(cls._checkers))

    @classmethod
    async def _load_results(cls):
        """从文件加载历史结果"""
        async with cls._lock:
            if os.path.exists(RESULTS_FILE):
                try:
                    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cls._results = data.get("history", {})
                    cls._latest = data.get("latest", {})
                    cls._async_latest = data.get("async_latest", {})
                    logger.info(f"[CheckerManager] 已加载历史结果，共 {len(cls._latest)} 个项目")
                except Exception as e:
                    logger.error(f"[CheckerManager] 加载历史结果失败: {e}")
                    cls._results = {}
                    cls._latest = {}
                    cls._async_latest = {}

    @classmethod
    async def _save_results_to_file(cls):
        """保存结果到文件"""
        async with cls._lock:
            try:
                data = {
                    "history": cls._results,
                    "latest": cls._latest,
                    "async_latest": cls._async_latest,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[CheckerManager] 保存结果失败: {e}")

    @classmethod
    def _add_log(cls, level: str, message: str):
        """添加实时日志（供前端展示）"""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        cls._recent_logs.append(log_entry)
        if len(cls._recent_logs) > cls._max_recent_logs:
            cls._recent_logs = cls._recent_logs[-cls._max_recent_logs:]

    @classmethod
    def get_recent_logs(cls) -> list[dict]:
        """获取最近的日志"""
        return list(cls._recent_logs[-50:])

    @classmethod
    async def save_result(cls, result: dict):
        """保存单次检查结果（同步Checker）"""
        project_name = result["project_name"]
        async with cls._lock:
            cls._latest[project_name] = result
            if project_name not in cls._results:
                cls._results[project_name] = []
            cls._results[project_name].append(result)
            if len(cls._results[project_name]) > HISTORY_MAX_SIZE:
                cls._results[project_name] = cls._results[project_name][-HISTORY_MAX_SIZE:]

            # 保存浏览器检查结果（如果有）
            browser_check = result.get("browser_check")
            if browser_check:
                cls._browser_latest[project_name] = browser_check
                if project_name not in cls._browser_results:
                    cls._browser_results[project_name] = []
                cls._browser_results[project_name].append(browser_check)
                if len(cls._browser_results[project_name]) > HISTORY_MAX_SIZE:
                    cls._browser_results[project_name] = cls._browser_results[project_name][-HISTORY_MAX_SIZE:]

        asyncio.create_task(cls._save_results_to_file())
        # WebSocket 推送
        await cls._ws_broadcast({
            "type": "status_update",
            "project": result,
        })
        # 添加实时日志
        log_msg = f"#{result.get('checker_id', '?')} 检测 {project_name}: {result['status']} " \
                  f"({result.get('response_time_ms', 0)}ms)"
        if browser_check:
            bc_status = "✓" if browser_check.get("browser_ok") else "✗"
            log_msg += f" [浏览器{bc_status}]"
        cls._add_log(
            "info" if result["status"] == "online" else "warning",
            log_msg
        )

    @classmethod
    async def save_async_result(cls, result: dict):
        """保存异步Checker结果"""
        project_name = result["project_name"]
        async with cls._lock:
            cls._async_latest[project_name] = result
        # 异步Checker结果也存入主历史
        async with cls._lock:
            if project_name not in cls._results:
                cls._results[project_name] = []
            cls._results[project_name].append(result)
            if len(cls._results[project_name]) > HISTORY_MAX_SIZE:
                cls._results[project_name] = cls._results[project_name][-HISTORY_MAX_SIZE:]
        asyncio.create_task(cls._save_results_to_file())
        await cls._ws_broadcast({
            "type": "async_status_update",
            "project": result,
        })
        # 添加实时日志
        detail = f"关键词={result.get('search_keyword', '?')}, " \
                 f"结果数={result.get('search_result_count', 0)}"
        cls._add_log(
            "info" if result["status"] == "online" else "warning",
            f"[异步] {project_name}: {result['status']} ({detail})"
        )

    @classmethod
    async def start_all(cls):
        """启动所有同步 Checker"""
        await cls.initialize()
        for checker in cls._checkers.values():
            if not checker.running:
                checker.task = asyncio.create_task(checker.run_loop())

    @classmethod
    async def stop_all(cls):
        """停止所有同步 Checker"""
        for checker in cls._checkers.values():
            if checker.running:
                checker.stop()
                if checker.task:
                    checker.task.cancel()
        # 关闭浏览器检查器
        try:
            from browser_checker import shutdown_browser_checker
            await shutdown_browser_checker()
        except Exception:
            pass

    @classmethod
    def pause_all(cls):
        """暂停所有同步 Checker（保持进程）"""
        for checker in cls._checkers.values():
            checker.pause()
        cls._add_log("info", "所有同步Checker已暂停")

    @classmethod
    def resume_all(cls):
        """恢复所有同步 Checker"""
        for checker in cls._checkers.values():
            checker.resume()
        cls._add_log("info", "所有同步Checker已恢复运行")

    @classmethod
    def get_all_status(cls) -> dict[str, dict]:
        return cls._latest.copy()

    @classmethod
    def get_async_status(cls) -> dict[str, dict]:
        return cls._async_latest.copy()

    @classmethod
    def get_project_history(cls, project_name: str) -> list[dict]:
        return cls._results.get(project_name, [])[-20:]

    @classmethod
    def get_all_history(cls) -> list[dict]:
        all_records = []
        for records in cls._results.values():
            all_records.extend(records)
        all_records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return all_records[:100]

    @classmethod
    def get_checkers_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._checkers.values()]

    @classmethod
    async def check_project_now(cls, project_name: str) -> dict | None:
        await cls.initialize()
        for checker in cls._checkers.values():
            for p in checker.projects:
                if p["name"] == project_name:
                    result = await checker.check_project(p)
                    await cls.save_result(result)
                    return result
        if cls._checkers:
            from config import get_project_by_name
            project = get_project_by_name(project_name)
            if project:
                checker = list(cls._checkers.values())[0]
                result = await checker.check_project(project)
                await cls.save_result(result)
                return result
        return None

    @classmethod
    async def check_all_now(cls) -> list[dict]:
        await cls.initialize()
        results = []
        tasks = []
        for project in PROJECTS:
            assigned = False
            for checker in cls._checkers.values():
                for p in checker.projects:
                    if p["name"] == project["name"]:
                        tasks.append(checker.check_project(project))
                        assigned = True
                        break
                if assigned:
                    break
            if not assigned and cls._checkers:
                checker = list(cls._checkers.values())[0]
                tasks.append(checker.check_project(project))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_results = []
        for r in results:
            if isinstance(r, dict):
                valid_results.append(r)
                await cls.save_result(r)
        return valid_results

    # ===== 总巡检次数管理 =====
    @classmethod
    def _ensure_daily_reset(cls):
        """确保每日计数重置"""
        today = datetime.now().strftime("%Y-%m-%d")
        if cls._inspection_count_date != today:
            cls._inspection_count_date = today
            cls._inspection_count = 0
            # 每日重置时，随机生成今日巡检上限
            from config import RuntimeConfig
            config = RuntimeConfig.get_instance()
            cls._daily_inspection_limit = config.get_random_total_inspections()
            logger.info(f"[CheckerManager] 每日巡检计数已重置。今日上限: {cls._daily_inspection_limit or '不限'}")

    @classmethod
    def can_run_inspection(cls) -> bool:
        """判断是否还可以继续巡检（未达今日上限）"""
        cls._ensure_daily_reset()
        if cls._daily_inspection_limit == 0:
            return True  # 不限
        return cls._inspection_count < cls._daily_inspection_limit

    @classmethod
    def increment_inspection_count(cls):
        """增加一次完整巡检计数（由第一个checker调用）"""
        cls._ensure_daily_reset()
        cls._inspection_count += 1
        logger.info(f"[CheckerManager] 今日已完成第 {cls._inspection_count} 轮巡检"
                    f"（上限: {cls._daily_inspection_limit or '不限'}）")

    @classmethod
    def get_inspection_stats(cls) -> dict:
        """获取巡检统计信息"""
        cls._ensure_daily_reset()
        # 获取下一次间隔（供前端显示）
        from config import RuntimeConfig
        config = RuntimeConfig.get_instance()
        if cls._next_interval_minutes is None:
            cls._next_interval_minutes = round(random.uniform(config.interval_min, config.interval_max), 1)
        return {
            "today_count": cls._inspection_count,
            "daily_limit": cls._daily_inspection_limit,
            "next_interval_minutes": cls._next_interval_minutes,
            "remaining": max(0, cls._daily_inspection_limit - cls._inspection_count) if cls._daily_inspection_limit > 0 else -1,
        }

    @classmethod
    def get_summary(cls) -> dict:
        latest = cls._latest
        online = sum(1 for r in latest.values() if r.get("status") == "online")
        offline = sum(1 for r in latest.values() if r.get("status") == "offline")
        slow = sum(1 for r in latest.values() if r.get("status") == "slow")
        total = len(PROJECTS)

        response_times = [
            r["response_time_ms"] for r in latest.values()
            if r.get("response_time_ms") is not None
        ]
        avg_response_time = round(sum(response_times) / len(response_times), 2) if response_times else 0

        last_check_times = [
            r["timestamp"] for r in latest.values() if r.get("timestamp")
        ]
        last_check = max(last_check_times) if last_check_times else None

        return {
            "total": total,
            "online": online,
            "offline": offline,
            "slow": slow,
            "avg_response_time_ms": avg_response_time,
            "last_check_time": last_check,
        }

    @classmethod
    def get_checker_workload(cls) -> dict[int, dict]:
        """获取各Checker的工作量分布"""
        workload = {}
        for checker in cls._checkers.values():
            workload[checker.id] = {
                "name": checker.name,
                "check_count": checker.check_count,
                "project_count": len(checker.projects),
                "running": checker.running and not checker.paused,
            }
        return workload

    # ===== 浏览器检查相关 =====
    @classmethod
    async def save_browser_result(cls, result: dict):
        """保存独立浏览器巡检循环的结果（供 browser_checker 回调使用）"""
        project_name = result.get("project_name", "")
        if not project_name:
            return

        async with cls._lock:
            cls._browser_latest[project_name] = result
            if project_name not in cls._browser_results:
                cls._browser_results[project_name] = []
            cls._browser_results[project_name].append(result)
            if len(cls._browser_results[project_name]) > HISTORY_MAX_SIZE:
                cls._browser_results[project_name] = cls._browser_results[project_name][-HISTORY_MAX_SIZE:]

        asyncio.create_task(cls._save_results_to_file())
        await cls._ws_broadcast({
            "type": "browser_check_update",
            "project_name": project_name,
            "result": result,
        })
        bc_status = "正常" if result.get("browser_ok") else "异常"
        log_msg = f"[浏览器巡检] {project_name}: {bc_status} (JS错误: {result.get('js_error_count', 0)})"
        cls._add_log(
            "info" if result.get("browser_ok") else "warning",
            log_msg,
        )

    @classmethod
    async def browser_check_project(cls, project_name: str) -> dict | None:
        """手动触发单个项目的浏览器深度检查"""
        await cls.initialize()
        from config import get_project_by_name
        project = get_project_by_name(project_name)
        if not project:
            return None
        # 找一个空闲的 checker 来执行
        for checker in cls._checkers.values():
            result = await checker.browser_check_only(project)
            # 保存浏览器检查结果
            async with cls._lock:
                cls._browser_latest[project_name] = result
                if project_name not in cls._browser_results:
                    cls._browser_results[project_name] = []
                cls._browser_results[project_name].append(result)
                if len(cls._browser_results[project_name]) > HISTORY_MAX_SIZE:
                    cls._browser_results[project_name] = cls._browser_results[project_name][-HISTORY_MAX_SIZE:]
            await cls._ws_broadcast({
                "type": "browser_check_update",
                "project_name": project_name,
                "result": result,
            })
            cls._add_log(
                "info" if result.get("browser_ok") else "warning",
                f"[浏览器] {project_name}: {'正常' if result.get('browser_ok') else '异常'} "
                f"(JS错误: {result.get('js_error_count', 0)})"
            )
            return result
        return None

    @classmethod
    def get_browser_latest(cls, project_name: str) -> dict | None:
        """获取指定项目最新的浏览器检查结果"""
        return cls._browser_latest.get(project_name)

    @classmethod
    def get_browser_all_latest(cls) -> dict[str, dict]:
        """获取所有项目最新的浏览器检查结果"""
        return cls._browser_latest.copy()

    @classmethod
    def get_browser_history(cls, project_name: str) -> list[dict]:
        """获取指定项目的浏览器检查历史"""
        return cls._browser_results.get(project_name, [])[-20:]

    # ===== 健康检查相关 =====
    @classmethod
    def get_health_info(cls) -> dict:
        """获取服务健康信息"""
        import psutil  # 延迟导入
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        uptime = None
        if cls._start_time:
            uptime = (datetime.now(timezone.utc) - cls._start_time).total_seconds()

        # 统计各状态数量
        latest = cls._latest
        online = sum(1 for r in latest.values() if r.get("status") == "online")
        offline = sum(1 for r in latest.values() if r.get("status") == "offline")

        return {
            "status": "ok" if offline <= 2 else "degraded",
            "service": "ai-health-checker",
            "version": "2.1.0",
            "uptime_seconds": round(uptime, 1) if uptime else None,
            "uptime_formatted": cls._format_uptime(uptime) if uptime else None,
            "memory": {
                "rss_mb": round(memory_info.rss / 1024 / 1024, 2),
                "vms_mb": round(memory_info.vms / 1024 / 1024, 2),
                "percent": round(process.memory_percent(), 2),
            },
            "projects": {
                "total": len(PROJECTS),
                "online": online,
                "offline": offline,
                "slow": sum(1 for r in latest.values() if r.get("status") == "slow"),
            },
            "checkers": {
                "sync_total": len(cls._checkers),
                "sync_running": sum(1 for c in cls._checkers.values() if c.running and not c.paused),
                "async_total": 0,  # 由main.py补充
                "async_running": 0,
            },
            "browser_checker": {
                "available": get_browser_checker().available,
                "total_checks": sum(1 for r in cls._browser_results.values() for _ in r),
            },
            "inspections_today": cls._inspection_count,
            "start_time": cls._start_time.isoformat() if cls._start_time else None,
        }

    @staticmethod
    def _format_uptime(seconds: float | None) -> str:
        if not seconds:
            return "N/A"
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分钟")
        return "".join(parts) if parts else "刚刚启动"

    # ===== WebSocket 相关 =====
    @classmethod
    async def add_ws_client(cls, websocket):
        cls._ws_clients.append(websocket)

    @classmethod
    async def remove_ws_client(cls, websocket):
        if websocket in cls._ws_clients:
            cls._ws_clients.remove(websocket)

    @classmethod
    async def _ws_broadcast(cls, message: dict):
        """广播消息到所有WebSocket客户端"""
        if not cls._ws_clients:
            return
        import json as _json
        msg_text = _json.dumps(message, ensure_ascii=False)
        dead_clients = []
        for ws in cls._ws_clients:
            try:
                await ws.send_text(msg_text)
            except Exception:
                dead_clients.append(ws)
        for ws in dead_clients:
            if ws in cls._ws_clients:
                cls._ws_clients.remove(ws)

    @classmethod
    async def ws_broadcast_config(cls, config: dict):
        """广播配置变更"""
        await cls._ws_broadcast({
            "type": "config_update",
            "config": config,
        })

    @classmethod
    async def ws_broadcast_control(cls, action: str):
        """广播控制指令"""
        await cls._ws_broadcast({
            "type": "control",
            "action": action,
        })

    @classmethod
    async def ws_broadcast_log(cls, log_entry: dict):
        """广播日志更新"""
        await cls._ws_broadcast({
            "type": "log_update",
            "log": log_entry,
        })
