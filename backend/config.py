"""配置管理 - 项目列表、检查间隔、Checker身份等"""
import os
import random

# ========== 监控项目列表 ==========
PROJECTS = [
    {"name": "智能工作台", "url": "https://www.zhinenti.cn", "category": "AI工具"},
    {"name": "部署助手", "url": "https://deploy.zhinenti.cn", "category": "AI工具"},
    {"name": "营销助手", "url": "https://craft.zhinenti.cn", "category": "AI工具"},
    {"name": "智能部署", "url": "https://auto.zhinenti.cn", "category": "AI工具"},
    {"name": "AI客服管理", "url": "http://47.113.216.237:8600", "category": "AI工具"},
    {"name": "起名工具", "url": "https://www.hpenn.online", "category": "AI工具"},
    {"name": "导航站", "url": "https://www.hpennn.online", "category": "工具"},
    {"name": "OCR识别", "url": "https://ocr.hpenn.xyz", "category": "工具"},
    {"name": "AI简历", "url": "https://resume.hpenn.xyz", "category": "工具"},
    {"name": "图片工具", "url": "https://www.kuaisutupo.xyz", "category": "工具"},
    {"name": "文档摘要", "url": "https://doc.hpenn.xyz", "category": "工具"},
    {"name": "文档转换", "url": "https://convert.hpenn.xyz", "category": "工具"},
    {"name": "文本工具", "url": "https://www.hpenn.xyz", "category": "工具"},
    {"name": "开发工具", "url": "https://www.hpennn.xyz", "category": "工具"},
    {"name": "祝福生成", "url": "https://www.zhinenti.vip", "category": "工具"},
    {"name": "AI文案", "url": "https://www.zhinenti.xyz", "category": "工具"},
    {"name": "批量图片编辑", "url": "https://imgedit.hpenn.xyz", "category": "工具"},
]

# ========== 检查配置 ==========
CHECK_INTERVAL_MIN = 30   # 最小检查间隔（秒）
CHECK_INTERVAL_MAX = 120  # 最大检查间隔（秒）
REQUEST_TIMEOUT = 10      # 请求超时时间（秒）
SLOW_THRESHOLD = 5        # 慢响应阈值（秒）
HISTORY_MAX_SIZE = 100    # 每个项目历史记录最大条数
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
RESULTS_FILE = os.path.join(DATA_DIR, "check_results.json")

# ========== 10个 Checker 的独立身份 ==========
# 每个 Checker 有独立的 User-Agent 和 IP 池
CHECKER_IDENTITIES = [
    {
        "id": 1,
        "name": "Chrome-Win10",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ip_pool": [
            "203.0.113.15", "203.0.113.42", "198.51.100.77",
            "192.0.2.123", "203.0.113.88",
        ],
        "type": "desktop",
    },
    {
        "id": 2,
        "name": "Firefox-Win11",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "ip_pool": [
            "198.51.100.23", "198.51.100.56", "203.0.113.101",
            "192.0.2.200", "198.51.100.150",
        ],
        "type": "desktop",
    },
    {
        "id": 3,
        "name": "Safari-macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "ip_pool": [
            "104.28.15.67", "104.28.16.89", "172.67.180.12",
            "104.21.45.231", "172.67.132.45",
        ],
        "type": "desktop",
    },
    {
        "id": 4,
        "name": "Edge-Win10",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "ip_pool": [
            "52.14.87.231", "18.190.123.45", "35.186.200.88",
            "104.154.89.67", "130.211.45.123",
        ],
        "type": "desktop",
    },
    {
        "id": 5,
        "name": "Chrome-iPhone",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.118 Mobile/15E148 Safari/604.1",
        "ip_pool": [
            "17.58.96.0", "17.58.100.45", "17.173.255.12",
            "17.248.128.67", "17.170.80.200",
        ],
        "type": "mobile",
    },
    {
        "id": 6,
        "name": "Safari-iPad",
        "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "ip_pool": [
            "17.110.224.0", "17.110.230.56", "17.255.128.34",
            "17.200.80.120", "17.150.45.78",
        ],
        "type": "tablet",
    },
    {
        "id": 7,
        "name": "Chrome-Android",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.119 Mobile Safari/537.36",
        "ip_pool": [
            "39.156.66.10", "220.181.38.148", "111.13.101.208",
            "123.125.71.90", "61.135.169.121",
        ],
        "type": "mobile",
    },
    {
        "id": 8,
        "name": "Firefox-Ubuntu",
        "user_agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "ip_pool": [
            "162.159.135.234", "188.114.96.0", "173.245.48.12",
            "190.93.244.56", "197.234.240.200",
        ],
        "type": "desktop",
    },
    {
        "id": 9,
        "name": "Chrome-Linux",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ip_pool": [
            "140.82.112.3", "151.101.1.6", "140.82.113.4",
            "151.101.65.140", "140.82.114.21",
        ],
        "type": "desktop",
    },
    {
        "id": 10,
        "name": "Samsung-Android",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S928U1) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/124.0.6367.113 Mobile Safari/537.36",
        "ip_pool": [
            "58.211.137.148", "117.136.81.145", "117.136.64.11",
            "223.104.128.200", "120.197.22.130",
        ],
        "type": "mobile",
    },
]


def get_random_ip(ip_pool: list) -> str:
    """从 IP 池中随机选择一个 IP"""
    return random.choice(ip_pool)


def get_random_interval() -> float:
    """获取随机检查间隔"""
    return random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX)


def get_project_by_name(name: str) -> dict | None:
    """根据名称获取项目"""
    for p in PROJECTS:
        if p["name"] == name:
            return p
    return None


def assign_projects_to_checkers():
    """将17个项目分配给10个Checker（轮询分配）"""
    assignments = {i + 1: [] for i in range(10)}
    for idx, project in enumerate(PROJECTS):
        checker_id = (idx % 10) + 1
        assignments[checker_id].append(project)
    return assignments
