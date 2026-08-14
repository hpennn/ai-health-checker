"""FastAPI 主服务 - 端口 8700
支持：10个同步Checker + N个异步Checker（搜索词模式）、巡检控制、时间段、动态配置、WebSocket推送
"""
import os
import sys
import asyncio
import logging
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# 确保 backend 目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import CheckerManager
from async_checker import AsyncCheckerManager
from config import PROJECTS, RuntimeConfig, get_project_by_name

logger = logging.getLogger("health_checker")

# 前端 HTML 文件路径
FRONTEND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "frontend",
    "dashboard.html"
)

# 版本号
APP_VERSION = "2.1.0"


# ========== 请求模型（带严格校验） ==========
class ControlRequest(BaseModel):
    action: str  # start / stop

    @field_validator("action")
    @classmethod
    def validate_action(cls, v):
        if v not in ("start", "stop"):
            raise ValueError("action 必须是 start 或 stop")
        return v


class TimeRange(BaseModel):
    start: str = Field(default="00:00", description="开始时间 HH:MM 格式")
    end: str = Field(default="23:59", description="结束时间 HH:MM 格式")

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v):
        if not re.match(r'^\d{2}:\d{2}$', v):
            raise ValueError("时间格式必须为 HH:MM")
        h, m = v.split(":")
        if int(h) > 23 or int(m) > 59:
            raise ValueError("时间值不合法")
        return v


class ConfigUpdateRequest(BaseModel):
    inspection_enabled: bool | None = None
    time_range: TimeRange | None = None
    interval_min: int | None = Field(default=None, ge=1, le=1440)
    interval_max: int | None = Field(default=None, ge=1, le=1440)
    rounds_min: int | None = Field(default=None, ge=1, le=10)
    rounds_max: int | None = Field(default=None, ge=1, le=10)
    rounds_interval_seconds: int | None = Field(default=None, ge=1, le=60)
    total_inspections_min: int | None = Field(default=None, ge=0, le=100)
    total_inspections_max: int | None = Field(default=None, ge=0, le=100)
    async_checker_count: int | None = Field(default=None, ge=0, le=30)
    search_engine: str | None = None
    project_search_keywords: dict | None = None
    # 向后兼容：旧字段
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    rounds_per_inspection: int | None = Field(default=None, ge=1, le=10)

    @field_validator("search_engine")
    @classmethod
    def validate_search_engine(cls, v):
        if v is not None:
            se = v.lower()
            if se not in ("baidu", "bing", "google"):
                raise ValueError("搜索引擎必须是 baidu、bing 或 google")
            return se
        return v

    @field_validator("project_search_keywords")
    @classmethod
    def validate_keywords(cls, v):
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError("project_search_keywords 必须是字典")
            for pname, kws in v.items():
                if not isinstance(kws, list):
                    raise ValueError(f"项目 {pname} 的关键词必须是列表")
                if len(kws) < 1 or len(kws) > 3:
                    raise ValueError(f"项目 {pname} 的关键词数量必须在 1-3 之间")
                for kw in kws:
                    if not isinstance(kw, str) or not kw.strip():
                        raise ValueError(f"项目 {pname} 的关键词不能为空")
        return v


# ========== 全局任务 ==========
_time_range_task = None


async def time_range_monitor():
    """时间段监控任务 - 根据时间段自动启停巡检"""
    config = RuntimeConfig.get_instance()
    last_state = None
    while True:
        try:
            now_in_range = config.is_within_time_range()
            # 只有用户启用了总开关时才受时间段控制
            target_paused = not (config.inspection_enabled and now_in_range)

            if last_state is None or target_paused != last_state:
                if target_paused:
                    CheckerManager.pause_all()
                    AsyncCheckerManager.pause_all()
                    logger.info("[TimeRange] 巡检已暂停（当前不在巡检时间段内或总开关关闭）")
                else:
                    CheckerManager.resume_all()
                    AsyncCheckerManager.resume_all()
                    logger.info("[TimeRange] 巡检已恢复（进入巡检时间段）")
                await CheckerManager.ws_broadcast_control("pause" if target_paused else "resume")
                last_state = target_paused
        except Exception as e:
            logger.error(f"[TimeRange] 监控异常: {e}")

        await asyncio.sleep(30)  # 每30秒检查一次


async def on_config_change(changed_keys: set):
    """配置变更回调"""
    logger.info(f"[Config] 配置变更: {changed_keys}")

    # 异步Checker数量变更
    if "async_checker_count" in changed_keys:
        config = RuntimeConfig.get_instance()
        await AsyncCheckerManager.set_count(config.async_checker_count)

    # 推送配置变更
    config = RuntimeConfig.get_instance()
    await CheckerManager.ws_broadcast_config(config.to_dict())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 加载配置
    config = RuntimeConfig.get_instance()
    await config.load()
    config.register_listener(on_config_change)

    # 启动同步Checker
    await CheckerManager.initialize()
    await CheckerManager.start_all()

    # 启动异步Checker
    await AsyncCheckerManager.initialize()
    await AsyncCheckerManager.start_all()

    # 根据初始配置和时间段决定是否暂停
    if not config.inspection_enabled or not config.is_within_time_range():
        CheckerManager.pause_all()
        AsyncCheckerManager.pause_all()
        logger.info("[Lifespan] 初始状态：巡检暂停（总开关关闭或不在时间段内）")
    else:
        logger.info("[Lifespan] 初始状态：巡检运行中")

    # 启动时间段监控
    global _time_range_task
    _time_range_task = asyncio.create_task(time_range_monitor())

    logger.info(f"AI Health Checker v{APP_VERSION} 启动完成")
    yield

    # 清理
    if _time_range_task:
        _time_range_task.cancel()
    await CheckerManager.stop_all()
    await AsyncCheckerManager.stop_all()
    logger.info("所有 Checker 已停止，服务关闭")


