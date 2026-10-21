"""交互式会话捕获：弹出可见浏览器，让用户手动过验证/登录，然后把会话存下来。

这是对付阿里云 WAF 滑块这类"只拦机器人、不拦真人"的站点最可靠的办法：
人过一次，工具把 cookie 存下来反复用。

关键点：抓取用的隐藏浏览器和用户日常浏览器是**两个完全独立的实例**，
不共享 cookie。所以用户在 Edge 里手动过验证，对抓取没有任何帮助——
必须让工具自己持有那份放行 cookie。
"""

from __future__ import annotations

import json
from pathlib import Path


def capture_session(
    url: str,
    save_path: str | Path,
    user_agent: str,
    existing_cookies: list[dict] | None = None,
    timeout_seconds: int = 300,
) -> bool:
    """打开可见浏览器让用户操作，用户确认后保存 storage_state。

    existing_cookies: 已有的 cookie，会先注入再打开页面。
    这样在"部分站点已通过、部分还没"时，可以只补验缺的那个，
    而不会把已经拿到的凭证弄丢。

    返回 True 表示已保存。
    """
    from playwright.sync_api import sync_playwright

    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n正在打开浏览器：{url}")
    print("请在弹出的窗口里完成验证/登录（比如拖动滑块），")
    print("确认页面能正常显示正文后，回到这里按【回车】保存会话。\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=user_agent,
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        if existing_cookies:
            try:
                context.add_cookies(existing_cookies)
                print(f"（已带上现有的 {len(existing_cookies)} 条 cookie）")
            except Exception as exc:  # noqa: BLE001
                print(f"（现有 cookie 注入失败，忽略：{exc}）")

        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"打开页面时出错（可继续手动操作）：{exc}")

        try:
            input(">>> 完成后按回车保存会话：")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            browser.close()
            return False

        state = context.storage_state()
        cookies = state.get("cookies", [])
        save_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()

    print(f"\n已保存 {len(cookies)} 条 cookie 到 {save_path}")
    print(f"下次这样用：python -m linkharvest links.txt --cookies {save_path.name}\n")
    return bool(cookies)


def load_session_cookies(path: str | Path) -> list[dict]:
    """读取已保存的会话 cookie，供捕获时复用。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("cookies", []))
        if isinstance(data, list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return []
