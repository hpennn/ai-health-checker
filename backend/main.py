"""FastAPI 主服务 - 端口 8700"""
import os
import sys
from contextlib import asynccontextmanager

# 确保 backend 目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from checker import CheckerManager
from config import PROJECTS

# 前端 HTML 文件路径
FRONTEND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "frontend",
    "dashboard.html"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 - 启动/停止 Checker"""
    # 启动所有 Checker
    await CheckerManager.initialize()
    await CheckerManager.start_all()
    print("所有 Checker 已启动")
    yield
    # 停止所有 Checker
    await CheckerManager.stop_all()
    print("所有 Checker 已停止")


app = FastAPI(
    title="AI Health Checker",
    description="多站点健康检测系统 - 10个Checker子Agent模拟不同IP自动检测",
    version="1.0.0",
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


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """返回监控面板"""
    if os.path.exists(FRONTEND_PATH):
        with open(FRONTEND_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Loading...</h1>")


@app.get("/api/status")
async def get_all_status():
    """获取所有项目当前状态"""
    status = CheckerManager.get_all_status()
    summary = CheckerManager.get_summary()

    # 补充未检测的项目
    all_projects = {}
    for p in PROJECTS:
        all_projects[p["name"]] = status.get(
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

    return {
        "summary": summary,
        "projects": all_projects,
    }


@app.get("/api/status/{project_name}")
async def get_project_status(project_name: str):
    """获取单个项目详情和历史"""
    from config import get_project_by_name

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
    """立即触发检查某个项目"""
    from config import get_project_by_name

    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    result = await CheckerManager.check_project_now(project_name)
    if not result:
        raise HTTPException(status_code=500, detail="检查失败")

    return {"message": f"已完成 {project_name} 的检查", "result": result}


@app.post("/api/check-all")
async def check_all_now():
    """立即触发全部检查"""
    results = await CheckerManager.check_all_now()
    return {
        "message": f"已完成 {len(results)} 个项目的检查",
        "count": len(results),
        "results": results,
    }


@app.get("/api/history")
async def get_history():
    """获取检查历史（最近100条）"""
    history = CheckerManager.get_all_history()
    return {"total": len(history), "records": history}


@app.get("/api/agents")
async def get_agents():
    """获取10个Checker的运行状态"""
    agents = CheckerManager.get_checkers_status()
    return {"total": len(agents), "agents": agents}


@app.get("/api/health")
async def health_check():
    """服务健康检查"""
    return {"status": "ok", "service": "ai-health-checker"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8700, reload=False)
