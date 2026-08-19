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
from urllib.parse import urljoin, urlparse

from deep_inspector import DeepInspector
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
    get_checker_role,
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

        self.role = get_checker_role(self.id)  # "main" 或 "visitor"
        self.running = False
        self.task = None
        self.check_count = 0
        self.visit_count = 0  # 模拟访问计数
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

    def _build_visitor_headers(self, referer_url: str = None) -> dict:
        """构建模拟真实浏览器访问的请求头（不含反代头，Visitor 专用）"""
        ip = get_random_ip(self.ip_pool)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer_url else "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if referer_url:
            headers["Referer"] = referer_url
        return headers

    def _extract_internal_links(self, html: str, base_url: str) -> list[str]:
        """增强版内链提取 - 支持 SPA navigateTo / switchTab / hash 路由 / <a href> / pushState

        提取策略（优先级与覆盖度）：
        1. <a href="..."> 标签中的同域链接
        2. onclick / 内联脚本中的 navigateTo('path') 模式
        3. onclick / 内联脚本中的 switchTab('path') 模式（微信小程序风格 SPA）
        4. hash 路由 /#path 或 href="/#path"
        5. <script> 中的 pushState / replaceState 路径
        6. window.location = '/path' 赋值

        所有链接去重，排除首页自身（path='/' 或空）。
        """
        base_parsed = urlparse(base_url)
        base_netloc = base_parsed.netloc
        base_path = base_parsed.path.rstrip("/") or "/"

        raw_links = set()

        # --- 1. <a href> 标签 ---
        for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
            href = m.group(1).strip()
            if not href or href.startswith("javascript:") \
                    or href.startswith("mailto:") or href.startswith("tel:") \
                    or href.startswith("data:"):
                continue
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # --- 2. navigateTo('path') 模式（SPA 常见） ---
        # 匹配单引号或双引号包裹的路径，支持相对路径与绝对路径
        for m in re.finditer(r"navigateTo\s*\(\s*['\"]([^'\"]+)['\"]", html):
            path = m.group(1).strip()
            if not path or path.startswith("http"):
                continue
            # 如果 path 已经以 / 开头，直接拼接；否则视为相对路径
            if not path.startswith("/"):
                path = "/" + path
            full_url = urljoin(base_url, path)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # --- 3. switchTab('path') 模式（微信小程序/H5 风格 SPA） ---
        for m in re.finditer(r"switchTab\s*\(\s*['\"]([^'\"]+)['\"]", html):
            path = m.group(1).strip()
            if not path or path.startswith("http"):
                continue
            if not path.startswith("/"):
                path = "/" + path
            full_url = urljoin(base_url, path)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # --- 4. hash 路由 /#path 或 href="/#path" ---
        # 匹配 href 中的 hash 路由
        for m in re.finditer(r'<a[^>]+href=["\'](/#?[^"\']+)["\']', html, re.I):
            href = m.group(1).strip()
            # href 以 / 开头，可能是 /#path 或 /path
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # 匹配内联脚本中的 hash 路由跳转，如 location.hash = '#path'
        for m in re.finditer(r"location\.hash\s*=\s*['\"]#?([^'\"]+)['\"]", html):
            hash_val = m.group(1).strip()
            if hash_val:
                full_url = base_url.rstrip("/") + "/#" + hash_val
                raw_links.add(full_url)

        # --- 5. pushState / replaceState 中的路径 ---
        for m in re.finditer(r"(?:pushState|replaceState)\s*\([^,]+,[^,]+,\s*['\"]([^'\"]+)['\"]", html):
            path = m.group(1).strip()
            if not path or path.startswith("http"):
                continue
            full_url = urljoin(base_url, path)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # --- 6. window.location / location.href 赋值 ---
        for m in re.finditer(r"(?:window\.)?(?:location|location\.href)\s*=\s*['\"]([^'\"]+)['\"]", html):
            path = m.group(1).strip()
            if not path or path.startswith("http") or path.startswith("#"):
                continue
            full_url = urljoin(base_url, path)
            parsed = urlparse(full_url)
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                raw_links.add(full_url)

        # --- 去重 & 排除首页自身 ---
        result = []
        for link in raw_links:
            # 标准化：修复裸 hash（domain#hash -> domain/#hash）
            # urljoin 对 "#hash" 格式的 href 可能产生 base#hash（缺少 /）
            parsed = urlparse(link)
            if not parsed.path and parsed.fragment:
                link = link.replace("#", "/#", 1)
                parsed = urlparse(link)

            clean_path = parsed.path.rstrip("/") or "/"
            # 排除首页自身（path=/ 且无 hash）
            if clean_path == "/" and not parsed.fragment:
                continue
            # 排除和 base_url 完全相同的链接
            if link.rstrip("/") == base_url.rstrip("/"):
                continue
            if link not in result:
                result.append(link)

        return result

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

        # ===== 深度 HTTP 检查（从首页入口检查资源） =====
        if result["status"] in ("online", "slow"):
            try:
                deep = await DeepInspector.inspect(url, headers)
                result["deep_check"] = deep
                # 如果深度检查发现 SPA 空壳，标记
                if deep.get("is_spa_shell"):
                    result["error"] = (result.get("error") or "") + " [深度检查] 疑似 SPA 空壳页面"
                    if result["status"] == "online":
                        result["status"] = "slow"
            except Exception as e:
                logger.warning(f"[Checker-{self.id}] {project['name']} 深度检查异常: {e}")
                result["deep_check"] = None
        else:
            result["deep_check"] = None

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


    async def visit_project(self, project: dict) -> dict:
        """模拟访问项目 - 首页 + 串行内页访问（Visitor 专用，模拟真人浏览）"""
        self.current_task = f"访问: {project['name']}"
        url = project["url"]
        result = {
            "project_name": project["name"],
            "project_url": url,
            "checker_id": self.id,
            "checker_name": self.name,
            "role": "visitor",
            "homepage_ok": False,
            "homepage_status": None,
            "homepage_time_ms": None,
            "visited_pages": 0,
            "all_ok": False,
            "pages": [],
            "errors": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # ---- 首页访问：使用 visitor 专用 headers（不带反代头，无 Referer） ----
        homepage_headers = self._build_visitor_headers()

        try:
            # 首页用独立 client
            async with httpx.AsyncClient(
                headers=homepage_headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as home_client:
                start_time = time.time()
                resp = await home_client.get(url)
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                result["homepage_status"] = resp.status_code
                result["homepage_time_ms"] = elapsed_ms
                result["homepage_ok"] = resp.status_code == 200

                if not result["homepage_ok"]:
                    result["errors"].append({
                        "url": url,
                        "status": resp.status_code,
                        "error": f"首页状态码 {resp.status_code}",
                    })
                    self.current_task = "空闲"
                    return result

                html = resp.text

            # 2. SPA 站点判断：如果项目标记为 is_spa，或 sub_paths 为空且 HTML 中只有 SPA 导航模式，
            #    则只访问首页，不尝试子页面（客户端路由 HTTP 访问会返回 404）
            is_spa = project.get("is_spa", False)
            if not is_spa:
                # 启发式判断：sub_paths 为空 且 HTML 中有 navigateTo/switchTab 但没有真实 <a href> 内链
                sub_paths_empty = not project.get("sub_paths", [])
                has_spa_pattern = bool(re.search(r"navigateTo\s*\(\s*['\"]", html)) or \
                                  bool(re.search(r"switchTab\s*\(\s*['\"]", html))
                if sub_paths_empty and has_spa_pattern:
                    is_spa = True

            if is_spa:
                result["all_ok"] = True
                result["visited_pages"] = 0
                result["note"] = "SPA站点，仅访问首页"
                self.current_task = "空闲"
                return result

            # 3. 提取内链（使用增强版提取方法，支持 SPA navigateTo / switchTab / hash 路由等）
            internal_links = self._extract_internal_links(html, url)

            # 4. 如果提取不足 3 个，从 config 的 sub_paths 补充
            if len(internal_links) < 3:
                sub_paths = project.get("sub_paths", [])
                if sub_paths:
                    config_links = []
                    for sp in sub_paths:
                        full_url = urljoin(url, sp)
                        parsed = urlparse(full_url)
                        # 排除首页自身
                        clean_path = parsed.path.rstrip("/") or "/"
                        if clean_path == "/" and not parsed.fragment:
                            continue
                        if full_url not in internal_links and full_url not in config_links:
                            config_links.append(full_url)
                    internal_links.extend(config_links)

            if not internal_links:
                result["all_ok"] = True  # 没有内链也算正常
                self.current_task = "空闲"
                return result

            # 5. 随机选 3-5 个内页
            num_pages = min(random.randint(3, 5), len(internal_links))
            sampled_links = random.sample(internal_links, num_pages)

            # 6. 串行访问内页（模拟真人逐个点击，带阅读间隔）
            #    每个内页用独立 client，设置不同 headers（带首页 Referer）
            for i, page_url in enumerate(sampled_links):
                # 模拟阅读间隔（第一个内页前不加延迟）
                if i > 0:
                    await asyncio.sleep(random.uniform(1.5, 4.0))

                # 内页用独立 headers，带首页 Referer
                page_headers = self._build_visitor_headers(referer_url=url)

                page_start = time.time()
                try:
                    async with httpx.AsyncClient(
                        headers=page_headers,
                        timeout=REQUEST_TIMEOUT,
                        follow_redirects=True,
                        verify=True,
                    ) as page_client:
                        page_resp = await page_client.get(page_url)
                        page_elapsed = round((time.time() - page_start) * 1000, 2)
                        page_html = page_resp.text

                        title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)
                        has_title = bool(title_match and title_match.group(1).strip())

                        ok = page_resp.status_code < 400
                        page_result = {
                            "url": page_url,
                            "status": page_resp.status_code,
                            "time_ms": page_elapsed,
                            "has_title": has_title,
                            "ok": ok,
                        }
                        if not ok:
                            result["errors"].append({
                                "url": page_url,
                                "status": page_resp.status_code,
                                "error": page_resp.reason_phrase or "Error",
                            })
                        result["pages"].append(page_result)
                except httpx.TimeoutException:
                    page_elapsed = round((time.time() - page_start) * 1000, 2)
                    result["errors"].append({
                        "url": page_url,
                        "status": None,
                        "error": "Timeout",
                    })
                    result["pages"].append({
                        "url": page_url,
                        "status": None,
                        "time_ms": page_elapsed,
                        "has_title": False,
                        "ok": False,
                    })
                except Exception as e:
                    page_elapsed = round((time.time() - page_start) * 1000, 2)
                    result["errors"].append({
                        "url": page_url,
                        "status": None,
                        "error": str(e)[:100],
                    })
                    result["pages"].append({
                        "url": page_url,
                        "status": None,
                        "time_ms": page_elapsed,
                        "has_title": False,
                        "ok": False,
                    })

            result["visited_pages"] = len(sampled_links)
            result["all_ok"] = all(p["ok"] for p in result["pages"])

        except httpx.TimeoutException:
            result["errors"].append({"url": url, "status": None, "error": "请求超时"})
        except httpx.ConnectError as e:
            result["errors"].append({"url": url, "status": None, "error": f"连接失败: {str(e)[:80]}"})
        except Exception as e:
            result["errors"].append({"url": url, "status": None, "error": f"未知错误: {str(e)[:80]}"})
            logger.error(f"[Visitor-{self.id}] {project['name']} 访问异常: {e}")

        self.visit_count += 1
        self.last_check_time = datetime.now(timezone.utc).isoformat()
        self.current_task = "空闲"

        return result

    async def run_visitor_loop(self):
        """Visitor 主循环 - 按权重随机选择项目进行模拟访问"""
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()

        config = self._get_config()
        logger.info(f"[Visitor-{self.id}] {self.name} 启动（模拟访问模式），共 {len(self.projects)} 个项目")

        while self.running:
            await self._pause_event.wait()
            if not self.running:
                break

            # 按 visit_count 权重随机选择项目
            project = self._pick_project_by_weight(config)
            if not project:
                await self._sleep_interruptible(30)
                continue

            try:
                result = await self.visit_project(project)
                await CheckerManager.save_visit_result(result)

                # 有错误时记录日志
                if result["errors"]:
                    err_summary = "; ".join(
                        f"{e['url'][:50]}: {e.get('error', 'error')}" for e in result["errors"][:3]
                    )
                    logger.warning(f"[Visitor-{self.id}] {project['name']} 发现错误: {err_summary}")
            except Exception as e:
                logger.error(f"[Visitor-{self.id}] 访问 {project['name']} 异常: {e}")

            # 间隔
            if self.running and self._pause_event.is_set():
                interval = config.get_visitor_interval_seconds()
                await self._sleep_interruptible(interval)

        logger.info(f"[Visitor-{self.id}] {self.name} 已停止")
        self.running = False

    def _pick_project_by_weight(self, config) -> dict | None:
        """按 visit_count 权重随机选择项目"""
        if not self.projects:
            return None
        weights = [config.get_project_visit_count(p["name"]) for p in self.projects]
        total = sum(weights)
        if total <= 0:
            return random.choice(self.projects)
        # 加权随机选择
        r = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return self.projects[i]
        return self.projects[-1]

    async def run_loop(self):
        """Checker 主循环 - 根据角色决定运行模式"""
        if self.role == "visitor":
            await self.run_visitor_loop()
        else:
            await self.run_main_loop()

    async def run_main_loop(self):
        """主 Checker 循环 - 支持暂停/恢复和动态间隔"""
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
            "current_task": self.current_task,
            "last_check_time": self.last_check_time,
            "role": self.role,
            "visit_count": self.visit_count,
            "project_count": len(self.projects),
            "projects": [p["name"] for p in self.projects],
        }


