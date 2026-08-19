"""配置管理 - 项目列表、检查间隔、Checker身份、全局巡检配置"""
import os
import json
import random
import asyncio
from datetime import datetime

# ========== 监控项目列表 ==========
# sub_paths: 已知子页面路径列表，用于 Visitor 模拟访问内页的兜底补充
PROJECTS = [
    # === zhinenti.cn 系列 ===
    {
        "name": "智能工作台", "url": "https://www.zhinenti.cn", "category": "AI工具", "visit_count": 5,
        "sub_paths": [],  # SPA 站点，客户端路由，HTTP 访问子路径返回 404
        "is_spa": True,
    },
    {
        "name": "部署助手", "url": "https://deploy.zhinenti.cn", "category": "AI工具", "visit_count": 5,
        "sub_paths": [],  # SPA 站点，客户端路由，HTTP 访问子路径返回 404
        "is_spa": True,
    },
    {
        "name": "营销助手", "url": "https://craft.zhinenti.cn", "category": "AI工具", "visit_count": 5,
        "sub_paths": [],  # SPA 站点，客户端路由，HTTP 访问子路径返回 404
        "is_spa": True,
    },
    {
        "name": "智能部署", "url": "https://auto.zhinenti.cn", "category": "AI工具", "visit_count": 5,
        "sub_paths": [],  # SPA 站点，客户端路由，HTTP 访问子路径返回 404
        "is_spa": True,
    },
    # === zhinenti.vip / xyz ===
    {
        "name": "祝福生成", "url": "https://www.zhinenti.vip", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/#generate"],
    },
    {
        "name": "AI文案", "url": "https://www.zhinenti.xyz", "category": "AI工具", "visit_count": 5,
        "sub_paths": ["/", "/#generate"],
    },
    # === hpenn.online ===
    {
        "name": "起名工具", "url": "https://www.hpenn.online", "category": "AI工具", "visit_count": 5,
        "sub_paths": ["/", "/features", "/login"],
    },
    # === hpenn.xyz 系列 ===
    {
        "name": "文本工具", "url": "https://www.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/text-dedup", "/word-count", "/case-convert", "/text-diff", "/text-replace", "/text-sort"],
    },
    {
        "name": "OCR识别", "url": "https://ocr.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/"],
    },
    {
        "name": "AI简历", "url": "https://resume.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/#optimize"],
    },
    {
        "name": "文档摘要", "url": "https://doc.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/#summarize"],
    },
    {
        "name": "文档转换", "url": "https://convert.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/"],
    },
    {
        "name": "批量图片编辑", "url": "https://imgedit.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/#compress", "/#resize", "/#watermark", "/#format"],
    },
    {
        "name": "图片尺寸", "url": "https://imgsize.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/"],
    },
    {
        "name": "表格工具", "url": "https://table.hpenn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/edit", "/csv", "/xlsx", "/login", "/register"],
    },
    # === hpennn.xyz 系列 ===
    {
        "name": "开发工具", "url": "https://www.hpennn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/json-formatter", "/base64", "/regex-tester", "/color-converter", "/timestamp", "/hash"],
    },
    {
        "name": "导航站", "url": "https://www.hpennn.online", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/category", "/submit"],
    },
    {
        "name": "智能搜索", "url": "https://smart.hpennn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/generate", "/copy", "/publish", "/#workflow"],
    },
    {
        "name": "PPT工具", "url": "https://ppt.hpennn.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/ppt", "/report"],
    },
    {
        "name": "内容生成", "url": "https://content.hpennn.xyz", "category": "AI工具", "visit_count": 5,
        "sub_paths": ["/", "/xiaohongshu", "/rewrite", "/scoring", "/video-script", "/sensitive"],
    },
    # === kuaisu 系列 ===
    {
        "name": "图片工具", "url": "https://www.kuaisutupo.xyz", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/convert", "/compress", "/crop", "/watermark", "/splice", "/info"],
    },
    {
        "name": "快速工具", "url": "https://www.kuaisu.online", "category": "工具", "visit_count": 5,
        "sub_paths": ["/", "/mortgage-calculator", "/unit-converter", "/tax-calculator", "/date-calculator", "/bmi-calculator", "/exchange-rate"],
    },
    # === IP直连（不推送IndexNow） ===
    {
        "name": "AI客服管理", "url": "http://47.113.216.237:8600", "category": "AI工具", "visit_count": 5,
        "sub_paths": [],  # SPA 站点，客户端路由，HTTP 访问子路径返回 404
        "is_spa": True,
    },
]

