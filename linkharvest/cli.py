"""命令行入口：linkharvest links.txt -o out"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .cookies import filter_by_urls, load_cookies_file
from .export import export_all
from .models import Options
from .pipeline import Harvest, parse_links
from .sites import SiteRegistry, format_table

BANNER = "LinkHarvest · 链接 → 文档"


def _progress(done: int, total: int, item) -> None:
    mark = {"ok": "✓", "partial": "!", "failed": "✗"}.get(item.status, "·")
    title = (item.title or item.url)[:48]
    print(f"[{done}/{total}] {mark} {title}  <{item.url[:70]}>", flush=True)


def _first_link_or_none(source: str) -> str | None:
    """从链接文件里取第一个链接，文件不存在时返回 None。"""
    try:
        src = Path(source)
        if not src.exists():
            return None
        links = parse_links(src.read_text(encoding="utf-8", errors="ignore"))
        return links[0] if links else None
    except Exception:  # noqa: BLE001
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="linkharvest",
        description="抓取一批链接的标题/正文/时间，导出为 Word、Markdown、Excel、JSON。",
    )
    p.add_argument("source", nargs="?", help="包含链接的 txt/md 文件；省略则从标准输入读取")
    p.add_argument("-o", "--out", default="output", help="输出目录（默认 output）")
    p.add_argument("-n", "--name", default=None, help="输出文件名（不含扩展名），默认按时间戳生成")
    p.add_argument("-f", "--formats", default="docx,md,xlsx,json", help="输出格式，逗号分隔：docx,md,xlsx,json")
    p.add_argument("-c", "--concurrency", type=int, default=6, help="并发数（默认 6）")
    p.add_argument(
        "--host-delay",
        type=float,
        default=1.0,
        help="同一域名两次请求的最小间隔秒数（默认 1.0；设为 0 关闭限速）",
    )
    p.add_argument(
        "--no-host-serialize",
        action="store_true",
        help="不强制同一域名串行（默认串行，配合 --host-delay 更不容易触发风控）",
    )
    p.add_argument("--timeout", type=float, default=25.0, help="单页超时秒数（默认 25）")
    p.add_argument("--retries", type=int, default=2, help="静态抓取重试次数（默认 2）")
    p.add_argument("--no-browser", action="store_true", help="禁用 Playwright 回退（更快，但 JS 页面会失败）")
    p.add_argument("--images", action="store_true", help="下载正文图片并嵌入文档（默认不下载）")
    p.add_argument("--show-browser", action="store_true", help="浏览器回退时显示窗口（调试用）")
    p.add_argument(
        "--site-policy",
        default=None,
        help="自定义站点策略 JSON 文件，覆盖/扩充内置反爬站点表",
    )
    p.add_argument("--list-sites", action="store_true", help="打印内置反爬站点策略表后退出")
    p.add_argument(
        "--no-sensitive-serial",
        action="store_true",
        help="不强制反爬站点串行（默认串行，最保守）",
    )
    p.add_argument(
        "--cookies",
        default=None,
        help="cookie 文件（浏览器扩展导出的 cookies.txt，或 --save-session 生成的 JSON）",
    )
    p.add_argument(
        "--save-session",
        default=None,
        metavar="PATH",
        help="弹出可见浏览器让你手动过验证/登录，成功后把会话保存到 PATH，供 --cookies 复用",
    )
    p.add_argument(
        "--login-url",
        default=None,
        help="配合 --save-session：先打开这个地址让你操作（默认取第一个链接）",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="不打印每条进度")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    registry = SiteRegistry()
    if args.site_policy:
        try:
            registry.merge_file(args.site_policy)
        except Exception as exc:  # noqa: BLE001
            print(f"读取站点策略文件失败：{exc}", file=sys.stderr)
            return 2

    if args.list_sites:
        print("内置反爬站点策略表（命中后自动放慢、必要时串行）：\n")
        print(format_table(registry))
        print("\n用 --site-policy custom.json 可覆盖或扩充，格式：")
        print('  {"example.com": {"delay": 6, "serial": true, "browser_first": false,'
              ' "note": "为什么这么配"}}')
        return 0

    # 交互式捕获会话：只需要一个目标地址，不依赖链接列表是否存在。
    # 先处理它，这样 `--save-session x.json --login-url <url>` 可以单独使用。
    if args.save_session:
        from .session import capture_session

        target = args.login_url
        if not target and args.source:
            target = _first_link_or_none(args.source)
        if not target:
            print(
                "用 --save-session 时需要指定要打开的地址：\n"
                "  python -m linkharvest --save-session session.json --login-url https://example.com/article/1",
                file=sys.stderr,
            )
            return 2
        ok = capture_session(target, args.save_session, Options().user_agent)
        if not ok:
            return 2
        if not args.cookies:
            args.cookies = args.save_session
        # 只捕获会话、不抓取
        if args.login_url and not args.source:
            return 0

    if args.source:
        src = Path(args.source)
        if not src.exists():
            print(
                f"找不到链接文件：{src}\n"
                "提示：把链接一行一个写进这个文件，或用 --login-url 直接指定地址。",
                file=sys.stderr,
            )
            return 2
        raw = src.read_text(encoding="utf-8", errors="ignore")
    else:
        if sys.stdin.isatty():
            print("请把链接粘贴进来，Ctrl+Z 回车（Windows）或 Ctrl+D（macOS/Linux）结束：", file=sys.stderr)
        raw = sys.stdin.read()

    links = parse_links(raw)
    if not links:
        print("没有在输入里找到任何 http(s) 链接。", file=sys.stderr)
        return 2

    cookies: list[dict] = []
    if args.cookies:
        try:
            cookies = load_cookies_file(args.cookies)
            cookies = filter_by_urls(cookies, links)
        except Exception as exc:  # noqa: BLE001
            print(f"读取 cookie 失败：{exc}", file=sys.stderr)
            return 2
        if not cookies:
            print(
                "警告：cookie 文件里没有与目标链接匹配的条目，将不带 cookie 抓取。",
                file=sys.stderr,
            )
        else:
            print(f"已加载 {len(cookies)} 条 cookie。\n", flush=True)

    formats = tuple(f.strip().lower() for f in args.formats.split(",") if f.strip())
    opts = Options(
        timeout=args.timeout,
        retries=args.retries,
        concurrency=args.concurrency,
        per_host_delay=args.host_delay,
        serialize_per_host=not args.no_host_serialize,
        serial_sensitive=not args.no_sensitive_serial,
        use_browser=not args.no_browser,
        headless=not args.show_browser,
        download_images=args.images,
        include_images=args.images,
        cookies=cookies,
        formats=formats,
    )

    out_dir = Path(args.out).resolve()
    image_dir = out_dir / "images"
    name = args.name or time.strftime("%Y%m%d-%H%M%S")

    print(f"{BANNER}\n共 {len(links)} 条链接 → {out_dir}\n", flush=True)
    started = time.time()
    harvest = Harvest(opts, on_progress=None if args.quiet else _progress, registry=registry)

    normal, sensitive = harvest.plan(links)
    if sensitive:
        hosts = sorted({urlparse(u).netloc for u in sensitive})
        print(f"检测到 {len(sensitive)} 条来自已知反爬站点（{', '.join(hosts)}），"
              f"将在普通站点之后单独低速处理。\n", flush=True)

    items = harvest.run(links, image_dir)
    produced = export_all(items, out_dir, image_dir, opts, name)

    ok = sum(1 for it in items if it.status == "ok")
    partial = sum(1 for it in items if it.status == "partial")
    failed = len(items) - ok - partial
    print(
        f"\n完成：成功 {ok}，部分 {partial}，失败 {failed}，用时 {time.time() - started:.1f}s",
        flush=True,
    )
    tripped = harvest.fetcher_breaker_state()
    if tripped:
        print("\n以下域名触发了反爬拦截，已提前停止请求（等一段时间再重跑这几条）：", flush=True)
        for host, reason in tripped.items():
            print(f"  {host}  —— {reason}", flush=True)
    for kind, path in produced.items():
        print(f"  {kind:<12} {path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