class CheckerManager:
    """Checker 管理器 - 管理所有 Checker 和结果存储"""

    _checkers: dict[int, Checker] = {}
    _results: dict[str, list[dict]] = {}  # project_name -> [history]
    _latest: dict[str, dict] = {}  # project_name -> latest result
    _async_latest: dict[str, dict] = {}  # 异步Checker最新结果
    _visit_results: dict[str, list[dict]] = {}  # 模拟访问记录 project_name -> [history]
    _visit_latest: dict[str, dict] = {}  # 每个项目最新的模拟访问结果
    _lock = asyncio.Lock()
    _initialized = False
    _ws_clients: list = []  # WebSocket 客户端列表
    _start_time = None  # 服务启动时间


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
                    cls._visit_results = data.get("visit_history", {})
                    cls._visit_latest = data.get("visit_latest", {})
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
                    "visit_latest": cls._visit_latest,
                    "visit_history": {k: v[-20:] for k, v in cls._visit_results.items()},
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

        asyncio.create_task(cls._save_results_to_file())
        # WebSocket 推送
        await cls._ws_broadcast({
            "type": "status_update",
            "project": result,
        })
        # 添加实时日志
        log_msg = f"#{result.get('checker_id', '?')} 检测 {project_name}: {result['status']} " \
                  f"({result.get('response_time_ms', 0)}ms)"
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
    async def save_visit_result(cls, result: dict):
        """保存模拟访问结果（Visitor）"""
        project_name = result["project_name"]
        async with cls._lock:
            cls._visit_latest[project_name] = result
            if project_name not in cls._visit_results:
                cls._visit_results[project_name] = []
            cls._visit_results[project_name].append(result)
            if len(cls._visit_results[project_name]) > HISTORY_MAX_SIZE:
                cls._visit_results[project_name] = cls._visit_results[project_name][-HISTORY_MAX_SIZE:]

        asyncio.create_task(cls._save_results_to_file())
        # WebSocket 推送
        await cls._ws_broadcast({
            "type": "visit_update",
            "project": result,
        })
        # 记录实时日志
        status_str = "正常" if result.get("all_ok") else "异常"
        cls._add_log(
            "info" if result.get("all_ok") else "warning",
            f"[访问] #{result.get('checker_id', '?')} {project_name}: {status_str} ({result.get('visited_pages', 0)}页)"
        )

    @classmethod
    def get_visit_latest(cls, project_name: str) -> dict | None:
        """获取单个项目的最新模拟访问结果"""
        return cls._visit_latest.get(project_name)

    @classmethod
    def get_visit_all_latest(cls) -> dict[str, dict]:
        """获取所有项目的最新模拟访问结果"""
        return cls._visit_latest.copy()

    @classmethod
    def get_visit_history(cls, project_name: str) -> list[dict]:
        """获取单个项目的模拟访问历史"""
        return cls._visit_results.get(project_name, [])[-20:]

    @classmethod
    def get_visitor_count(cls) -> int:
        """获取运行中的 Visitor 数量"""
        return sum(1 for c in cls._checkers.values() if c.role == "visitor" and c.running and not c.paused)

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
            "visitors_running": sum(1 for c in cls._checkers.values() if c.role == "visitor" and c.running and not c.paused),
            "visitors_total": sum(1 for c in cls._checkers.values() if c.role == "visitor"),
        }

    @classmethod
    def get_checker_workload(cls) -> dict[int, dict]:
        """获取各Checker的工作量分布"""
        workload = {}
        for checker in cls._checkers.values():
            workload[checker.id] = {
                "name": checker.name,
                "check_count": checker.check_count,
                "visit_count": checker.visit_count,
                "project_count": len(checker.projects),
                "running": checker.running and not checker.paused,
                "current_task": checker.current_task,
            }
        return workload

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
                "main_running": sum(1 for c in cls._checkers.values() if c.role == "main" and c.running and not c.paused),
                "visitor_total": sum(1 for c in cls._checkers.values() if c.role == "visitor"),
                "visitor_running": sum(1 for c in cls._checkers.values() if c.role == "visitor" and c.running and not c.paused),
                "async_total": 0,  # 由main.py补充
                "async_running": 0,
            },"inspections_today": cls._inspection_count,
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