# ========== 检查配置（基础常量） ==========
REQUEST_TIMEOUT = 10      # 请求超时时间（秒）
SLOW_THRESHOLD = 5        # 慢响应阈值（秒）
HISTORY_MAX_SIZE = 100    # 每个项目历史记录最大条数
_this_dir = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_this_dir, "data")
RESULTS_FILE = os.path.join(DATA_DIR, "check_results.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# ========== 20个 Checker 的独立身份 ==========
CHECKER_IDENTITIES = [
    {
        "id": 1, "name": "Chrome-Win10",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ip_pool": ["203.0.113.15", "203.0.113.42", "198.51.100.77", "192.0.2.123", "203.0.113.88"],
        "type": "desktop",
    },
    {
        "id": 2, "name": "Firefox-Win11",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "ip_pool": ["198.51.100.23", "198.51.100.56", "203.0.113.101", "192.0.2.200", "198.51.100.150"],
        "type": "desktop",
    },
    {
        "id": 3, "name": "Safari-macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "ip_pool": ["104.28.15.67", "104.28.16.89", "172.67.180.12", "104.21.45.231", "172.67.132.45"],
        "type": "desktop",
    },
    {
        "id": 4, "name": "Edge-Win10",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "ip_pool": ["52.14.87.231", "18.190.123.45", "35.186.200.88", "104.154.89.67", "130.211.45.123"],
        "type": "desktop",
    },
    {
        "id": 5, "name": "Chrome-iPhone",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.118 Mobile/15E148 Safari/604.1",
        "ip_pool": ["17.58.96.0", "17.58.100.45", "17.173.255.12", "17.248.128.67", "17.170.80.200"],
        "type": "mobile",
    },
    {
        "id": 6, "name": "Safari-iPad",
        "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "ip_pool": ["17.110.224.0", "17.110.230.56", "17.255.128.34", "17.200.80.120", "17.150.45.78"],
        "type": "tablet",
    },
    {
        "id": 7, "name": "Chrome-Android",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.119 Mobile Safari/537.36",
        "ip_pool": ["39.156.66.10", "220.181.38.148", "111.13.101.208", "123.125.71.90", "61.135.169.121"],
        "type": "mobile",
    },
    {
        "id": 8, "name": "Firefox-Ubuntu",
        "user_agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "ip_pool": ["162.159.135.234", "188.114.96.0", "173.245.48.12", "190.93.244.56", "197.234.240.200"],
        "type": "desktop",
    },
    {
        "id": 9, "name": "Chrome-Linux",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ip_pool": ["140.82.112.3", "151.101.1.6", "140.82.113.4", "151.101.65.140", "140.82.114.21"],
        "type": "desktop",
    },
    {
        "id": 10, "name": "Samsung-Android",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S928U1) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/124.0.6367.113 Mobile Safari/537.36",
        "ip_pool": ["58.211.137.148", "117.136.81.145", "117.136.64.11", "223.104.128.200", "120.197.22.130"],
        "type": "mobile",
    },
    {
        "id": 11, "name": "Brave-Win10",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Brave/1.67",
        "ip_pool": ["45.33.32.156", "104.131.8.209", "198.211.112.54", "107.170.78.32", "45.56.92.141"],
        "type": "desktop",
    },
    {
        "id": 12, "name": "Opera-Win11",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/111.0.0.0",
        "ip_pool": ["185.15.56.14", "95.172.42.50", "185.15.56.22", "95.172.42.61", "185.15.56.33"],
        "type": "desktop",
    },
    {
        "id": 13, "name": "Vivaldi-macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Vivaldi/6.7",
        "ip_pool": ["207.154.232.45", "165.22.178.90", "138.197.12.44", "167.71.55.88", "134.122.77.201"],
        "type": "desktop",
    },
    {
        "id": 14, "name": "Chrome-Huawei",
        "user_agent": "Mozilla/5.0 (Linux; Android 12; HarmonyOS; ALN-AL10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "ip_pool": ["120.244.130.12", "36.152.44.96", "101.89.15.78", "222.73.144.220", "117.144.210.186"],
        "type": "mobile",
    },
    {
        "id": 15, "name": "Safari-iPhone15",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "ip_pool": ["17.250.90.45", "17.57.144.200", "17.168.100.88", "17.85.128.67", "17.188.200.150"],
        "type": "mobile",
    },
    {
        "id": 16, "name": "Edge-macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "ip_pool": ["54.183.202.199", "52.52.46.215", "13.52.134.88", "54.176.148.200", "54.67.120.55"],
        "type": "desktop",
    },
    {
        "id": 17, "name": "Chrome-Xiaomi",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; 23113RKC6C) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "ip_pool": ["111.206.11.88", "221.217.52.150", "123.58.176.67", "182.61.130.94", "114.113.196.45"],
        "type": "mobile",
    },
    {
        "id": 18, "name": "Firefox-macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0",
        "ip_pool": ["151.101.128.10", "199.232.18.10", "151.101.66.10", "199.232.82.10", "151.101.2.10"],
        "type": "desktop",
    },
    {
        "id": 19, "name": "Chrome-OPPO",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; PHZ110) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "ip_pool": ["60.174.12.88", "112.28.168.55", "117.66.138.44", "223.240.11.77", "36.57.252.66"],
        "type": "mobile",
    },
    {
        "id": 20, "name": "Yandex-Linux",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 YaBrowser/24.6.0",
        "ip_pool": ["178.154.170.45", "178.154.171.88", "2a02:6b8::111", "5.255.250.131", "100.43.80.45"],
        "type": "desktop",
    },
]

