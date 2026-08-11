"""Checker 引擎 - 10个异步 Checker 子 agent 核心模块"""
import asyncio
import json
import os
import ssl
import time
import random
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from config import (
    CHECKER_IDENTITIES,
    PROJECTS,
    REQUEST_TIMEOUT,
    SLOW_THRESHOLD,
    HISTORY_MAX_SIZE,
    RESULTS_FILE,
    DATA_DIR,
    get_random_ip,
    get_random_interval,
    assign_projects_to_checkers,
)


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
        self.current_task = "空闲"
        self.last_check_time = None
        self._stop_event = asyncio.Event()

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

            # 解析证书有效期
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
                    # 可能是普通页面，不算错误
                    return {
                        "api_ok": True,
                        "api_status": resp.status_code,
                        "api_has_json": False,
                        "api_sample_keys": [],
                    }
        except Exception:
            # /api 路径不存在或出错，不算主站故障
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

                # 状态判断
                if resp.status_code == 200:
                    if response_time_ms > SLOW_THRESHOLD * 1000:
                        result["status"] = "slow"
                    else:
                        result["status"] = "online"
                else:
                    result["status"] = "offline"

                # 页面内容检查
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
            result["error"] = "请求超时"
        except httpx.ConnectError as e:
            elapsed = time.time() - start_time
            result["status"] = "offline"
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"连接失败: {str(e)[:80]}"
        except Exception as e:
            elapsed = time.time() - start_time
            result["status"] = "offline"
            result["response_time_ms"] = round(elapsed * 1000, 2)
            result["error"] = f"未知错误: {str(e)[:80]}"

        # SSL 检查（HTTPS 站点）
        if url.startswith("https://"):
            result["ssl_check"] = await self._check_ssl(url)

        # API 端点检测（可选，不影响主状态）
        result["api_check"] = await self._check_api_endpoint(url, headers)

        self.check_count += 1
        self.last_check_time = datetime.now(timezone.utc).isoformat()
        self.current_task = "空闲"

        return result

    async def run_loop(self):
        """Checker 主循环 - 持续检测负责的项目"""
        self.running = True
        self._stop_event.clear()

        while self.running:
            for project in self.projects:
                if not self.running:
                    break
                try:
                    result = await self.check_project(project)
                    # 存入全局结果存储
                    await CheckerManager.save_result(result)
                except Exception as e:
                    print(f"[Checker-{self.id}] 检测 {project['name']} 异常: {e}")

                # 随机间隔
                interval = get_random_interval()
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(interval),
                        timeout=interval + 1,
                    )
                except asyncio.TimeoutError:
                    pass

                # 检查停止信号
                if self._stop_event.is_set():
                    break

            # 如果没有项目，等待一下避免死循环
            if not self.projects:
                await asyncio.sleep(60)

        self.running = False

    def stop(self):
        """停止 Checker"""
        self.running = False
        self._stop_event.set()

    def get_status(self) -> dict:
        """获取 Checker 运行状态"""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "user_agent": self.user_agent,
            "ip_sample": self.ip_pool[0],
            "running": self.running,
            "check_count": self.check_count,
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
    _lock = asyncio.Lock()
    _initialized = False

    @classmethod
    async def initialize(cls):
        """初始化所有 Checker"""
        if cls._initialized:
            return

        # 确保数据目录存在
        os.makedirs(DATA_DIR, exist_ok=True)

        # 加载已有结果
        await cls._load_results()

        # 分配项目并创建 Checker
        assignments = assign_projects_to_checkers()
        for identity in CHECKER_IDENTITIES:
            checker = Checker(identity, assignments[identity["id"]])
            cls._checkers[checker.id] = checker

        cls._initialized = True

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
                except Exception as e:
                    print(f"加载历史结果失败: {e}")
                    cls._results = {}
                    cls._latest = {}

    @classmethod
    async def _save_results_to_file(cls):
        """保存结果到文件"""
        async with cls._lock:
            try:
                data = {
                    "history": cls._results,
                    "latest": cls._latest,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"保存结果失败: {e}")

    @classmethod
    async def save_result(cls, result: dict):
        """保存单次检查结果"""
        project_name = result["project_name"]
        async with cls._lock:
            cls._latest[project_name] = result
            if project_name not in cls._results:
                cls._results[project_name] = []
            cls._results[project_name].append(result)
            # 限制历史记录数量
            if len(cls._results[project_name]) > HISTORY_MAX_SIZE:
                cls._results[project_name] = cls._results[project_name][-HISTORY_MAX_SIZE:]
        # 异步写文件（不阻塞）
        asyncio.create_task(cls._save_results_to_file())

    @classmethod
    async def start_all(cls):
        """启动所有 Checker"""
        await cls.initialize()
        for checker in cls._checkers.values():
            if not checker.running:
                checker.task = asyncio.create_task(checker.run_loop())
                print(f"[Checker-{checker.id}] {checker.name} 已启动，负责 {len(checker.projects)} 个项目")

    @classmethod
    async def stop_all(cls):
        """停止所有 Checker"""
        for checker in cls._checkers.values():
            if checker.running:
                checker.stop()
                if checker.task:
                    checker.task.cancel()
                print(f"[Checker-{checker.id}] {checker.name} 已停止")

    @classmethod
    def get_all_status(cls) -> dict[str, dict]:
        """获取所有项目最新状态"""
        return cls._latest.copy()

    @classmethod
    def get_project_history(cls, project_name: str) -> list[dict]:
        """获取单个项目历史"""
        return cls._results.get(project_name, [])[-20:]  # 最近20条

    @classmethod
    def get_all_history(cls) -> list[dict]:
        """获取所有项目最近检查历史（最多100条）"""
        all_records = []
        for records in cls._results.values():
            all_records.extend(records)
        # 按时间倒序
        all_records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return all_records[:100]

    @classmethod
    def get_checkers_status(cls) -> list[dict]:
        """获取所有 Checker 状态"""
        return [c.get_status() for c in cls._checkers.values()]

    @classmethod
    async def check_project_now(cls, project_name: str) -> dict | None:
        """立即触发某个项目的检查（找负责它的 checker）"""
        await cls.initialize()
        for checker in cls._checkers.values():
            for p in checker.projects:
                if p["name"] == project_name:
                    result = await checker.check_project(p)
                    await cls.save_result(result)
                    return result
        # 如果没找到分配的 checker，用第一个 checker 检测
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
        """立即触发所有项目检查"""
        await cls.initialize()
        results = []
        # 并发检测
        tasks = []
        for project in PROJECTS:
            # 找负责的 checker
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
                # 兜底
                checker = list(cls._checkers.values())[0]
                tasks.append(checker.check_project(project))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_results = []
        for r in results:
            if isinstance(r, dict):
                valid_results.append(r)
                await cls.save_result(r)
        return valid_results

    @classmethod
    def get_summary(cls) -> dict:
        """获取总览统计"""
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
