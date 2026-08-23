"""FastAPI 主服务 - 服务器面板 + 节点架构（宝塔模式）
端口 8700，Docker 部署
"""
import os
import sys
import asyncio
import logging
import json
import re
import platform
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import CheckerManager, VideoCheckerManager
from async_checker import AsyncCheckerManager
from indexnow_pusher import IndexNowPusher
from config import (
    PROJECTS_FILE, NODES_FILE, CHECKERS_FILE, CONFIG_FILE,
    RESULTS_FILE, VIDEO_CONFIG_FILE, DATA_DIR,
    load_projects, save_projects, get_project_by_name,
    RuntimeConfig, VideoConfig,
)
from node_manager import NodeManager
from checker_dispatcher import CheckerDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("health_checker")

FRONTEND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "frontend",
    "panel.html",
)
APP_VERSION = "3.0.0"

# ========== 全局实例 ==========
node_manager = NodeManager()
dispatcher: Optional[CheckerDispatcher] = None
_indexnow_pusher: Optional[IndexNowPusher] = None
_time_range_task = None
_node_monitor_task = None


# ========== 请求模型 ==========
class ControlRequest(BaseModel):
    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, v):
        if v not in ("start", "stop"):
            raise ValueError("action 必须是 start 或 stop")
        return v


class TimeRange(BaseModel):
    start: str = "00:00"
    end: str = "23:59"

    @field_validator("start", "end")
    @classmethod
    def validate_time(cls, v):
        if not re.match(r'^\d{2}:\d{2}$', v):
            raise ValueError("时间格式必须为 HH:MM")
        return v


class ConfigUpdateRequest(BaseModel):
    inspection_enabled: Optional[bool] = None
    time_range: Optional[TimeRange] = None
    interval_min: Optional[int] = Field(default=None, ge=1, le=1440)
    interval_max: Optional[int] = Field(default=None, ge=1, le=1440)
    search_engine: Optional[str] = None
    visitor_interval_min: Optional[int] = Field(default=None, ge=1, le=1440)
    visitor_interval_max: Optional[int] = Field(default=None, ge=1, le=1440)
    default_visit_count: Optional[int] = Field(default=None, ge=1, le=100)
    indexnow_interval_hours: Optional[int] = Field(default=None, ge=1, le=168)
    browser_concurrency: Optional[int] = Field(default=None, ge=1, le=5)


# 节点模型
class NodeRegisterRequest(BaseModel):
    node_id: Optional[str] = None
    name: Optional[str] = None
    ip: Optional[str] = ""
    os: Optional[str] = ""
    python_version: Optional[str] = ""
    capabilities: Optional[dict] = None
    installed_packages: Optional[list] = None


class NodeHeartbeatRequest(BaseModel):
    node_id: str
    status: str = "online"
    capabilities: Optional[dict] = None
    installed_packages: Optional[list] = None
    install_results: Optional[list] = None


class NodeInstallRequest(BaseModel):
    command: str  # install_package / install_browser
    package: Optional[str] = None


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = None


# Checker 模型
class CheckerCreateRequest(BaseModel):
    name: str
    type: str  # sync / async / browser / video
    node_id: str = "builtin"
    enabled: bool = True
    interval_min: int = 5
    interval_max: int = 15
    projects: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ("sync", "async", "browser", "video"):
            raise ValueError("type 必须是 sync、async、browser 或 video")
        return v


class CheckerUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    node_id: Optional[str] = None
    enabled: Optional[bool] = None
    interval_min: Optional[int] = None
    interval_max: Optional[int] = None
    projects: Optional[list] = None
    config: Optional[dict] = None


# 项目模型
class ProjectRequest(BaseModel):
    name: str
    url: str
    category: str = "工具"
    sub_paths: list[str] = Field(default_factory=list)
    is_spa: bool = False
    check_count: int = Field(default=1, ge=1, le=100)


class NodeResultRequest(BaseModel):
    node_id: str
    checker_id: str
    results: list[dict]