# ========== 异步Checker 身份池（独立于同步Checker） ==========
ASYNC_CHECKER_IDENTITIES = [
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.119 Mobile/15E148 Safari/604.1", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15", "type": "tablet"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; MI 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15", "type": "tablet"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; OnePlus 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.118 Mobile/15E148 Safari/604.1", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S928U1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15", "type": "tablet"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0", "type": "desktop"},
    {"user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36", "type": "mobile"},
    {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "type": "desktop"},
]


def get_random_ip(ip_pool: list) -> str:
    return random.choice(ip_pool)


def get_random_interval(min_sec: float = 30, max_sec: float = 120) -> float:
    return random.uniform(min_sec, max_sec)


def get_project_by_name(name: str) -> dict | None:
    for p in PROJECTS:
        if p["name"] == name:
            return p
    return None




PROJECT_VISIT_COUNTS = {p["name"]: p.get("visit_count", 5) for p in PROJECTS}

def assign_projects_to_checkers():
    """分配项目给 Checker
    - Checker id=1 (主Checker): 负责所有项目的完整健康检查
    - Checker id=2-20 (Visitor): 负责所有项目的模拟访问
    """
    assignments = {i["id"]: list(PROJECTS) for i in CHECKER_IDENTITIES}
    return assignments


def get_checker_role(checker_id: int) -> str:
    """获取 Checker 的角色：main(主健康检查) 或 visitor(模拟访问)"""
    return "main" if checker_id == 1 else "visitor"


