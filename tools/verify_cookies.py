"""验证 cookie 加载、过滤、注入逻辑（不依赖真实站点）。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from linkharvest.cookies import (
    filter_by_urls,
    load_cookies_file,
    parse_netscape_cookies,
)
from linkharvest.fetch import _cookie_header

NETSCAPE = """# Netscape HTTP Cookie File
.huxiu.com\tTRUE\t/\tFALSE\t1893456000\tacw_tc\tabc123
.huxiu.com\tTRUE\t/\tTRUE\t1893456000\t_c_WBKFRo\ttoken456
.example.com\tTRUE\t/\tFALSE\t1893456000\tsid\tsid789
#HttpOnly_www.huxiu.com\tTRUE\t/\tFALSE\t1893456000\t_nb_ioWEgULi\tnb999
"""


def test_parse():
    print("=== 1. 解析 Netscape cookies.txt ===")
    cookies = parse_netscape_cookies(NETSCAPE)
    print(f"  解析出 {len(cookies)} 条")
    for c in cookies:
        print(f"    {c['domain']:20} {c['name']:16} value={c['value']} "
              f"secure={c.get('secure', False)}")
    ok = len(cookies) == 4 and any(c["name"] == "acw_tc" for c in cookies)
    print(f"  结果：{'通过' if ok else '失败'}\n")
    return ok


def test_filter():
    print("=== 2. 按目标链接过滤 cookie ===")
    cookies = parse_netscape_cookies(NETSCAPE)
    kept = filter_by_urls(cookies, ["https://www.huxiu.com/article/1.html"])
    print(f"  目标 huxiu，保留 {len(kept)} 条：{[c['name'] for c in kept]}")
    ok = len(kept) == 3 and all("huxiu" in c["domain"] for c in kept)
    print(f"  结果：{'通过（example.com 被正确排除）' if ok else '失败'}\n")
    return ok


def test_header():
    print("=== 3. 生成 Cookie 请求头 ===")
    cookies = parse_netscape_cookies(NETSCAPE)
    header = _cookie_header("https://www.huxiu.com/article/1.html", cookies)
    print(f"  header = {header!r}")
    ok = "acw_tc=abc123" in header and "sid=" not in header
    print(f"  结果：{'通过' if ok else '失败'}\n")
    return ok


def test_storage_state():
    print("=== 4. 读取 Playwright storage_state JSON ===")
    state = {
        "cookies": [
            {"name": "acw_tc", "value": "x1", "domain": ".huxiu.com", "path": "/",
             "expires": 1893456000, "httpOnly": False, "secure": False, "sameSite": "Lax"},
        ],
        "origins": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = load_cookies_file(path)
    print(f"  读取 {len(loaded)} 条：{loaded[0]['name']}={loaded[0]['value']}")
    ok = len(loaded) == 1 and loaded[0]["name"] == "acw_tc"
    print(f"  结果：{'通过' if ok else '失败'}\n")
    return ok


def test_invalid():
    print("=== 5. 非法文件应报错而不是静默通过 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.txt"
        path.write_text("这不是 cookie 文件", encoding="utf-8")
        try:
            load_cookies_file(path)
            print("  未报错（不符合预期）")
            ok = False
        except ValueError as exc:
            print(f"  正确报错：{exc}")
            ok = True
    print(f"  结果：{'通过' if ok else '失败'}\n")
    return ok


if __name__ == "__main__":
    results = [
        test_parse(),
        test_filter(),
        test_header(),
        test_storage_state(),
        test_invalid(),
    ]
    print("=" * 50)
    print("全部通过" if all(results) else "存在失败项")