# 本地访问结果（兼容旧版）
class LocalVisitResult(BaseModel):
    client_id: str = ""
    project_name: str
    project_url: str
    success: bool = False
    pages_visited: int = 0
    duration_seconds: float = 0
    user_agent: str = ""
    device_type: str = "desktop"
    error: Optional[str] = None
    timestamp: str = ""
    extra: Optional[dict] = None


# 视频模型
class VideoTypeRequest(BaseModel):
    type: str = "sync"

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ("sync", "async"):
            raise ValueError("type 必须是 sync 或 async")
        return v


class VideoConfigItem(BaseModel):
    name: str
    url: str
    play_count: int = Field(default=5, ge=1, le=100)
    # 关联的视频检查器 ID 列表（哪些检查器要播放此视频）
    checker_ids: list[str] = Field(default_factory=list)
    # 异步搜索关键词列表（async 类型视频检查器使用）
    keywords: list[str] = Field(default_factory=list)


class VideoConfigBatchRequest(BaseModel):
    videos: Optional[list[VideoConfigItem]] = None
    delete: Optional[list[str]] = None


# ========== 后台任务 ==========
async def time_range_monitor():
    config = RuntimeConfig.get_instance()
    last_paused = None
    while True:
        try:
            target_paused = not (config.inspection_enabled and config.is_within_time_range())
            if last_paused is None or target_paused != last_paused:
                if target_paused:
                    dispatcher.pause_all()
                else:
                    dispatcher.resume_all()
                await CheckerManager.ws_broadcast_control("pause" if target_paused else "resume")
                last_paused = target_paused
        except Exception as e:
            logger.error(f"[TimeRange] 监控异常: {e}")
        await asyncio.sleep(30)


async def node_monitor():
    """每30秒检查节点存活"""
    while True:
        try:
            await node_manager.check_stale_nodes(timeout=120)
        except Exception as e:
            logger.error(f"[NodeMonitor] 异常: {e}")
        await asyncio.sleep(30)


async def on_config_change(changed_keys: set):
    logger.info(f"[Config] 配置变更: {changed_keys}")
    config = RuntimeConfig.get_instance()
    await CheckerManager.ws_broadcast_config(config.to_dict())


# ========== 生命周期 ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispatcher, _time_range_task, _node_monitor_task, _indexnow_pusher

    os.makedirs(DATA_DIR, exist_ok=True)

    config = RuntimeConfig.get_instance()
    await config.load()
    config.register_listener(on_config_change)

    video_config = VideoConfig.get_instance()
    await video_config.load()

    await node_manager.initialize()
    dispatcher = CheckerDispatcher(node_manager)
    await dispatcher.initialize()

    if not config.inspection_enabled or not config.is_within_time_range():
        dispatcher.pause_all()

    _time_range_task = asyncio.create_task(time_range_monitor())
    _node_monitor_task = asyncio.create_task(node_monitor())

    _indexnow_pusher = IndexNowPusher(load_projects, key=config.indexnow_key or None)
    if config.indexnow_interval_hours:
        _indexnow_pusher.interval_hours = config.indexnow_interval_hours
    if not config.indexnow_key:
        config.indexnow_key = _indexnow_pusher.key
        config.save()
    _indexnow_pusher.start()

    logger.info(f"AI Health Checker v{APP_VERSION} 启动完成（面板+节点架构）")
    yield

    if _time_range_task:
        _time_range_task.cancel()
    if _node_monitor_task:
        _node_monitor_task.cancel()
    if dispatcher:
        dispatcher.stop_all()
    if _indexnow_pusher:
        await _indexnow_pusher.stop()
    logger.info("服务已关闭")


