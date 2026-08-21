"""节点管理器 - 注册/心跳/状态监控/Checker分配/安装指令队列"""
import asyncio
import logging
from datetime import datetime, timezone
from config import (
    load_nodes, save_nodes, DEFAULT_BUILTIN_NODE,
)

logger = logging.getLogger("health_checker")


class NodeManager:
    """管理所有工作节点（含 builtin 内置节点）"""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        # 安装指令队列: {node_id: [{"command": "install_package", "package": "xxx", "id": "cmd-xxx"}, ...]}
        self.install_commands: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self):
        nodes = load_nodes()
        for n in nodes:
            self.nodes[n["node_id"]] = n
        # 确保 builtin 节点存在
        if "builtin" not in self.nodes:
            self.nodes["builtin"] = dict(DEFAULT_BUILTIN_NODE)
            await self._persist()
        logger.info(f"[NodeManager] 初始化完成，节点数: {len(self.nodes)}")

    async def _persist(self):
        save_nodes(list(self.nodes.values()))

    async def register_node(self, node_info: dict) -> dict:
        """注册或更新节点（心跳也走这里）"""
        node_id = node_info.get("node_id")
        if not node_id:
            node_id = node_info.get("id", "")
        if not node_id:
            import uuid
            node_id = f"local-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            existing = self.nodes.get(node_id, {})
            node = {
                "node_id": node_id,
                "name": node_info.get("name", existing.get("name", f"节点-{node_id[:8]}")),
                "ip": node_info.get("ip", existing.get("ip", "")),
                "os": node_info.get("os", existing.get("os", "")),
                "python_version": node_info.get("python_version", existing.get("python_version", "")),
                "status": "online",
                "last_heartbeat": now,
                "capabilities": node_info.get("capabilities", existing.get("capabilities", {})),
                "registered_at": existing.get("registered_at", now),
                "is_builtin": existing.get("is_builtin", False),
                "installed_packages": node_info.get("installed_packages", existing.get("installed_packages", [])),
                "install_results": node_info.get("install_results", existing.get("install_results", [])),
            }
            self.nodes[node_id] = node
            await self._persist()
        logger.info(f"[NodeManager] 节点注册/心跳: {node_id} ({node['name']})")
        return node

    async def heartbeat(self, node_id: str, status: str = "online",
                        capabilities: dict | None = None,
                        installed_packages: list | None = None,
                        install_results: list | None = None) -> dict:
        """节点心跳更新"""
        async with self._lock:
            node = self.nodes.get(node_id)
            if not node:
                return {"error": "节点未注册"}
            node["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            node["status"] = status
            if capabilities:
                node["capabilities"] = capabilities
            if installed_packages is not None:
                node["installed_packages"] = installed_packages
            if install_results is not None:
                # 追加安装结果
                node.setdefault("install_results", []).extend(install_results)
                # 只保留最近20条
                node["install_results"] = node["install_results"][-20:]
            await self._persist()
            # 返回待执行的安装指令
            pending = self.install_commands.pop(node_id, [])
            return {"node": node, "install_commands": pending}

    async def unregister_node(self, node_id: str) -> bool:
        if node_id == "builtin":
            return False
        async with self._lock:
            if node_id in self.nodes:
                del self.nodes[node_id]
                self.install_commands.pop(node_id, None)
                await self._persist()
                return True
        return False

    def get_node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def list_nodes(self) -> list[dict]:
        return list(self.nodes.values())

    async def update_node(self, node_id: str, updates: dict) -> dict | None:
        async with self._lock:
            node = self.nodes.get(node_id)
            if not node:
                return None
            for key in ("name",):
                if key in updates:
                    node[key] = updates[key]
            await self._persist()
            return node

    async def check_stale_nodes(self, timeout: int = 120):
        """将超过 timeout 秒没心跳的节点标记为 offline"""
        now = datetime.now(timezone.utc)
        changed = False
        for node in self.nodes.values():
            if node.get("is_builtin"):
                node["status"] = "online"
                continue
            last_hb = node.get("last_heartbeat")
            if not last_hb:
                continue
            try:
                last_time = datetime.fromisoformat(last_hb)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                if (now - last_time).total_seconds() > timeout:
                    if node["status"] != "offline":
                        node["status"] = "offline"
                        changed = True
                        logger.info(f"[NodeManager] 节点 {node['node_id']} 心跳超时，标记 offline")
            except Exception:
                pass
        if changed:
            await self._persist()

    async def queue_install_command(self, node_id: str, command: str,
                                     package: str | None = None) -> dict:
        """下发安装指令到节点队列"""
        import uuid
        cmd = {
            "id": f"cmd-{uuid.uuid4().hex[:8]}",
            "command": command,
            "package": package,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.install_commands.setdefault(node_id, []).append(cmd)
        logger.info(f"[NodeManager] 向节点 {node_id} 下发安装指令: {command} {package or ''}")
        return cmd

    def get_online_node_ids(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.get("status") == "online"]
