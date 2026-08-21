"""配置管理 - 项目列表（动态加载）、节点/Checker配置、全局巡检配置"""
import os
import json
import random
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger("health_checker")

# ========== 路径常量 ==========
_this_dir = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_this_dir, "data")
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
CHECKERS_FILE = os.path.join(DATA_DIR, "checkers.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
RESULTS_FILE = os.path.join(DATA_DIR, "check_results.json")
VIDEO_CONFIG_FILE = os.path.join(DATA_DIR, "video_config.json")
VIDEO_RESULTS_FILE = os.path.join(DATA_DIR, "video_results.json")

# ========== 检查配置常量 ==========
REQUEST_TIMEOUT = 10
SLOW_THRESHOLD = 5
HISTORY_MAX_SIZE = 100

# 视频播放相关常量
VIDEO_REQUEST_TIMEOUT = 30
VIDEO_RANGE_BYTES = 524288
VIDEO_MIN_DELAY = 2
VIDEO_MAX_DELAY = 5

# ========== 默认项目列表（23个，首次启动写入 projects.json） ==========
DEFAULT_PROJECTS = [
    {"name": "智能工作台", "url": "https://www.zhinenti.cn", "category": "AI工具", "sub_paths": [], "is_spa": True},
    {"name": "部署助手", "url": "https://deploy.zhinenti.cn", "category": "AI工具", "sub_paths": [], "is_spa": True},
    {"name": "营销助手", "url": "https://craft.zhinenti.cn", "category": "AI工具", "sub_paths": [], "is_spa": True},
    {"name": "智能部署", "url": "https://auto.zhinenti.cn", "category": "AI工具", "sub_paths": [], "is_spa": True},
    {"name": "祝福生成", "url": "https://www.zhinenti.vip", "category": "工具", "sub_paths": ["/", "/#generate"]},
    {"name": "AI文案", "url": "https://www.zhinenti.xyz", "category": "AI工具", "sub_paths": ["/", "/#generate"]},
    {"name": "起名工具", "url": "https://www.hpenn.online", "category": "AI工具", "sub_paths": ["/", "/features", "/login"]},
    {"name": "文本工具", "url": "https://www.hpenn.xyz", "category": "工具", "sub_paths": ["/", "/text-dedup", "/word-count", "/case-convert", "/text-diff", "/text-replace", "/text-sort"]},
    {"name": "OCR识别", "url": "https://ocr.hpenn.xyz", "category": "工具", "sub_paths": ["/"]},
    {"name": "AI简历", "url": "https://resume.hpenn.xyz", "category": "工具", "sub_paths": ["/", "/#optimize"]},
    {"name": "文档摘要", "url": "https://doc.hpenn.xyz", "category": "工具", "sub_paths": ["/", "/#summarize"]},
    {"name": "文档转换", "url": "https://convert.hpenn.xyz", "category": "工具", "sub_paths": ["/"]},
    {"name": "批量图片编辑", "url": "https://imgedit.hpenn.xyz", "category": "工具", "sub_paths": ["/", "/#compress", "/#resize", "/#watermark", "/#format"]},
    {"name": "图片尺寸", "url": "https://imgsize.hpenn.xyz", "category": "工具", "sub_paths": ["/"]},
    {"name": "表格工具", "url": "https://table.hpenn.xyz", "category": "工具", "sub_paths": ["/edit", "/csv", "/xlsx", "/login", "/register"]},
    {"name": "开发工具", "url": "https://www.hpennn.xyz", "category": "工具", "sub_paths": ["/json-formatter", "/base64", "/regex-tester", "/color-converter", "/timestamp", "/hash"]},
    {"name": "导航站", "url": "https://www.hpennn.online", "category": "工具", "sub_paths": ["/category", "/submit"]},
    {"name": "智能搜索", "url": "https://smart.hpennn.xyz", "category": "工具", "sub_paths": ["/generate", "/copy", "/publish", "/#workflow"]},
    {"name": "PPT工具", "url": "https://ppt.hpennn.xyz", "category": "工具", "sub_paths": ["/ppt", "/report"]},
    {"name": "内容生成", "url": "https://content.hpennn.xyz", "category": "AI工具", "sub_paths": ["/xiaohongshu", "/rewrite", "/scoring", "/video-script", "/sensitive"]},
    {"name": "图片工具", "url": "https://www.kuaisutupo.xyz", "category": "工具", "sub_paths": ["/convert", "/compress", "/crop", "/watermark", "/splice", "/info"]},
    {"name": "快速工具", "url": "https://www.kuaisu.online", "category": "工具", "sub_paths": ["/mortgage-calculator", "/unit-converter", "/tax-calculator", "/date-calculator", "/bmi-calculator", "/exchange-rate"]},
    {"name": "AI客服管理", "url": "http://47.113.216.237:8600", "category": "AI工具", "sub_paths": [], "is_spa": True},
]

# ========== 默认 User-Agent 池（用于 sync checker） ==========
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

# ========== 默认 builtin 节点 ==========
DEFAULT_BUILTIN_NODE = {
    "node_id": "builtin",
    "name": "服务器内置节点",
    "ip": "127.0.0.1",
    "os": "Linux (Docker)",
    "python_version": "3.11",
    "status": "online",
    "last_heartbeat": datetime.now().isoformat(),
    "capabilities": {
        "has_browser": False,
        "playwright_version": None,
        "chromium_installed": False,
    },
    "registered_at": datetime.now().isoformat(),
    "is_builtin": True,
}

# ========== 默认 sync checker ==========
DEFAULT_CHECKERS = [
    {
        "id": "chk-sync-001",
        "name": "主HTTP深度检测",
        "type": "sync",
        "node_id": "builtin",
        "enabled": True,
        "interval_min": 5,
        "interval_max": 15,
        "projects": [],
        "config": {
            "user_agent": DEFAULT_USER_AGENTS[0],
            "deep_inspect": True,
        },
    },
]


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载 {path} 失败: {e}，使用默认值")
    return default


def _save_json(path: str, data):
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ========== 项目管理 ==========
def load_projects() -> list[dict]:
    """从 projects.json 加载项目列表，首次启动写入默认23个项目"""
    _ensure_data_dir()
    if not os.path.exists(PROJECTS_FILE):
        _save_json(PROJECTS_FILE, DEFAULT_PROJECTS)
        logger.info(f"[Config] 已写入默认 {len(DEFAULT_PROJECTS)} 个项目到 {PROJECTS_FILE}")
        return list(DEFAULT_PROJECTS)
    return _load_json(PROJECTS_FILE, list(DEFAULT_PROJECTS))


def save_projects(projects: list[dict]):
    _save_json(PROJECTS_FILE, projects)


def get_project_by_name(name: str) -> dict | None:
    projects = load_projects()
    for p in projects:
        if p["name"] == name:
            return p
    return None


# ========== 节点管理（轻量封装，NodeManager 有更完整实现） ==========
def load_nodes() -> list[dict]:
    _ensure_data_dir()
    if not os.path.exists(NODES_FILE):
        _save_json(NODES_FILE, [DEFAULT_BUILTIN_NODE])
        return [dict(DEFAULT_BUILTIN_NODE)]
    nodes = _load_json(NODES_FILE, [])
    # 确保 builtin 节点存在
    if not any(n.get("node_id") == "builtin" for n in nodes):
        nodes.insert(0, dict(DEFAULT_BUILTIN_NODE))
        _save_json(NODES_FILE, nodes)
    return nodes


def save_nodes(nodes: list[dict]):
    _save_json(NODES_FILE, nodes)


# ========== Checker 配置管理 ==========
def load_checkers_config() -> list[dict]:
    _ensure_data_dir()
    if not os.path.exists(CHECKERS_FILE):
        _save_json(CHECKERS_FILE, DEFAULT_CHECKERS)
        return [dict(c) for c in DEFAULT_CHECKERS]
    return _load_json(CHECKERS_FILE, [])


def save_checkers_config(checkers: list[dict]):
    _save_json(CHECKERS_FILE, checkers)


# ========== 工具函数 ==========
def get_random_interval(min_sec: float = 30, max_sec: float = 120) -> float:
    return random.uniform(min_sec, max_sec)


def _default_search_keywords() -> dict:
    """为每个项目生成默认搜索关键词"""
    keyword_presets = {
        "智能工作台": ["AI办公工具", "智能体工作台", "AI工作台"],
        "部署助手": ["一键部署", "自动化部署", "AI部署"],
        "营销助手": ["AI营销", "营销文案生成", "智能营销"],
        "智能部署": ["自动部署工具", "智能部署系统", "一键部署工具"],
        "AI客服管理": ["AI客服", "智能客服系统", "客服管理系统"],
        "起名工具": ["AI起名", "智能起名", "在线起名"],
        "导航站": ["AI工具导航", "工具导航站", "AI导航"],
        "OCR识别": ["OCR文字识别", "图片转文字", "在线OCR"],
        "AI简历": ["AI简历生成", "智能简历", "简历生成器"],
        "图片工具": ["图片处理工具", "在线图片编辑", "AI图片工具"],
        "文档摘要": ["文档摘要生成", "AI文档总结", "长文摘要"],
        "文档转换": ["文档格式转换", "PDF转换", "在线文档转换"],
        "文本工具": ["文本处理工具", "在线文本工具", "文字工具"],
        "开发工具": ["开发者工具", "在线开发工具", "编程工具"],
        "祝福生成": ["祝福语生成", "AI祝福", "生日祝福"],
        "AI文案": ["AI文案生成", "智能文案", "文案写作工具"],
        "批量图片编辑": ["批量图片处理", "图片批量编辑", "批量修图"],
    }
    projects = load_projects()
    keywords_map = {}
    for p in projects:
        keywords_map[p["name"]] = keyword_presets.get(p["name"], [p["name"], p["name"] + " 官网"])
    return keywords_map


# ========== 运行时全局配置 ==========
class RuntimeConfig:
    """运行时配置管理 - 支持持久化到 config.json，动态热更新"""
    _instance = None
    _lock = asyncio.Lock()

    def __init__(self):
        self.inspection_enabled = True
        self.time_range = {"start": "00:00", "end": "23:59"}
        self.interval_min = 30
        self.interval_max = 30
        self.rounds_min = 1
        self.rounds_max = 1
        self.rounds_interval_seconds = 3
        self.total_inspections_min = 0
        self.total_inspections_max = 0
        self.search_engine = "baidu"
        self.project_search_keywords = _default_search_keywords()
        self.visitor_interval_min = 20
        self.visitor_interval_max = 45
        self.default_visit_count = 5
        projects = load_projects()
        self.project_visit_counts = {p["name"]: self.default_visit_count for p in projects}
        self.indexnow_interval_hours = 24
        self.indexnow_key = ""
        self._listeners = []

    @classmethod
    def get_instance(cls) -> "RuntimeConfig":
        if cls._instance is None:
            cls._instance = RuntimeConfig()
        return cls._instance

    def register_listener(self, callback):
        self._listeners.append(callback)

    def unregister_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def _notify_listeners(self, changed_keys: set):
        for cb in self._listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(changed_keys)
                else:
                    cb(changed_keys)
            except Exception as e:
                logger.error(f"[Config] 监听器回调异常: {e}")

    async def load(self):
        """从 config.json 加载配置"""
        data = _load_json(CONFIG_FILE, {})
        if not data:
            return
        for key in (
            "inspection_enabled", "time_range", "interval_min", "interval_max",
            "rounds_min", "rounds_max", "rounds_interval_seconds",
            "total_inspections_min", "total_inspections_max",
            "search_engine", "project_search_keywords",
            "visitor_interval_min", "visitor_interval_max",
            "default_visit_count", "project_visit_counts",
            "indexnow_interval_hours", "indexnow_key",
        ):
            if key in data:
                setattr(self, key, data[key])
        logger.info(f"[Config] 已加载运行时配置")

    def save(self):
        _save_json(CONFIG_FILE, self.to_dict())

    def to_dict(self) -> dict:
        return {
            "inspection_enabled": self.inspection_enabled,
            "time_range": self.time_range,
            "interval_min": self.interval_min,
            "interval_max": self.interval_max,
            "rounds_min": self.rounds_min,
            "rounds_max": self.rounds_max,
            "rounds_interval_seconds": self.rounds_interval_seconds,
            "total_inspections_min": self.total_inspections_min,
            "total_inspections_max": self.total_inspections_max,
            "search_engine": self.search_engine,
            "project_search_keywords": self.project_search_keywords,
            "visitor_interval_min": self.visitor_interval_min,
            "visitor_interval_max": self.visitor_interval_max,
            "default_visit_count": self.default_visit_count,
            "project_visit_counts": self.project_visit_counts,
            "indexnow_interval_hours": self.indexnow_interval_hours,
            "indexnow_key": self.indexnow_key,
        }

    async def update(self, updates: dict) -> dict:
        changed = set()
        for key, value in updates.items():
            if hasattr(self, key) and getattr(self, key) != value:
                setattr(self, key, value)
                changed.add(key)
        if changed:
            self.save()
            await self._notify_listeners(changed)
        return {"changed": list(changed), "config": self.to_dict()}

    def is_within_time_range(self) -> bool:
        now = datetime.now().strftime("%H:%M")
        return self.time_range["start"] <= now <= self.time_range["end"]

    def get_interval_seconds(self) -> float:
        return random.uniform(self.interval_min, self.interval_max) * 60

    def get_project_visit_count(self, project_name: str) -> int:
        return self.project_visit_counts.get(project_name, self.default_visit_count)

    def set_project_visit_count(self, project_name: str, count: int):
        self.project_visit_counts[project_name] = count


# ========== 视频配置 ==========
class VideoConfig:
    _instance = None

    def __init__(self):
        self.videos: list[dict] = []

    @classmethod
    def get_instance(cls) -> "VideoConfig":
        if cls._instance is None:
            cls._instance = VideoConfig()
        return cls._instance

    async def load(self):
        self.videos = _load_json(VIDEO_CONFIG_FILE, [])

    def save(self):
        _save_json(VIDEO_CONFIG_FILE, self.videos)

    def get_videos(self) -> list[dict]:
        return self.videos

    async def add_video(self, name: str, url: str, play_count: int = 5) -> dict:
        for v in self.videos:
            if v["name"] == name:
                v["url"] = url
                v["play_count"] = play_count
                self.save()
                return {"action": "updated", "video": v}
        video = {"name": name, "url": url, "play_count": play_count}
        self.videos.append(video)
        self.save()
        return {"action": "added", "video": video}

    async def delete_video(self, name: str) -> bool:
        before = len(self.videos)
        self.videos = [v for v in self.videos if v["name"] != name]
        if len(self.videos) < before:
            self.save()
            return True
        return False