app = FastAPI(
    title="AI Health Checker Panel",
    description="服务器面板 + 本地节点架构",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ========== 前端页面 ==========
@app.get("/", response_class=HTMLResponse)
async def get_panel():
    if os.path.exists(FRONTEND_PATH):
        with open(FRONTEND_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Panel loading...</h1>")


# ========== 健康检查 ==========
@app.get("/api/health")
async def health_check():
    info = CheckerManager.get_health_info()
    info.update({
        "service": "ai-health-checker",
        "version": APP_VERSION,
        "projects_total": len(load_projects()),
        "nodes_online": len([n for n in node_manager.list_nodes() if n.get("status") == "online"]),
        "checkers_total": len(dispatcher.list_checkers()) if dispatcher else 0,
    })
    return info


# ========== 状态 API ==========
@app.get("/api/status")
async def get_all_status():
    projects = load_projects()
    sync_status = CheckerManager.get_all_status()
    async_status = CheckerManager.get_async_status()
    summary = CheckerManager.get_summary()
    config = RuntimeConfig.get_instance()
    visit_latest = CheckerManager.get_visit_all_latest()

    all_projects = {}
    for p in projects:
        base = sync_status.get(p["name"], {
            "project_name": p["name"], "project_url": p["url"],
            "category": p["category"], "status": "pending",
            "status_code": None, "response_time_ms": None,
            "checker_id": None, "timestamp": None,
        }).copy()
        base["name"] = base.get("name") or p["name"]
        base["url"] = base.get("url") or p["url"]
        base["category"] = base.get("category") or p["category"]
        base["visit_count"] = config.get_project_visit_count(p["name"])
        all_projects[p["name"]] = base

    return {
        "summary": summary,
        "projects": all_projects,
        "async_projects": async_status,
        "visit_latest": visit_latest,
        "inspection_enabled": config.inspection_enabled,
        "within_time_range": config.is_within_time_range(),
        "inspection_stats": CheckerManager.get_inspection_stats(),
    }


@app.get("/api/status/{project_name}")
async def get_project_status(project_name: str):
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "项目不存在")
    latest = CheckerManager.get_all_status().get(project_name)
    history = CheckerManager.get_project_history(project_name)
    return {"project": project, "latest": latest, "history": history}


@app.post("/api/check/{project_name}")
async def check_project_now(project_name: str):
    result = await CheckerManager.check_project_now(project_name)
    if not result:
        raise HTTPException(500, "检查失败或无可用 checker")
    return {"message": f"已完成 {project_name} 检查", "result": result}


@app.post("/api/check-all")
async def check_all_now():
    results = await CheckerManager.check_all_now()
    return {"message": f"已完成 {len(results)} 个项目检查", "count": len(results), "results": results}


@app.get("/api/history")
async def get_history():
    history = CheckerManager.get_all_history()
    return {"total": len(history), "records": history}


# ========== 巡检控制 ==========
@app.post("/api/control")
async def control_inspection(req: ControlRequest):
    config = RuntimeConfig.get_instance()
    if req.action == "start":
        await config.update({"inspection_enabled": True})
        if config.is_within_time_range():
            dispatcher.resume_all()
        await CheckerManager.ws_broadcast_control("start")
        return {"message": "巡检已启动", "enabled": True}
    else:
        await config.update({"inspection_enabled": False})
        dispatcher.pause_all()
        await CheckerManager.ws_broadcast_control("stop")
        return {"message": "巡检已停止", "enabled": False}


# ========== 配置 API ==========
@app.get("/api/config")
async def get_config():
    return RuntimeConfig.get_instance().to_dict()


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    config = RuntimeConfig.get_instance()
    updates = req.model_dump(exclude_none=True)
    if req.time_range:
        updates["time_range"] = {"start": req.time_range.start, "end": req.time_range.end}
    if not updates:
        return {"message": "无更新", "config": config.to_dict()}
    result = await config.update(updates)
    # IndexNow 间隔
    if "indexnow_interval_hours" in updates and _indexnow_pusher:
        await _indexnow_pusher.set_interval(updates["indexnow_interval_hours"])
    # 启停
    if "inspection_enabled" in updates:
        if config.inspection_enabled and config.is_within_time_range():
            dispatcher.resume_all()
        else:
            dispatcher.pause_all()
    return {"message": "配置已更新", "changed": result["changed"], "config": result["config"]}


# ========== 项目管理 API ==========
@app.get("/api/projects")
async def list_projects():
    projects = load_projects()
    return {"total": len(projects), "projects": projects}


@app.post("/api/projects")
async def create_project(req: ProjectRequest):
    projects = load_projects()
    if any(p["name"] == req.name for p in projects):
        raise HTTPException(400, "项目名称已存在")
    project = req.model_dump()
    projects.append(project)
    save_projects(projects)
    logger.info(f"[Project] 添加项目: {req.name}")
    return {"message": "项目已添加", "project": project}


@app.put("/api/projects/{name}")
async def update_project(name: str, req: ProjectRequest):
    projects = load_projects()
    found = False
    for i, p in enumerate(projects):
        if p["name"] == name:
            projects[i] = req.model_dump()
            if name != req.name:
                projects[i]["name"] = req.name
            found = True
            break
    if not found:
        raise HTTPException(404, "项目不存在")
    save_projects(projects)
    return {"message": "项目已更新", "project": projects[i]}


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    projects = load_projects()
    original_len = len(projects)
    projects = [p for p in projects if p["name"] != name]
    if len(projects) == original_len:
        raise HTTPException(404, "项目不存在")
    save_projects(projects)
    return {"message": f"项目 {name} 已删除"}


# ========== 节点管理 API ==========
@app.post("/api/node/register")
async def node_register(req: NodeRegisterRequest):
    node_info = req.model_dump(exclude_none=True)
    node = await node_manager.register_node(node_info)
    tasks = dispatcher.get_node_tasks(node["node_id"]) if dispatcher else []
    logger.info(f"[Node] 注册: {node['node_id']} ({node['name']})")
    return {"node": node, "tasks": tasks, "projects": load_projects()}


@app.post("/api/node/heartbeat")
async def node_heartbeat(req: NodeHeartbeatRequest):
    result = await node_manager.heartbeat(
        node_id=req.node_id,
        status=req.status,
        capabilities=req.capabilities,
        installed_packages=req.installed_packages,
        install_results=req.install_results,
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    tasks = dispatcher.get_node_tasks(req.node_id) if dispatcher else []
    # 节点恢复时自动恢复其 checker（builtin 实例由 dispatcher 管理，这里仅通知）
    config = RuntimeConfig.get_instance()
    return {
        "node": result["node"],
        "tasks": tasks,
        "install_commands": result.get("install_commands", []),
        "projects": load_projects(),
        "browser_concurrency": config.browser_concurrency,
    }


@app.get("/api/node/tasks")
async def node_tasks(node_id: str):
    if not node_manager.get_node(node_id):
        raise HTTPException(404, "节点未注册")
    tasks = dispatcher.get_node_tasks(node_id) if dispatcher else []
    return {"node_id": node_id, "tasks": tasks, "projects": load_projects()}


@app.post("/api/node/result")
async def node_result(req: NodeResultRequest):
    if not node_manager.get_node(req.node_id):
        raise HTTPException(404, "节点未注册")
    await dispatcher.report_result(req.node_id, req.checker_id, req.results)
    return {"message": "结果已接收", "count": len(req.results)}


@app.post("/api/node/install-command")
async def node_install_command(node_id: str, req: NodeInstallRequest):
    """面板下发安装指令"""
    node = node_manager.get_node(node_id)
    if not node:
        raise HTTPException(404, "节点不存在")
    if node.get("is_builtin"):
        raise HTTPException(400, "内置节点不支持远程安装")
    cmd = await node_manager.queue_install_command(
        node_id, req.command, req.package
    )
    return {"message": "安装指令已下发", "command": cmd}


@app.get("/api/nodes")
async def list_nodes():
    nodes = node_manager.list_nodes()
    # 附加每个节点的 checker 信息
    result = []
    for n in nodes:
        node_data = dict(n)
        node_data["checkers"] = dispatcher.get_node_checker_ids(n["node_id"]) if dispatcher else []
        result.append(node_data)
    return {"total": len(result), "nodes": result}


@app.put("/api/nodes/{node_id}")
async def update_node(node_id: str, req: NodeUpdateRequest):
    node = await node_manager.update_node(node_id, req.model_dump(exclude_none=True))
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"message": "节点已更新", "node": node}


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: str):
    if node_id == "builtin":
        raise HTTPException(400, "不能删除内置节点")
    success = await node_manager.unregister_node(node_id)
    if not success:
        raise HTTPException(404, "节点不存在")
    return {"message": f"节点 {node_id} 已删除"}