# =====================================================================
# 视频访问模块 - 独立于站点巡检
# =====================================================================

from config import (
    VIDEO_REQUEST_TIMEOUT,
    VIDEO_RANGE_BYTES,
    VIDEO_MIN_DELAY,
    VIDEO_MAX_DELAY,
    VIDEO_RESULTS_FILE,
)


class VideoChecker:
    """单个视频访问 agent - 模拟真实用户播放视频"""

    def __init__(self, identity: dict, video_index: int = 0):
        self.id = identity.get("id", video_index + 1)
        self.name = identity.get("name", f"VideoAgent-{video_index + 1}")
        self.user_agent = identity.get("user_agent", identity.get("user_agent", "Mozilla/5.0"))
        self.ip_pool = identity.get("ip_pool", ["127.0.0.1"])
        self.type = identity.get("type", "desktop")

        self.running = False
        self.task = None
        self.play_count = 0
        self.current_video = "空闲"
        self.last_play_time = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()

        self._video_config = None

    def _get_video_config(self):
        if self._video_config is None:
            from config import VideoConfig
            self._video_config = VideoConfig.get_instance()
        return self._video_config

    def _build_video_headers(self, referer_url: str = None, is_range: bool = False) -> dict:
        """构建视频请求头，模拟真实浏览器视频播放行为"""
        ip = get_random_ip(self.ip_pool)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "video" if is_range else "document",
            "Sec-Fetch-Mode": "no-cors" if is_range else "navigate",
            "Sec-Fetch-Site": "same-origin" if referer_url else "none",
            "Cache-Control": "max-age=0",
            "Pragma": "no-cache",
        }
        if referer_url:
            headers["Referer"] = referer_url
        if is_range:
            headers["Range"] = f"bytes=0-{VIDEO_RANGE_BYTES - 1}"
            headers["Accept-Ranges"] = "bytes"
        return headers

    def _is_video_file_url(self, url: str) -> bool:
        """判断URL是否直接指向视频文件"""
        video_extensions = ('.mp4', '.webm', '.m3u8', '.ts', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.ogg')
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(ext) for ext in video_extensions)

    async def visit_video(self, video: dict) -> dict:
        """模拟访问视频
        - 先访问视频页面（如果URL不是直接视频文件）
        - 再用 Range 头请求视频数据（模拟播放器预加载）
        """
        self.current_video = video["name"]
        url = video["url"]
        result = {
            "video_name": video["name"],
            "video_url": url,
            "checker_id": self.id,
            "checker_name": self.name,
            "checker_type": self.type,
            "source_ip": get_random_ip(self.ip_pool),
            "page_status": None,
            "page_time_ms": None,
            "video_status": None,
            "video_time_ms": None,
            "video_size_bytes": None,
            "accept_ranges": False,
            "success": False,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        is_direct_video = self._is_video_file_url(url)

        try:
            if not is_direct_video:
                # ---- 第一步：访问视频页面 ----
                page_headers = self._build_video_headers(referer_url=None, is_range=False)
                async with httpx.AsyncClient(
                    headers=page_headers,
                    timeout=VIDEO_REQUEST_TIMEOUT,
                    follow_redirects=True,
                    verify=True,
                ) as page_client:
                    page_start = time.time()
                    page_resp = await page_client.get(url)
                    page_elapsed = round((time.time() - page_start) * 1000, 2)
                    result["page_status"] = page_resp.status_code
                    result["page_time_ms"] = page_elapsed

                    if page_resp.status_code >= 400:
                        result["error"] = f"视频页面状态码 {page_resp.status_code}"
                        self.current_video = "空闲"
                        return result

                    html = page_resp.text
                    final_url = str(page_resp.url)

                # ---- 第二步：从页面中提取视频源地址 ----
                video_src = self._extract_video_src(html, final_url)
                if not video_src:
                    # 找不到视频源，用页面URL本身尝试作为视频URL
                    video_src = url
            else:
                # 直接是视频文件URL
                video_src = url
                result["page_status"] = 200
                result["page_time_ms"] = 0

            # ---- 第三步：请求视频数据（Range 头模拟播放） ----
            video_headers = self._build_video_headers(
                referer_url=url if not is_direct_video else None,
                is_range=True
            )
            async with httpx.AsyncClient(
                headers=video_headers,
                timeout=VIDEO_REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as video_client:
                video_start = time.time()
                video_resp = await video_client.get(video_src)
                video_elapsed = round((time.time() - video_start) * 1000, 2)
                result["video_status"] = video_resp.status_code
                result["video_time_ms"] = video_elapsed

                # 获取视频大小
                content_length = video_resp.headers.get("Content-Length")
                if content_length:
                    try:
                        result["video_size_bytes"] = int(content_length)
                    except (ValueError, TypeError):
                        pass

                # 检查是否支持 Range
                accept_ranges = video_resp.headers.get("Accept-Ranges", "").lower()
                result["accept_ranges"] = "bytes" in accept_ranges or video_resp.status_code == 206

                # 判断成功（200 或 206 都算成功）
                if video_resp.status_code in (200, 206):
                    result["success"] = True
                else:
                    result["error"] = f"视频请求状态码 {video_resp.status_code}"

        except httpx.TimeoutException:
            result["error"] = "视频请求超时"
        except httpx.ConnectError as e:
            result["error"] = f"连接失败: {str(e)[:80]}"
        except Exception as e:
            result["error"] = f"未知错误: {str(e)[:80]}"
            logger.error(f"[VideoAgent-{self.id}] {video['name']} 访问异常: {e}")

        self.play_count += 1
        self.last_play_time = datetime.now(timezone.utc).isoformat()
        self.current_video = "空闲"

        return result

    def _extract_video_src(self, html: str, base_url: str) -> str | None:
        """从HTML中提取视频源地址"""
        # 1. <video src="..."> 或 <source src="...">
        patterns = [
            r'<video[^>]+src=["\']([^"\']+)["\']',
            r'<source[^>]+src=["\']([^"\']+)["\']',
            r'video(?:Url|Src)\s*[:=]\s*["\']([^"\']+)["\']',
            r'poster\s*=\s*["\'][^"\']*["\'][^>]*src=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.I)
            if match:
                src = match.group(1).strip()
                if src:
                    # 处理相对路径
                    if src.startswith("//"):
                        return "https:" + src
                    elif src.startswith("http"):
                        return src
                    else:
                        return urljoin(base_url, src)
        return None

    async def run_video_loop(self):
        """视频访问主循环 - 按 play_count 依次访问每个视频"""
        self.running = True
        self._stop_event.clear()
        self._pause_event.set()

        video_config = self._get_video_config()
        logger.info(f"[VideoAgent-{self.id}] {self.name} 启动（视频访问模式）")

        while self.running:
            await self._pause_event.wait()
            if not self.running:
                break

            videos = video_config.get_videos()
            if not videos:
                await self._sleep_interruptible(30)
                continue

            # 每轮循环：按 play_count 依次访问每个视频
            for video in videos:
                if not self.running:
                    break
                if not self._pause_event.is_set():
                    break

                play_count = video.get("play_count", 5)
                for i in range(play_count):
                    if not self.running:
                        break
                    if not self._pause_event.is_set():
                        break

                    try:
                        result = await self.visit_video(video)
                        await VideoCheckerManager.save_result(result)

                        if result["success"]:
                            logger.info(f"[VideoAgent-{self.id}] 播放 {video['name']} ({i + 1}/{play_count}) 成功 ({result.get('video_time_ms', 0)}ms)")
                        else:
                            logger.warning(f"[VideoAgent-{self.id}] 播放 {video['name']} 失败: {result.get('error', 'unknown')}")
                    except Exception as e:
                        logger.error(f"[VideoAgent-{self.id}] 播放 {video['name']} 异常: {e}")

                    # 每次播放间随机延迟（模拟真实观看）
                    if i < play_count - 1 and self.running and self._pause_event.is_set():
                        delay = random.uniform(VIDEO_MIN_DELAY, VIDEO_MAX_DELAY)
                        await self._sleep_interruptible(delay)

                # 不同视频之间也加短暂延迟
                if self.running and self._pause_event.is_set():
                    await self._sleep_interruptible(random.uniform(1, 3))

            # 一轮循环结束后短暂休息
            if self.running and self._pause_event.is_set():
                await self._sleep_interruptible(random.uniform(10, 30))

        logger.info(f"[VideoAgent-{self.id}] {self.name} 已停止")
        self.running = False

    async def _sleep_interruptible(self, seconds: float):
        """可中断睡眠"""
        elapsed = 0
        step = min(1.0, max(0.5, seconds / 30))
        while elapsed < seconds and self.running and self._pause_event.is_set():
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += step

    def stop(self):
        self.running = False
        self._stop_event.set()
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "user_agent": self.user_agent,
            "ip_sample": self.ip_pool[0] if self.ip_pool else "N/A",
            "running": self.running,
            "paused": self.paused,
            "play_count": self.play_count,
            "current_video": self.current_video,
            "last_play_time": self.last_play_time,
        }


class VideoCheckerManager:
    """视频访问管理器 - 管理视频访问agent和结果存储"""

    _sync_checkers: dict[int, VideoChecker] = {}
    _async_checkers: dict[int, VideoChecker] = {}
    _results: dict[str, list[dict]] = {}  # video_name -> [history]
    _latest: dict[str, dict] = {}  # video_name -> latest result
    _lock = asyncio.Lock()
    _initialized = False

    @classmethod
    async def initialize(cls):
        """初始化视频访问管理器"""
        if cls._initialized:
            return

        os.makedirs(os.path.dirname(VIDEO_RESULTS_FILE), exist_ok=True)
        await cls._load_results()
        cls._initialized = True
        logger.info("[VideoCheckerManager] 视频访问管理器初始化完成")

    @classmethod
    async def _load_results(cls):
        """从文件加载视频访问结果"""
        async with cls._lock:
            if os.path.exists(VIDEO_RESULTS_FILE):
                try:
                    with open(VIDEO_RESULTS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cls._results = data.get("history", {})
                    cls._latest = data.get("latest", {})
                    logger.info(f"[VideoCheckerManager] 已加载视频访问结果，共 {len(cls._latest)} 个视频")
                except Exception as e:
                    logger.error(f"[VideoCheckerManager] 加载视频访问结果失败: {e}")
                    cls._results = {}
                    cls._latest = {}

    @classmethod
    async def _save_results_to_file(cls):
        """保存视频访问结果到文件"""
        async with cls._lock:
            try:
                data = {
                    "history": {k: v[-20:] for k, v in cls._results.items()},
                    "latest": cls._latest,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                os.makedirs(os.path.dirname(VIDEO_RESULTS_FILE), exist_ok=True)
                with open(VIDEO_RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[VideoCheckerManager] 保存视频访问结果失败: {e}")

    @classmethod
    async def save_result(cls, result: dict):
        """保存单次视频访问结果"""
        video_name = result["video_name"]
        async with cls._lock:
            cls._latest[video_name] = result
            if video_name not in cls._results:
                cls._results[video_name] = []
            cls._results[video_name].append(result)
            if len(cls._results[video_name]) > HISTORY_MAX_SIZE:
                cls._results[video_name] = cls._results[video_name][-HISTORY_MAX_SIZE:]

        asyncio.create_task(cls._save_results_to_file())
        # WebSocket 推送
        await CheckerManager._ws_broadcast({
            "type": "video_status_update",
            "video": result,
        })

    @classmethod
    def get_all_status(cls) -> dict[str, dict]:
        return cls._latest.copy()

    @classmethod
    def get_video_history(cls, video_name: str) -> list[dict]:
        return cls._results.get(video_name, [])[-20:]

    # ===== 同步视频访问 =====
    @classmethod
    async def start_sync(cls):
        """启动同步视频访问（使用同步Checker身份池）"""
        await cls.initialize()
        # 使用同步身份池中的前5个身份作为视频访问agent
        from config import CHECKER_IDENTITIES
        identities = CHECKER_IDENTITIES[:5]
        cls._sync_checkers = {}
        for i, identity in enumerate(identities):
            checker = VideoChecker(identity, video_index=i)
            cls._sync_checkers[checker.id] = checker
            checker.task = asyncio.create_task(checker.run_video_loop())
        logger.info(f"[VideoCheckerManager] 同步视频访问已启动，共 {len(cls._sync_checkers)} 个agent")

    @classmethod
    async def stop_sync(cls):
        """停止同步视频访问"""
        for checker in cls._sync_checkers.values():
            if checker.running:
                checker.stop()
                if checker.task:
                    checker.task.cancel()
        logger.info("[VideoCheckerManager] 同步视频访问已停止")

    @classmethod
    def pause_sync(cls):
        for checker in cls._sync_checkers.values():
            checker.pause()

    @classmethod
    def resume_sync(cls):
        for checker in cls._sync_checkers.values():
            checker.resume()

    # ===== 异步视频访问 =====
    @classmethod
    async def start_async(cls):
        """启动异步视频访问（使用异步身份池）"""
        await cls.initialize()
        from config import ASYNC_CHECKER_IDENTITIES
        identities = ASYNC_CHECKER_IDENTITIES[:5]
        cls._async_checkers = {}
        for i, identity in enumerate(identities):
            # 给异步身份补充id和name
            full_identity = {
                "id": 100 + i,
                "name": f"AsyncVideo-{i + 1}",
                "user_agent": identity["user_agent"],
                "ip_pool": ["172.16.0." + str(10 + i) for _ in range(5)],
                "type": identity["type"],
            }
            checker = VideoChecker(full_identity, video_index=i)
            cls._async_checkers[checker.id] = checker
            checker.task = asyncio.create_task(checker.run_video_loop())
        logger.info(f"[VideoCheckerManager] 异步视频访问已启动，共 {len(cls._async_checkers)} 个agent")

    @classmethod
    async def stop_async(cls):
        """停止异步视频访问"""
        for checker in cls._async_checkers.values():
            if checker.running:
                checker.stop()
                if checker.task:
                    checker.task.cancel()
        logger.info("[VideoCheckerManager] 异步视频访问已停止")

    @classmethod
    def pause_async(cls):
        for checker in cls._async_checkers.values():
            checker.pause()

    @classmethod
    def resume_async(cls):
        for checker in cls._async_checkers.values():
            checker.resume()

    @classmethod
    def get_sync_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._sync_checkers.values()]

    @classmethod
    def get_async_status(cls) -> list[dict]:
        return [c.get_status() for c in cls._async_checkers.values()]

    @classmethod
    def get_all_agents(cls) -> dict:
        return {
            "sync": cls.get_sync_status(),
            "async": cls.get_async_status(),
        }

    @classmethod
    def is_sync_running(cls) -> bool:
        return any(c.running and not c.paused for c in cls._sync_checkers.values())

    @classmethod
    def is_async_running(cls) -> bool:
        return any(c.running and not c.paused for c in cls._async_checkers.values())
