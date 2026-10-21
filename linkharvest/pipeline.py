"""并发流水线：输入链接列表 → 抓取 → 提取 → 图片本地化 → 汇总结果。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from .extract import ImageDownloader, extract_content, html_to_text, probe_text_length
from .fetch import Fetcher
from .models import Item, Options, URL_RE
from .sites import SiteRegistry


def parse_links(raw: str | Iterable[str]) -> list[str]:
    """从任意文本里抽链接：支持一行一个、Markdown 列表、粘贴的一整段文字。"""
    if isinstance(raw, str):
        text = raw
    else:
        text = "\n".join(raw)
    found = URL_RE.findall(text)
    seen: set[str] = set()
    links: list[str] = []
    for link in found:
        link = link.rstrip(".,;:，。；：、")
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


class Harvest:
    """一次抓取任务的执行器。"""

    def __init__(
        self,
        opts: Options | None = None,
        on_progress: Callable[[int, int, Item], None] | None = None,
        registry: SiteRegistry | None = None,
    ) -> None:
        self.opts = opts or Options()
        self.on_progress = on_progress
        self.registry = registry or SiteRegistry()
        self._progress_lock = threading.Lock()
        self._done = 0
        self._fetcher: Fetcher | None = None

    def fetcher_breaker_state(self) -> dict[str, str]:
        """返回本次任务里被熔断的域名及原因，供 CLI/网页展示。"""
        fetcher = self._fetcher
        return fetcher.breaker.tripped_hosts() if fetcher else {}

    def plan(self, links: list[str]) -> tuple[list[str], list[str]]:
        """把链接分成两批：普通站点（可全并发）和已知反爬站点（需谨慎）。

        反爬站点放到最后单独跑，理由：
        1. 万一触发风控，前面的正常站点已经抓完了，不会白跑；
        2. 这些站点之间各有独立的 IP 风控，串行跑能避免相互加剧。
        """
        normal: list[str] = []
        sensitive: list[str] = []
        for url in links:
            if self.registry.match(url) is not None:
                sensitive.append(url)
            else:
                normal.append(url)
        return normal, sensitive

    def run(self, links: list[str], image_dir: Path) -> list[Item]:
        opts = self.opts
        total = len(links)
        items: list[Item] = []
        if not total:
            return items

        fetcher = Fetcher(opts, self.registry)
        self._fetcher = fetcher
        downloader = ImageDownloader(opts, image_dir) if opts.download_images else None
        normal, sensitive = self.plan(links)

        def work(url: str) -> Item:
            return self._process(url, fetcher, downloader)

        def run_batch(batch: list[str], workers: int) -> None:
            if not batch:
                return
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {pool.submit(work, url): url for url in batch}
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        item = future.result()
                    except Exception as exc:  # noqa: BLE001
                        item = Item(url=url, status="failed", error=f"{type(exc).__name__}: {exc}")
                    items.append(item)
                    with self._progress_lock:
                        self._done += 1
                        if self.on_progress:
                            self.on_progress(self._done, total, item)

        try:
            # 先跑普通站点（全并发），再跑已知反爬站点（受限并发）
            run_batch(normal, min(opts.concurrency, len(normal) or 1))
            sensitive_workers = 1 if opts.serial_sensitive else min(opts.concurrency, 3)
            run_batch(sensitive, sensitive_workers)
        finally:
            fetcher.close()
            if downloader:
                downloader.close()

        order = {url: i for i, url in enumerate(links)}
        items.sort(key=lambda it: order.get(it.url, 0))
        return items

    # ------------------------------------------------------------------
    def _process(self, url: str, fetcher: Fetcher, downloader: ImageDownloader | None) -> Item:
        opts = self.opts
        item = Item(url=url)
        result = fetcher.fetch(url, probe_text_length)
        if not result.ok or not result.html:
            item.status = "failed"
            item.error = result.error or "抓取失败"
            item.title = url
            return item
        item.fetch_mode = result.mode
        meta, body_html = extract_content(result.html, url, opts)
        item.title = meta["title"]
        item.author = meta["author"]
        item.sitename = meta["sitename"]
        item.date = meta["date"]
        item.summary = meta["summary"]

        if downloader and body_html:
            body_html, images = downloader.localize(body_html, url)
            item.images = images

        item.content_html = body_html
        item.content_text = html_to_text(body_html)

        if not item.title:
            item.title = url
        if len(item.content_text) < opts.min_text_length:
            item.status = "partial"
            item.error = item.error or f"正文过短（{len(item.content_text)} 字），可能需要登录或页面结构特殊"
        else:
            item.status = "ok"
        return item