@app.get("/api/nodes/{node_id}/env")
async def get_node_env(node_id: str):
    node = node_manager.get_node(node_id)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {
        "node_id": node_id,
        "os": node.get("os"),
        "python_version": node.get("python_version"),
        "capabilities": node.get("capabilities", {}),
        "installed_packages": node.get("installed_packages", []),
        "install_results": node.get("install_results", []),
    }


@app.post("/api/nodes/{node_id}/install")
async def trigger_node_install(node_id: str, req: NodeInstallRequest):
    return await node_install_command(node_id, req)


# ========== Checker 管理 API ==========
@app.get("/api/checkers")
async def list_checkers():
    checkers = dispatcher.list_checkers() if dispatcher else []
    return {"total": len(checkers), "checkers": checkers}


@app.post("/api/checkers")
async def create_checker(req: CheckerCreateRequest):
    try:
        cfg = await dispatcher.add_checker(req.model_dump())
        return {"message": "检查器已创建", "checker": cfg}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/checkers/{checker_id}")
async def update_checker(checker_id: str, req: CheckerUpdateRequest):
    try:
        cfg = await dispatcher.update_checker(
            checker_id, req.model_dump(exclude_none=True)
        )
        if not cfg:
            raise HTTPException(404, "检查器不存在")
        return {"message": "检查器已更新", "checker": cfg}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/checkers/{checker_id}")