def _default_search_keywords() -> dict:
    """为每个项目生成默认搜索关键词"""
    keywords_map = {}
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
    for p in PROJECTS:
        keywords_map[p["name"]] = keyword_presets.get(p["name"], [p["name"], p["name"] + " 官网"])
    return keywords_map


class RuntimeConfig:
    """运行时配置管理 - 支持持久化到 config.json，动态热更新"""

    _instance = None
    _lock = asyncio.Lock()

    def __init__(self):
        self.inspection_enabled = True
        self.time_range = {"start": "00:00", "end": "23:59"}
        # 巡检间隔（随机区间，单位：分钟）
        self.interval_min = 30
        self.interval_max = 30
        # 每轮检查次数（随机区间）
        self.rounds_min = 1
        self.rounds_max = 1
        self.rounds_interval_seconds = 3  # 每轮内多次检查间隔
        # 每日总巡检次数（随机区间，0=不限）
        self.total_inspections_min = 0
        self.total_inspections_max = 0
        self.async_checker_count = 0
        self.search_engine = "baidu"  # baidu / bing / google
        self.project_search_keywords = _default_search_keywords()
        # 模拟访问（Visitor）配置
        self.visitor_interval_min = 20
        self.visitor_interval_max = 45
        self.default_visit_count = 5
        # 每个项目的 visit_count（可动态修改）
        self.project_visit_counts = {p["name"]: p.get("visit_count", 5) for p in PROJECTS}
        self._listeners = []  # 配置变更回调

    @classmethod
    def get_instance(cls) -> "RuntimeConfig":
        if cls._instance is None:
            cls._instance = RuntimeConfig()
        return cls._instance

    def register_listener(self, callback):
        """注册配置变更监听器"""
        self._listeners.append(callback)

    def unregister_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def notify_change(self, changed_keys: set):
        """通知所有监听器配置已变更"""
        for cb in self._listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(changed_keys)
                else:
                    cb(changed_keys)
            except Exception as e:
                print(f"[Config] 通知监听器失败: {e}")

    async def load(self):
        """从文件加载配置，不存在则使用默认值并创建"""
        async with self._lock:
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._apply_dict(data)
                    print(f"[Config] 已加载配置: {CONFIG_FILE}")
                except Exception as e:
                    print(f"[Config] 加载配置失败，使用默认值: {e}")
            else:
                print(f"[Config] 配置文件不存在，使用默认值")
            # 保存一次以确保文件存在
            await self._save_to_file()

    async def _save_to_file(self):
        """保存配置到文件（必须持有 _lock）"""
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Config] 保存配置失败: {e}")

    async def update(self, new_config: dict) -> dict:
        """更新配置并持久化，返回变更的keys"""
        async with self._lock:
            old_dict = self.to_dict()
            self._apply_dict(new_config)
            await self._save_to_file()
            new_dict = self.to_dict()

        changed_keys = set()
        for key in new_config.keys():
            if old_dict.get(key) != new_dict.get(key):
                changed_keys.add(key)

        if changed_keys:
            await self.notify_change(changed_keys)

        return {"changed": list(changed_keys), "config": self.to_dict()}

    def _apply_dict(self, data: dict):
        """从字典应用配置（带校验），支持旧字段向后兼容迁移"""
        if "inspection_enabled" in data:
            self.inspection_enabled = bool(data["inspection_enabled"])

        if "time_range" in data and isinstance(data["time_range"], dict):
            tr = data["time_range"]
            start = tr.get("start", self.time_range["start"])
            end = tr.get("end", self.time_range["end"])
            # 简单格式校验 HH:MM
            if re_match_time(start) and re_match_time(end):
                self.time_range = {"start": start, "end": end}

        # ========== 向后兼容：旧字段 interval_minutes → interval_min/max ==========
        if "interval_minutes" in data and "interval_min" not in data and "interval_max" not in data:
            try:
                v = int(data["interval_minutes"])
                v = max(1, min(1440, v))
                self.interval_min = v
                self.interval_max = v
            except (ValueError, TypeError):
                pass

        if "interval_min" in data:
            try:
                v = int(data["interval_min"])
                self.interval_min = max(1, min(1440, v))
            except (ValueError, TypeError):
                pass

        if "interval_max" in data:
            try:
                v = int(data["interval_max"])
                self.interval_max = max(1, min(1440, v))
            except (ValueError, TypeError):
                pass

        # 确保 min <= max
        if self.interval_min > self.interval_max:
            self.interval_min, self.interval_max = self.interval_max, self.interval_min

        # ========== 向后兼容：旧字段 rounds_per_inspection → rounds_min/max ==========
        if "rounds_per_inspection" in data and "rounds_min" not in data and "rounds_max" not in data:
            try:
                v = int(data["rounds_per_inspection"])
                v = max(1, min(10, v))
                self.rounds_min = v
                self.rounds_max = v
            except (ValueError, TypeError):
                pass

        if "rounds_min" in data:
            try:
                v = int(data["rounds_min"])
                self.rounds_min = max(1, min(10, v))
            except (ValueError, TypeError):
                pass

        if "rounds_max" in data:
            try:
                v = int(data["rounds_max"])
                self.rounds_max = max(1, min(10, v))
            except (ValueError, TypeError):
                pass

        # 确保 min <= max
        if self.rounds_min > self.rounds_max:
            self.rounds_min, self.rounds_max = self.rounds_max, self.rounds_min

        # ========== 每日总巡检次数（随机区间） ==========
        if "total_inspections_min" in data:
            try:
                v = int(data["total_inspections_min"])
                self.total_inspections_min = max(0, min(100, v))
            except (ValueError, TypeError):
                pass

        if "total_inspections_max" in data:
            try:
                v = int(data["total_inspections_max"])
                self.total_inspections_max = max(0, min(100, v))
            except (ValueError, TypeError):
                pass

        # 确保 min <= max（如果都是0则不限；如果只有一个为0，以另一个为准）
        if self.total_inspections_min > 0 and self.total_inspections_max > 0:
            if self.total_inspections_min > self.total_inspections_max:
                self.total_inspections_min, self.total_inspections_max = self.total_inspections_max, self.total_inspections_min
        elif self.total_inspections_min > 0 and self.total_inspections_max == 0:
            self.total_inspections_max = self.total_inspections_min
        elif self.total_inspections_max > 0 and self.total_inspections_min == 0:
            self.total_inspections_min = self.total_inspections_max

        if "rounds_interval_seconds" in data:
            try:
                v = int(data["rounds_interval_seconds"])
                self.rounds_interval_seconds = max(1, min(60, v))
            except (ValueError, TypeError):
                pass

        if "async_checker_count" in data:
            try:
                v = int(data["async_checker_count"])
                self.async_checker_count = max(0, min(30, v))
            except (ValueError, TypeError):
                pass

        if "search_engine" in data:
            se = str(data["search_engine"]).lower()
            if se in ("baidu", "bing", "google"):
                self.search_engine = se

        # ========== 模拟访问（Visitor）配置 ==========
        if "visitor_interval_min" in data:
            try:
                v = int(data["visitor_interval_min"])
                self.visitor_interval_min = max(1, min(1440, v))
            except (ValueError, TypeError):
                pass

        if "visitor_interval_max" in data:
            try:
                v = int(data["visitor_interval_max"])
                self.visitor_interval_max = max(1, min(1440, v))
            except (ValueError, TypeError):
                pass

        if self.visitor_interval_min > self.visitor_interval_max:
            self.visitor_interval_min, self.visitor_interval_max = self.visitor_interval_max, self.visitor_interval_min

        if "default_visit_count" in data:
            try:
                v = int(data["default_visit_count"])
                self.default_visit_count = max(1, min(100, v))
            except (ValueError, TypeError):
                pass

        if "project_visit_counts" in data and isinstance(data["project_visit_counts"], dict):
            for pname, vc in data["project_visit_counts"].items():
                try:
                    v = int(vc)
                    if pname in self.project_visit_counts or any(p["name"] == pname for p in PROJECTS):
                        self.project_visit_counts[pname] = max(1, min(100, v))
                except (ValueError, TypeError):
                    pass

        if "project_search_keywords" in data and isinstance(data["project_search_keywords"], dict):
            # 只更新存在的项目
            for pname, kws in data["project_search_keywords"].items():
                if isinstance(kws, list):
                    # 限制 1-3 个关键词
                    valid_kws = [str(k) for k in kws[:3] if str(k).strip()]
                    if valid_kws:
                        self.project_search_keywords[pname] = valid_kws

    def to_dict(self) -> dict:
        return {
            "inspection_enabled": self.inspection_enabled,
            "time_range": dict(self.time_range),
            "interval_min": self.interval_min,
            "interval_max": self.interval_max,
            "rounds_min": self.rounds_min,
            "rounds_max": self.rounds_max,
            "rounds_interval_seconds": self.rounds_interval_seconds,
            "total_inspections_min": self.total_inspections_min,
            "total_inspections_max": self.total_inspections_max,
            "async_checker_count": self.async_checker_count,
            "search_engine": self.search_engine,
            "project_search_keywords": {
                k: list(v) for k, v in self.project_search_keywords.items()
            },
            "visitor_interval_min": self.visitor_interval_min,
            "visitor_interval_max": self.visitor_interval_max,
            "default_visit_count": self.default_visit_count,
            "project_visit_counts": dict(self.project_visit_counts),
        }

    def is_within_time_range(self, now_time: datetime | None = None) -> bool:
        """判断当前时间是否在巡检时间段内"""
        if now_time is None:
            now_time = datetime.now()
        now_minutes = now_time.hour * 60 + now_time.minute
        start_h, start_m = self.time_range["start"].split(":")
        end_h, end_m = self.time_range["end"].split(":")
        start_minutes = int(start_h) * 60 + int(start_m)
        end_minutes = int(end_h) * 60 + int(end_m)

        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes <= end_minutes
        else:
            # 跨天情况，如 22:00 - 06:00
            return now_minutes >= start_minutes or now_minutes <= end_minutes

    def get_interval_seconds(self) -> float:
        """获取巡检间隔（秒），在 interval_min ~ interval_max 分钟之间随机取值"""
        minutes = random.uniform(self.interval_min, self.interval_max)
        return minutes * 60

    def get_random_rounds(self) -> int:
        """获取本轮检查次数，在 rounds_min ~ rounds_max 之间随机取整数"""
        return random.randint(self.rounds_min, self.rounds_max)

    def get_random_total_inspections(self) -> int:
        """获取每日总巡检次数上限，在 total_inspections_min ~ max 之间随机；0表示不限"""
        if self.total_inspections_min == 0 and self.total_inspections_max == 0:
            return 0  # 不限
        return random.randint(self.total_inspections_min, self.total_inspections_max)

    def get_project_visit_count(self, project_name: str) -> int:
        """获取项目的模拟访问次数权重"""
        return self.project_visit_counts.get(project_name, self.default_visit_count)

    def set_project_visit_count(self, project_name: str, count: int):
        """设置项目的模拟访问次数权重"""
        count = max(1, min(100, count))
        self.project_visit_counts[project_name] = count

    def get_visitor_interval_seconds(self) -> float:
        """获取模拟访问间隔（秒）"""
        minutes = random.uniform(self.visitor_interval_min, self.visitor_interval_max)
        return minutes * 60

    def get_project_keywords(self, project_name: str) -> list[str]:
        """获取项目的搜索关键词"""
        return self.project_search_keywords.get(project_name, [project_name])


def re_match_time(s: str) -> bool:
    """简单的 HH:MM 格式校验"""
    import re
    return bool(re.match(r'^\d{2}:\d{2}$', s))

