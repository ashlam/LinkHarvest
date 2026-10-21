"""Cookie 支持：让工具复用你在浏览器里已经通过验证的会话。

为什么需要这个：有些站点（虎嗅用的是阿里云 WAF）会在浏览器指纹层面拦截
全新的无头浏览器，即使页面本身没有登录墙。但你的日常浏览器里已经有一份
"通过验证"的 cookie，把它拿过来就能正常抓。

支持两种来源：
  1. Netscape 格式的 cookies.txt（浏览器扩展导出，最通用）
  2. Playwright 的 storage_state JSON（--save-session 生成的）
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def parse_netscape_cookies(text: str) -> list[dict]:
    """解析 Netscape cookies.txt（浏览器扩展的标准导出格式）。

    注意 `#HttpOnly_` 前缀：这类行看起来像注释，但其实是 HttpOnly cookie，
    往往正是关键的会话凭证，必须解析而不是跳过。
    """
    cookies: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        http_only = False
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            http_only = True
        elif line.startswith("#"):
            continue

        # 制表符分隔：domain, flag, path, secure, expiration, name, value
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        try:
            expires_i = int(float(expires))
        except ValueError:
            expires_i = -1
        cookie: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "httpOnly": http_only,
        }
        if expires_i > 0:
            cookie["expires"] = expires_i
        if secure.upper() == "TRUE":
            cookie["secure"] = True
        cookies.append(cookie)
    return cookies


def load_cookies_file(path: str | Path) -> list[dict]:
    """读取 cookie 文件：自动识别 Netscape 或 Playwright storage_state JSON。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"cookie 文件不存在：{p}")
    text = p.read_text(encoding="utf-8", errors="ignore").strip()

    if text.startswith("{"):
        data = json.loads(text)
        # Playwright storage_state 格式：{"cookies": [...], "origins": [...]}
        if isinstance(data, dict) and "cookies" in data:
            return list(data["cookies"])
        if isinstance(data, list):
            return data
        raise ValueError("JSON 里没找到 cookies 字段")

    cookies = parse_netscape_cookies(text)
    if not cookies:
        raise ValueError(f"没能从 {p.name} 解析出任何 cookie，请确认是 Netscape 格式")
    return cookies


def filter_by_urls(cookies: list[dict], urls: list[str]) -> list[dict]:
    """只保留与目标链接相关的 cookie，避免把无关域的凭证也带上。"""
    if not urls:
        return cookies
    hosts = {urlparse(u).netloc.lower() for u in urls}
    kept: list[dict] = []
    for c in cookies:
        domain = (c.get("domain") or "").lower().lstrip(".")
        if not domain:
            continue
        for host in hosts:
            if host == domain or host.endswith("." + domain):
                kept.append(c)
                break
    return kept


def to_netscape_cookies(cookies: list[dict]) -> str:
    """把 cookie 列表导出成 Netscape cookies.txt 文本（便于复制粘贴）。"""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain") or ""
        if not domain:
            continue
        name = c.get("name") or ""
        value = c.get("value") or ""
        if not name:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append(
            f"{prefix}{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}"
        )
    return "\n".join(lines) + "\n"