async def delete_checker(checker_id: str):
    success = await dispatcher.delete_checker(checker_id)
    if not success:
        raise HTTPException(404, "检查器不存在")
    return {"message": f"检查器 {checker_id} 已删除"}


@app.post("/api/checkers/{checker_id}/start")
async def start_checker(checker_id: str):
    await dispatcher.start_checker(checker_id)
    return {"message": "已启动"}


@app.post("/api/checkers/{checker_id}/stop")
async def stop_checker(checker_id: str):
    await dispatcher.stop_checker(checker_id)
    return {"message": "已停止"}


@app.post("/api/checkers/{checker_id}/pause")
async def pause_checker(checker_id: str):
    await dispatcher.pause_checker(checker_id)
    return {"message": "已暂停"}


@app.post("/api/checkers/{checker_id}/resume")
async def resume_checker(checker_id: str):
    await dispatcher.resume_checker(checker_id)
    return {"message": "已恢复"}


# ========== 本地访问结果（兼容旧版 local_visitor） ==========
@app.post("/api/local-visit-result")
async def save_local_visit_result(result: LocalVisitResult):
    result_dict = result.model_dump()
    if not result_dict.get("timestamp"):
        result_dict["timestamp"] = datetime.utcnow().isoformat() + "Z"
    await CheckerManager.save_local_visit_result(result_dict)
    return {"message": "已记录", "received": result_dict}


@app.get("/api/local-visit-stats")
async def get_local_visit_stats():
    return CheckerManager.get_local_visit_stats()


# ========== 日志 API ==========
@app.get("/api/logs")
async def get_logs(limit: int = 50):
    logs = CheckerManager.get_recent_logs()
    return {"total": len(logs), "logs": logs[-max(1, min(100, limit)):]}


# ========== IndexNow API ==========
@app.get("/api/indexnow/verify")
async def indexnow_verify():
    if not _indexnow_pusher:
        raise HTTPException(503, "IndexNow 未初始化")
    return _indexnow_pusher.get_verify_info()


@app.post("/api/indexnow/push")
async def indexnow_push():
    if not _indexnow_pusher:
        raise HTTPException(503, "IndexNow 未初始化")
    results = await _indexnow_pusher.push_all_engines()
    success = sum(1 for r in results if r["success"])
    return {"message": f"推送完成 {success}/{len(results)}", "results": results}