app = FastAPI(
    title="AI Health Checker",
    description="多站点健康检测系统 - 同步Checker + 异步搜索Checker",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 前端页面 ==========
@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    if os.path.exists(FRONTEND_PATH):
        with open(FRONTEND_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Loading...</h1>")


# ========== 健康检查 ==========
@app.get("/api/health")
async def health_check():
    """健康检查端点 - 返回服务运行状态、内存使用、运行时长等"""
    try:
        health_info = CheckerManager.get_health_info()
        # 补充异步Checker信息
        health_info["checkers"]["async_total"] = AsyncCheckerManager.get_count()
        health_info["checkers"]["async_running"] = AsyncCheckerManager.get_running_count()
        return health_info
    except ImportError:
        # psutil 未安装时返回简化信息
        config = RuntimeConfig.get_instance()
        return {
            "status": "ok",
            "service": "ai-health-checker",
            "version": APP_VERSION,
            "uptime_seconds": None,
            "memory": None,
            "projects": {
                "total": len(PROJECTS),
            },
            "checkers": {
                "sync_total": 10,
                "async_total": AsyncCheckerManager.get_count(),
            },
            "inspection_enabled": config.inspection_enabled,
        }


# ========== 状态 API ==========
@app.get("/api/status")
async def get_all_status():
    """获取所有项目当前状态"""
    status = CheckerManager.get_all_status()
    async_status = CheckerManager.get_async_status()
    summary = CheckerManager.get_summary()
    config = RuntimeConfig.get_instance()

    all_projects = {}
    for p in PROJECTS:
        base = status.get(
            p["name"],
            {
                "project_name": p["name"],
                "project_url": p["url"],
                "category": p["category"],
                "status": "pending",
                "status_code": None,
                "response_time_ms": None,
                "checker_id": None,
                "timestamp": None,
            },
        )
        all_projects[p["name"]] = base

    return {
        "summary": summary,
        "projects": all_projects,
        "async_projects": async_status,
        "inspection_enabled": config.inspection_enabled,
        "within_time_range": config.is_within_time_range(),
        "inspection_stats": CheckerManager.get_inspection_stats(),
        "checker_workload": CheckerManager.get_checker_workload(),
    }


@app.get("/api/status/{project_name}")
async def get_project_status(project_name: str):
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    all_status = CheckerManager.get_all_status()
    latest = all_status.get(project_name)
    history = CheckerManager.get_project_history(project_name)

    return {
        "project": project,
        "latest": latest,
        "history": history,
    }


@app.post("/api/check/{project_name}")
async def check_project_now(project_name: str):
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    result = await CheckerManager.check_project_now(project_name)
    if not result:
        raise HTTPException(status_code=500, detail="检查失败")

    return {"message": f"已完成 {project_name} 的检查", "result": result}


@app.post("/api/check-all")
async def check_all_now():
    results = await CheckerManager.check_all_now()
    return {
        "message": f"已完成 {len(results)} 个项目的检查",
        "count": len(results),
        "results": results,
    }


@app.get("/api/history")
async def get_history():
    history = CheckerManager.get_all_history()
    return {"total": len(history), "records": history}


@app.get("/api/agents")
async def get_agents():
    agents = CheckerManager.get_checkers_status()
    return {"total": len(agents), "agents": agents}


# ========== 实时日志 API ==========
@app.get("/api/logs")
async def get_recent_logs(limit: int = 50):
    """获取最近的巡检日志"""
    logs = CheckerManager.get_recent_logs()
    limit = max(1, min(100, limit))
    return {"total": len(logs), "logs": logs[-limit:]}


# ========== 巡检控制 API ==========
@app.post("/api/control")
async def control_inspection(req: ControlRequest):
    """启停巡检总开关"""
    config = RuntimeConfig.get_instance()

    if req.action == "start":
        result = await config.update({"inspection_enabled": True})
        # 只有同时在时间段内才真正恢复
        if config.is_within_time_range():
            CheckerManager.resume_all()
            AsyncCheckerManager.resume_all()
        await CheckerManager.ws_broadcast_control("start")
        logger.info("[Control] 巡检已启动")
        return {"message": "巡检已启动", "enabled": True, "config": config.to_dict()}

    elif req.action == "stop":
        result = await config.update({"inspection_enabled": False})
        CheckerManager.pause_all()
        AsyncCheckerManager.pause_all()
        await CheckerManager.ws_broadcast_control("stop")
        logger.info("[Control] 巡检已停止")
        return {"message": "巡检已停止", "enabled": False, "config": config.to_dict()}


# ========== 配置 API ==========
@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    config = RuntimeConfig.get_instance()
    return config.to_dict()


@app.get("/api/config/default")
async def get_default_config():
    """获取默认配置（用于恢复默认）"""
    # 创建一个临时实例获取默认值
    default_config = RuntimeConfig()
    return default_config.to_dict()


@app.post("/api/config/reset")
async def reset_config():
    """恢复默认配置"""
    config = RuntimeConfig.get_instance()
    default_config = RuntimeConfig()
    default_dict = default_config.to_dict()

    result = await config.update(default_dict)

    # 处理启停
    if "inspection_enabled" in result["changed"]:
        if config.inspection_enabled and config.is_within_time_range():
            CheckerManager.resume_all()
            AsyncCheckerManager.resume_all()
        elif not config.inspection_enabled:
            CheckerManager.pause_all()
            AsyncCheckerManager.pause_all()

    # 时间段变更后检查
    if "time_range" in result["changed"]:
        if config.inspection_enabled:
            if config.is_within_time_range():
                CheckerManager.resume_all()
                AsyncCheckerManager.resume_all()
            else:
                CheckerManager.pause_all()
                AsyncCheckerManager.pause_all()

    logger.info("[Config] 配置已恢复为默认值")
    return {
        "message": "配置已恢复为默认值",
        "changed": result["changed"],
        "config": result["config"],
    }


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    """更新配置（带严格校验）"""
    config = RuntimeConfig.get_instance()
    update_dict = {}

    if req.inspection_enabled is not None:
        update_dict["inspection_enabled"] = req.inspection_enabled

    if req.time_range is not None:
        update_dict["time_range"] = {"start": req.time_range.start, "end": req.time_range.end}

    if req.interval_min is not None:
        update_dict["interval_min"] = req.interval_min

    if req.interval_max is not None:
        update_dict["interval_max"] = req.interval_max

    if req.interval_minutes is not None:
        update_dict["interval_minutes"] = req.interval_minutes

    if req.rounds_min is not None:
        update_dict["rounds_min"] = req.rounds_min

    if req.rounds_max is not None:
        update_dict["rounds_max"] = req.rounds_max

    if req.rounds_per_inspection is not None:
        update_dict["rounds_per_inspection"] = req.rounds_per_inspection

    if req.rounds_interval_seconds is not None:
        update_dict["rounds_interval_seconds"] = req.rounds_interval_seconds

    if req.total_inspections_min is not None:
        update_dict["total_inspections_min"] = req.total_inspections_min

    if req.total_inspections_max is not None:
        update_dict["total_inspections_max"] = req.total_inspections_max

    if req.async_checker_count is not None:
        update_dict["async_checker_count"] = req.async_checker_count

    if req.search_engine is not None:
        update_dict["search_engine"] = req.search_engine

    if req.project_search_keywords is not None:
        update_dict["project_search_keywords"] = req.project_search_keywords

    if not update_dict:
        return {"message": "无配置更新", "config": config.to_dict(), "changed": []}

    result = await config.update(update_dict)

    # 处理启停
    if "inspection_enabled" in result["changed"]:
        if config.inspection_enabled and config.is_within_time_range():
            CheckerManager.resume_all()
            AsyncCheckerManager.resume_all()
        elif not config.inspection_enabled:
            CheckerManager.pause_all()
            AsyncCheckerManager.pause_all()

    # 时间段变更后检查
    if "time_range" in result["changed"]:
        if config.inspection_enabled:
            if config.is_within_time_range():
                CheckerManager.resume_all()
                AsyncCheckerManager.resume_all()
            else:
                CheckerManager.pause_all()
                AsyncCheckerManager.pause_all()

    logger.info(f"[Config] 配置更新完成，变更项: {result['changed']}")

    return {
        "message": "配置已更新",
        "changed": result["changed"],
        "config": result["config"],
    }


# ========== 异步 Checker API ==========
@app.get("/api/async-checkers")
async def get_async_checkers():
    """获取异步Checker状态"""
    checkers = AsyncCheckerManager.get_checkers_status()
    async_status = CheckerManager.get_async_status()
    return {
        "total": len(checkers),
        "checkers": checkers,
        "latest": async_status,
    }


# ========== WebSocket ==========
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await CheckerManager.add_ws_client(websocket)

    # 初始推送当前配置和状态
    config = RuntimeConfig.get_instance()
    await websocket.send_json({
        "type": "config_update",
        "config": config.to_dict(),
    })

    # 推送初始日志
    logs = CheckerManager.get_recent_logs()
    await websocket.send_json({
        "type": "log_init",
        "logs": logs[-20:],
    })

    try:
        while True:
            # 接收客户端消息（心跳等）
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[WebSocket] 异常: {e}")
    finally:
        await CheckerManager.remove_ws_client(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8700, reload=False)
