"""深度 HTTP 检查器 - 从首页入口检查页面资源完整性"""
import asyncio
import random
import re
import ssl
import time
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import httpx
from config import REQUEST_TIMEOUT, SLOW_THRESHOLD

logger = logging.getLogger("health_checker")


class DeepInspector:
    """从首页入口进行深度 HTTP 检查"""

    @staticmethod
    async def inspect(url: str, headers: dict) -> dict:
        """从首页入口执行深度检查"""
        result = {
            "status_code": None,
            "response_time_ms": None,
            "status": "unknown",
            "content_length": 0,
            "title": "",
            "has_title": False,
            "has_meta_description": False,
            "has_meta_viewport": False,
            "has_og_tags": False,
            "body_has_content": False,
            "is_spa_shell": False,
            "resources": {"css": [], "js": [], "images": []},
            "resource_check": {"total": 0, "accessible": 0, "failed": 0, "failed_list": []},
            "simulated_visits": None,
            "ssl_check": {"valid": None, "expiry": None, "error": None},
            "error": None,
        }

        # Step 1: 获取首页
        start = time.time()
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                verify=True,
            ) as client:
                resp = await client.get(url)
                elapsed = time.time() - start
                ms = round(elapsed * 1000, 2)

                result["status_code"] = resp.status_code
                result["response_time_ms"] = ms
                result["content_length"] = len(resp.text)

                if resp.status_code == 200:
                    result["status"] = "slow" if ms > SLOW_THRESHOLD * 1000 else "online"
                else:
                    result["status"] = "offline"

                html = resp.text

                # Step 2: 解析 meta/title
                title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
                result["title"] = title_m.group(1).strip()[:100] if title_m else ""
                result["has_title"] = bool(result["title"])
                result["has_meta_description"] = bool(re.search(
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'][^"\']+', html, re.I))
                result["has_meta_viewport"] = bool(re.search(
                    r'<meta[^>]+name=["\']viewport["\']', html, re.I))
                result["has_og_tags"] = bool(re.search(
                    r'<meta[^>]+property=["\']og:', html, re.I))

                # Step 3: body 内容检查
                body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.I | re.S)
                body_text = body_m.group(1) if body_m else ""
                body_text = re.sub(r"<script[^>]*>.*?</script>", "", body_text, flags=re.I | re.S)
                body_text = re.sub(r"<style[^>]*>.*?</style>", "", body_text, flags=re.I | re.S)
                body_text = re.sub(r"<[^>]+>", "", body_text).strip()
                result["body_has_content"] = len(body_text) > 50
                result["is_spa_shell"] = (
                    bool(re.search(r'id=["\']app["\']', html)) and len(body_text) < 20
                )

                # Step 4: 提取首页引用的资源
                resources = []
                for m in re.finditer(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']stylesheet["\']', html, re.I):
                    resources.append(("css", urljoin(url, m.group(1))))
                for m in re.finditer(r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']', html, re.I):
                    href = m.group(1)
                    if not any(r[1] == urljoin(url, href) for r in resources):
                        resources.append(("css", urljoin(url, href)))
                for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
                    resources.append(("js", urljoin(url, m.group(1))))
                for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
                    src = urljoin(url, m.group(1))
                    if not src.startswith("data:"):
                        resources.append(("image", src))

                # 去重 + 限制数量
                seen = set()
                unique = []
                for rtype, rurl in resources:
                    if rurl not in seen:
                        seen.add(rurl)
                        unique.append((rtype, rurl))
                resources = unique[:30]  # 最多检查 30 个资源

                result["resources"] = {"css": [], "js": [], "images": []}
                for rtype, rurl in resources:
                    key = "css" if rtype == "css" else ("js" if rtype == "js" else "images")
                    result["resources"][key].append(rurl)

                # Step 5: 检查关键资源可访问性（只查 CSS 和 JS，图片跳过太多）
                key_resources = [(t, u) for t, u in resources if t in ("css", "js")]
                if key_resources:
                    checks = await asyncio.gather(
                        *[DeepInspector._check_resource(client, u) for _, u in key_resources],
                        return_exceptions=True,
                    )
                    ok = 0
                    fail_list = []
                    for i, c in enumerate(checks):
                        if isinstance(c, Exception):
                            fail_list.append({"url": key_resources[i][1], "type": key_resources[i][0], "error": str(c)[:100]})
                        elif c.get("accessible"):
                            ok += 1
                        else:
                            fail_list.append({"url": key_resources[i][1], "type": key_resources[i][0], "error": c.get("error", "unknown")})

                    result["resource_check"] = {
                        "total": len(key_resources),
                        "accessible": ok,
                        "failed": len(fail_list),
                        "failed_list": fail_list[:5],
                    }

                # Step 5.5: 如果资源全部失败且有内容，降级但不标记离线
                if result["resource_check"]["total"] > 0 and result["resource_check"]["accessible"] == 0:
                    if result["body_has_content"]:
                        result["status"] = "slow"
                        result["error"] = "所有关键资源加载失败"

                # Step 5.7: 模拟访问内部页面
                if result["status"] in ("online", "slow"):
                    internal_links = DeepInspector._extract_internal_links(html, url)
                    if len(internal_links) > 1:  # 至少有 2 个内链才有意义
                        result["simulated_visits"] = await DeepInspector._visit_internal_pages(client, internal_links)
                    else:
                        result["simulated_visits"] = {"visited": 0, "all_ok": True, "pages": [], "errors": [], "note": "站内链接不足"}
                else:
                    result["simulated_visits"] = None

        except httpx.TimeoutException:
            result["status"] = "offline"
            result["response_time_ms"] = round((time.time() - start) * 1000, 2)
            result["error"] = "请求超时"
        except httpx.ConnectError as e:
            result["status"] = "offline"
            result["response_time_ms"] = round((time.time() - start) * 1000, 2)
            result["error"] = f"连接失败: {str(e)[:80]}"
        except Exception as e:
            result["status"] = "offline"
            result["response_time_ms"] = round((time.time() - start) * 1000, 2)
            result["error"] = f"未知错误: {str(e)[:80]}"

        # Step 6: SSL 证书检查
        if url.startswith("https://"):
            result["ssl_check"] = await DeepInspector._check_ssl(url)

        return result

    @staticmethod
    def _extract_internal_links(html: str, base_url: str) -> list[str]:
        """从首页 HTML 提取同域名的内部链接"""
        base_parsed = urlparse(base_url)
        base_netloc = base_parsed.netloc
        seen = set()
        links = []
        for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
            href = m.group(1).strip()
            # 排除无效协议
            if not href or href.startswith("#") or href.startswith("javascript:") \
                    or href.startswith("mailto:") or href.startswith("tel:") \
                    or href.startswith("data:"):
                continue
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            # 只保留同域名且为 http/https 的链接
            if parsed.netloc == base_netloc and parsed.scheme in ("http", "https"):
                # 去除锚点部分以避免重复
                clean_url = full_url.split("#")[0]
                if clean_url not in seen:
                    seen.add(clean_url)
                    links.append(clean_url)
        return links

    @staticmethod
    async def _visit_internal_pages(client: httpx.AsyncClient, urls: list[str], max_pages: int = 5) -> dict:
        """模拟访问内部页面，检查状态"""
        sample_count = min(max_pages, len(urls))
        sampled = random.sample(urls, sample_count)

        pages = []
        errors = []

        async def _visit(page_url: str) -> dict:
            page_start = time.time()
            try:
                resp = await client.get(page_url, timeout=8)
                elapsed_ms = round((time.time() - page_start) * 1000, 2)
                page_html = resp.text

                # 检查 title
                title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)
                has_title = bool(title_match and title_match.group(1).strip())

                # 检查内容（body 去标签后 > 20 字符）
                body_match = re.search(r"<body[^>]*>(.*?)</body>", page_html, re.I | re.S)
                body_content = body_match.group(1) if body_match else ""
                body_content = re.sub(r"<script[^>]*>.*?</script>", "", body_content, flags=re.I | re.S)
                body_content = re.sub(r"<style[^>]*>.*?</style>", "", body_content, flags=re.I | re.S)
                body_content = re.sub(r"<[^>]+>", "", body_content).strip()
                has_content = len(body_content) > 20

                status_label = "ok" if resp.status_code < 400 else "error"
                page_result = {
                    "url": page_url,
                    "status_code": resp.status_code,
                    "response_time_ms": elapsed_ms,
                    "has_title": has_title,
                    "has_content": has_content,
                    "status": status_label,
                }
                if status_label == "error":
                    errors.append({"url": page_url, "status_code": resp.status_code, "error": resp.reason_phrase or "Error"})
                return page_result
            except httpx.TimeoutException:
                elapsed_ms = round((time.time() - page_start) * 1000, 2)
                errors.append({"url": page_url, "status_code": None, "error": "Timeout"})
                return {
                    "url": page_url,
                    "status_code": None,
                    "response_time_ms": elapsed_ms,
                    "has_title": False,
                    "has_content": False,
                    "status": "timeout",
                }
            except Exception as e:
                elapsed_ms = round((time.time() - page_start) * 1000, 2)
                errors.append({"url": page_url, "status_code": None, "error": str(e)[:100]})
                return {
                    "url": page_url,
                    "status_code": None,
                    "response_time_ms": elapsed_ms,
                    "has_title": False,
                    "has_content": False,
                    "status": "error",
                }

        results = await asyncio.gather(*[_visit(u) for u in sampled])
        pages = list(results)

        all_ok = all(p["status"] == "ok" for p in pages)

        return {
            "visited": len(pages),
            "all_ok": all_ok,
            "pages": pages,
            "errors": errors,
        }

    @staticmethod
    async def _check_resource(client: httpx.AsyncClient, resource_url: str) -> dict:
        """检查单个资源是否可访问（先 HEAD 再 GET）"""
        try:
            resp = await client.head(resource_url, follow_redirects=True, timeout=8)
            if resp.status_code < 400:
                return {"accessible": True, "status": resp.status_code}
            resp = await client.get(resource_url, follow_redirects=True, timeout=8)
            return {"accessible": resp.status_code < 400, "status": resp.status_code}
        except Exception as e:
            return {"accessible": False, "error": str(e)[:100]}

    @staticmethod
    async def _check_ssl(url: str) -> dict:
        """检查 SSL 证书"""
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
            not_after = cert.get("notAfter", "")
            expiry = None
            if not_after:
                try:
                    expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            return {"valid": True, "expiry": expiry.isoformat() if expiry else None, "error": None}
        except Exception as e:
            return {"valid": False, "expiry": None, "error": str(e)[:100]}