@app.get("/api/indexnow/history")
async def indexnow_history(limit: int = 20):
    if not _indexnow_pusher:
        raise HTTPException(503, "IndexNow 未初始化")
    return {"records": _indexnow_pusher.get_history(limit)}


@app.post("/api/indexnow/interval")
async def indexnow_set_interval(hours: int):
    if not _indexnow_pusher:
        raise HTTPException(503, "IndexNow 未初始化")
    result = await _indexnow_pusher.set_interval(hours)
    return result


# ========== 视频 API ==========
@app.get("/api/video-status")
async def get_video_status():
    await VideoCheckerManager.initialize()
    video_config = VideoConfig.get_instance()
    videos = video_config.get_videos()
    status = VideoCheckerManager.get_all_status()
    all_videos = {}
    for v in videos:
        base = status.get(v["name"], {
            "video_name": v["name"], "video_url": v["url"],
            "success": False, "error": "未开始",
        }).copy()
        base["name"] = v["name"]
        base["url"] = v["url"]
        base["play_count"] = v["play_count"]
        all_videos[v["name"]] = base
    return {
        "videos": all_videos,
        "sync_running": VideoCheckerManager.is_sync_running(),
        "async_running": VideoCheckerManager.is_async_running(),
    }


@app.get("/api/video-config")
async def get_video_config():
    vc = VideoConfig.get_instance()
    return {"videos": vc.get_videos(), "total": len(vc.get_videos())}


@app.post("/api/video-config")
async def update_video_config(req: VideoConfigBatchRequest):
    vc = VideoConfig.get_instance()
    if req.videos:
        for v in req.videos:
            await vc.add_video(v.name, v.url, v.play_count,
                               checker_ids=v.checker_ids,
                               keywords=v.keywords)
    if req.delete:
        for name in req.delete:
            await vc.delete_video(name)
    return {"message": "视频配置已更新", "videos": vc.get_videos()}


@app.post("/api/video-start")
async def start_video(req: VideoTypeRequest):
    await VideoCheckerManager.initialize()
    if req.type == "sync":
        await VideoCheckerManager.start_sync()
    else:
        await VideoCheckerManager.start_async()
    return {"message": f"{req.type} 视频访问已启动"}


@app.post("/api/video-stop")
async def stop_video(req: VideoTypeRequest):
    if req.type == "sync":
        await VideoCheckerManager.stop_sync()
    else:
        await VideoCheckerManager.stop_async()
    return {"message": f"{req.type} 视频访问已停止"}


# ========== 仪表盘统计 ==========
@app.get("/api/dashboard")
async def dashboard():
    projects = load_projects()
    nodes = node_manager.list_nodes()
    checkers = dispatcher.list_checkers() if dispatcher else []
    sync_latest = CheckerManager.get_all_status()
    summary = CheckerManager.get_summary()
    online_nodes = [n for n in nodes if n.get("status") == "online"]
    running_checkers = [c for c in checkers if c.get("runtime", {}).get("running")]

    # 服务器资源
    health = CheckerManager.get_health_info()

    # 最近检测结果
    recent = []
    for pname, result in sync_latest.items():
        recent.append(result)
    recent.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return {
        "stats": {
            "total_projects": len(projects),
            "online_projects": summary["online"],
            "offline_projects": summary["offline"],
            "slow_projects": summary["slow"],
            "total_nodes": len(nodes),
            "online_nodes": len(online_nodes),
            "total_checkers": len(checkers),
            "running_checkers": len(running_checkers),
        },
        "recent_results": recent[:10],
        "nodes": [{"node_id": n["node_id"], "name": n["name"],
                    "status": n["status"], "os": n.get("os", "")}
                   for n in nodes],
        "system": health,
    }


# ========== WebSocket ==========
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    await CheckerManager.add_ws_client(ws)
    config = RuntimeConfig.get_instance()
    await ws.send_json({"type": "config_update", "config": config.to_dict()})
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[WebSocket] 异常: {e}")
    finally:
        await CheckerManager.remove_ws_client(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8700, reload=False)
