"""Checker 调度器 - 统一管理所有 checker 的创建、启停、节点分配"""
import asyncio
import logging
from datetime import datetime, timezone

from config import (
    load_checkers_config, save_checkers_config, load_projects,
)
from checker import Checker, CheckerManager
from async_checker import AsyncChecker, AsyncCheckerManager
from node_manager import NodeManager

logger = logging.getLogger("health_checker")


class CheckerDispatcher:
    """统一调度器：管理 checker 配置、builtin 实例、远程节点分配"""

    def __init__(self, node_manager: NodeManager):
        self.node_manager = node_manager
        self.checkers_config: list[dict] = []
        # builtin 运行实例: {checker_id: Checker/AsyncChecker}
        self._builtin_instances: dict[str, object] = {}
        # 远程 checker 运行状态跟踪: {checker_id: {"last_result": ..., "check_count": ...}}
        self._remote_stats: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def initialize(self):
        """加载 checkers.json 并为 builtin 节点创建运行实例"""
        self.checkers_config = load_checkers_config()
        for cfg in self.checkers_config:
            if cfg.get("enabled", True) and cfg.get("node_id") == "builtin":
                await self._start_builtin_checker(cfg)
        logger.info(f"[Dispatcher] 初始化完成，共 {len(self.checkers_config)} 个 checker 配置")

    async def _start_builtin_checker(self, cfg: dict):
        """为 builtin 节点创建并启动 checker 实例"""
        cid = cfg["id"]
        ctype = cfg.get("type", "sync")
        if ctype == "sync":
            checker = await CheckerManager.create_checker(cfg)
            checker.start()
            self._builtin_instances[cid] = checker
        elif ctype == "async":
            checker = await AsyncCheckerManager.create_checker(cfg)
            checker.start()
            self._builtin_instances[cid] = checker
        elif ctype == "browser":
            # builtin 不支持 browser
            logger.warning(f"[Dispatcher] Browser checker {cid} 不能在 builtin 节点运行")
        logger.info(f"[Dispatcher] 启动 builtin checker: {cid} ({cfg.get('name')})")

    async def _stop_builtin_checker(self, checker_id: str):
        inst = self._builtin_instances.pop(checker_id, None)
        if inst:
            inst.stop()
            if isinstance(inst, Checker):
                await CheckerManager.remove_checker(checker_id)
            elif isinstance(inst, AsyncChecker):
                await AsyncCheckerManager.remove_checker(checker_id)
            logger.info(f"[Dispatcher] 停止 builtin checker: {checker_id}")

    # ========== CRUD ==========
    def list_checkers(self) -> list[dict]:
        """列出所有 checker（含运行状态）"""
        result = []
        for cfg in self.checkers_config:
            cid = cfg["id"]
            entry = dict(cfg)
            # 运行状态
            if cid in self._builtin_instances:
                inst = self._builtin_instances[cid]
                entry["runtime"] = {
                    "running": inst.running,
                    "paused": inst.paused,
                    "check_count": inst.check_count,
                    "failed_count": inst.failed_count,
                    "last_check_time": inst.last_check_time,
                    "current_task": inst.current_task,
                }
            else:
                # 远程节点 checker
                stats = self._remote_stats.get(cid, {})
                node = self.node_manager.get_node(cfg.get("node_id", ""))
                node_online = node and node.get("status") == "online"
                entry["runtime"] = {
                    "running": cfg.get("enabled", True) and node_online,
                    "paused": not node_online,
                    "check_count": stats.get("check_count", 0),
                    "failed_count": stats.get("failed_count", 0),
                    "last_check_time": stats.get("last_check_time"),
                    "current_task": "等待节点上线" if not node_online else "远程运行中",
                    "node_online": node_online,
                }
            result.append(entry)
        return result

    def get_checker(self, checker_id: str) -> dict | None:
        for cfg in self.checkers_config:
            if cfg["id"] == checker_id:
                return cfg
        return None

    async def add_checker(self, config: dict) -> dict:
        """创建新 checker"""
        import uuid
        cid = config.get("id") or f"chk-{uuid.uuid4().hex[:8]}"
        config["id"] = cid
        config.setdefault("enabled", True)
        config.setdefault("projects", [])
        config.setdefault("config", {})

        # 验证：browser 类型不能分配给 builtin
        if config.get("type") == "browser" and config.get("node_id") == "builtin":
            raise ValueError("浏览器检测不能分配给服务器内置节点，请选择有浏览器的本地节点")

        self.checkers_config.append(config)
        save_checkers_config(self.checkers_config)

        # 如果分配给 builtin 且启用，立即启动
        if config.get("enabled", True) and config.get("node_id") == "builtin":
            await self._start_builtin_checker(config)

        logger.info(f"[Dispatcher] 添加 checker: {cid} ({config.get('name')})")
        return config

    async def update_checker(self, checker_id: str, updates: dict) -> dict | None:
        """更新 checker 配置"""
        cfg = None
        for i, c in enumerate(self.checkers_config):
            if c["id"] == checker_id:
                cfg = c
                break
        if not cfg:
            return None

        old_node = cfg.get("node_id")
        old_enabled = cfg.get("enabled", True)
        # 更新字段
        for key in ("name", "type", "node_id", "enabled", "interval_min",
                     "interval_max", "projects", "config"):
            if key in updates:
                cfg[key] = updates[key]

        # browser 不能在 builtin
        if cfg.get("type") == "browser" and cfg.get("node_id") == "builtin":
            raise ValueError("浏览器检测不能分配给服务器内置节点")

        save_checkers_config(self.checkers_config)

        # 处理 builtin 实例变更
        if old_node == "builtin":
            await self._stop_builtin_checker(checker_id)
        if cfg.get("enabled", True) and cfg.get("node_id") == "builtin":
            await self._start_builtin_checker(cfg)

        logger.info(f"[Dispatcher] 更新 checker: {checker_id}")
        return cfg

    async def delete_checker(self, checker_id: str) -> bool:
        cfg = self.get_checker(checker_id)
        if not cfg:
            return False
        if checker_id in self._builtin_instances:
            await self._stop_builtin_checker(checker_id)
        self.checkers_config = [c for c in self.checkers_config if c["id"] != checker_id]
        save_checkers_config(self.checkers_config)
        self._remote_stats.pop(checker_id, None)
        logger.info(f"[Dispatcher] 删除 checker: {checker_id}")
        return True

    async def start_checker(self, checker_id: str):
        cfg = self.get_checker(checker_id)
        if not cfg:
            return
        cfg["enabled"] = True
        save_checkers_config(self.checkers_config)
        if cfg.get("node_id") == "builtin":
            if checker_id not in self._builtin_instances:
                await self._start_builtin_checker(cfg)
            else:
                self._builtin_instances[checker_id].resume()
        logger.info(f"[Dispatcher] 启动 checker: {checker_id}")

    async def stop_checker(self, checker_id: str):
        cfg = self.get_checker(checker_id)
        if not cfg:
            return
        cfg["enabled"] = False
        save_checkers_config(self.checkers_config)
        if checker_id in self._builtin_instances:
            self._builtin_instances[checker_id].stop()
            await self._stop_builtin_checker(checker_id)
        logger.info(f"[Dispatcher] 停止 checker: {checker_id}")

    async def pause_checker(self, checker_id: str):
        if checker_id in self._builtin_instances:
            self._builtin_instances[checker_id].pause()
        logger.info(f"[Dispatcher] 暂停 checker: {checker_id}")

    async def resume_checker(self, checker_id: str):
        cfg = self.get_checker(checker_id)
        if not cfg or not cfg.get("enabled", True):
            return
        if checker_id in self._builtin_instances:
            self._builtin_instances[checker_id].resume()
        elif cfg.get("node_id") == "builtin":
            await self._start_builtin_checker(cfg)
        logger.info(f"[Dispatcher] 恢复 checker: {checker_id}")

    # ========== 节点任务分发 ==========
    def get_node_tasks(self, node_id: str) -> list[dict]:
        """获取分配给指定节点的 checker 配置和项目列表"""
        projects = load_projects()
        tasks = []
        for cfg in self.checkers_config:
            if cfg.get("node_id") == node_id and cfg.get("enabled", True):
                # 过滤项目
                cfg_projects = cfg.get("projects", [])
                if cfg_projects:
                    assigned = [p for p in projects if p["name"] in cfg_projects]
                else:
                    assigned = list(projects)
                task = dict(cfg)
                task["assigned_projects"] = assigned
                tasks.append(task)
        return tasks

    async def report_result(self, node_id: str, checker_id: str, results: list[dict]):
        """节点回报检测结果"""
        from checker import CheckerManager
        for result in results:
            result["node_id"] = node_id
            ctype = result.get("checker_type", result.get("type", "sync"))
            if ctype == "browser":
                # browser 结果作为 visit 结果存储
                await CheckerManager.save_local_visit_result(result)
            elif ctype == "async":
                await CheckerManager.save_async_result(result)
            else:
                await CheckerManager.save_result(result)

        # 更新远程统计
        stats = self._remote_stats.setdefault(checker_id, {
            "check_count": 0, "failed_count": 0, "last_check_time": None,
        })
        stats["check_count"] += len(results)
        stats["failed_count"] += sum(1 for r in results if r.get("status") == "offline")
        if results:
            stats["last_check_time"] = results[-1].get("timestamp")

    def get_node_checker_ids(self, node_id: str) -> list[str]:
        return [c["id"] for c in self.checkers_config if c.get("node_id") == node_id]

    def pause_all(self):
        for inst in self._builtin_instances.values():
            inst.pause()

    def resume_all(self):
        for inst in self._builtin_instances.values():
            inst.resume()

    def stop_all(self):
        for inst in self._builtin_instances.values():
            inst.stop()
