from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[^\s<>\"'）)\]】，。；]+", re.IGNORECASE)


@dataclass
class Options:
    """一次抓取任务的全部可调参数。"""

    # 抓取
    timeout: float = 25.0
    retries: int = 2
    concurrency: int = 6
    # 同一域名的最小请求间隔（秒）。0 表示不限制。
    # 对 36kr 这类有频率风控的站点，把它调大比整体降并发更有效。
    per_host_delay: float = 1.0
    # 是否让同一域名的请求串行执行（配合 per_host_delay 组成礼貌抓取）
    serialize_per_host: bool = True
    # 命中站点策略表的"反爬站点"是否强制一条一条跑（最保守）
    serial_sensitive: bool = True
    # 先用静态抓取；正文抓不到或被拦截时回退到 Playwright
    use_browser: bool = True
    browser_wait: int = 2500  # 毫秒，最短等待时间
    browser_max_wait: int = 12000  # 毫秒，等待正文出现并稳定的上限
    headless: bool = True
    user_agent: str = DEFAULT_UA
    # 用户浏览器里导出的 cookie（Playwright 格式的 dict 列表），
    # 用来复用已经通过 WAF 验证 / 已登录的会话
    cookies: list[dict] = field(default_factory=list)

    # 提取
    include_images: bool = False
    include_links: bool = True
    min_text_length: int = 80  # 少于此长度视为提取失败

    # 图片本地化（默认关闭，需要时显式打开）
    download_images: bool = False
    max_image_bytes: int = 8 * 1024 * 1024
    image_timeout: float = 20.0

    # 输出
    formats: tuple[str, ...] = ("docx", "md", "xlsx", "json")
    keep_failed_in_doc: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass
class Item:
    """一条链接的抓取结果。"""

    url: str
    title: str = ""
    author: str = ""
    sitename: str = ""
    date: str = ""
    summary: str = ""
    content_html: str = ""  # trafilatura 输出的正文 HTML（图片已改写为本地路径）
    content_text: str = ""
    fetch_mode: str = ""  # static / browser
    status: str = "pending"  # ok / partial / failed
    error: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_row(self) -> dict[str, Any]:
        return {
            "标题": self.title or "(无标题)",
            "作者": self.author,
            "站点": self.sitename,
            "发布时间": self.date,
            "链接": self.url,
            "摘要": self.summary,
            "正文长度": len(self.content_text),
            "抓取方式": self.fetch_mode,
            "状态": self.status,
            "错误": self.error,
        }
