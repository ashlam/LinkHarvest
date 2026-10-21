"""验证站点策略表：域名匹配、分批调度、限速参数生效。

注意：本脚本不访问任何真实反爬站点，只用桩数据验证逻辑。
"""

import sys

sys.path.insert(0, ".")

from linkharvest.models import Options
from linkharvest.pipeline import Harvest, parse_links
from linkharvest.sites import BUILTIN_SITES, SitePolicy, SiteRegistry, format_table


def test_match():
    print("=== 1. 域名匹配（子域、www、大小写） ===")
    reg = SiteRegistry()
    cases = [
        ("https://36kr.com/p/123", "36kr.com"),
        ("https://www.36kr.com/p/123", "36kr.com"),
        ("https://m.36kr.com/p/123", "36kr.com"),
        ("https://36KR.COM/p/123", "36kr.com"),
        ("https://www.huxiu.com/article/1.html", "huxiu.com"),
        ("https://mp.weixin.qq.com/s/xxx", "mp.weixin.qq.com"),
        ("https://realpython.com/x", None),
        ("https://example.com", None),
    ]
    allok = True
    for url, expect in cases:
        policy = reg.match(url)
        got = policy.host if policy else None
        ok = got == expect
        allok &= ok
        print(f"  {'OK ' if ok else 'BAD'} {url:45} -> {got}")
    print(f"  结果：{'通过' if allok else '有失败'}\n")
    return allok


def test_plan():
    print("=== 2. 分批调度（反爬站点排到最后） ===")
    links = parse_links("""
        https://realpython.com/a
        https://36kr.com/p/1
        https://example.com
        https://www.huxiu.com/article/1.html
        https://github.com/x
        https://36kr.com/p/2
    """)
    harvest = Harvest(Options())
    normal, sensitive = harvest.plan(links)
    print(f"  普通站点 {len(normal)} 条：{normal}")
    print(f"  反爬站点 {len(sensitive)} 条：{sensitive}")
    ok = len(normal) == 3 and len(sensitive) == 3
    print(f"  结果：{'通过' if ok else '有失败'}\n")
    return ok


def test_throttle_params():
    print("=== 3. 限速参数按站点策略收紧 ===")
    from linkharvest.fetch import HostThrottle

    reg = SiteRegistry()
    th = HostThrottle(delay=1.0, serialize=False, registry=reg)
    cases = [
        ("https://36kr.com/p/1", 5.0, True),   # 表里 5s + 串行
        ("https://example.com", 1.0, False),   # 全局默认
    ]
    ok = True
    for url, want_delay, want_serial in cases:
        delay, serial = th.effective(url)
        good = delay >= want_delay and serial == want_serial
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} {url:32} delay={delay} serial={serial}"
              f"（期望 >={want_delay} / {want_serial}）")
    print(f"  结果：{'通过' if ok else '有失败'}\n")
    return ok


def test_custom_policy():
    print("=== 4. 自定义策略覆盖内置 ===")
    import json
    import tempfile
    from pathlib import Path

    reg = SiteRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.json"
        path.write_text(json.dumps({
            "36kr.com": {"delay": 10, "serial": True, "note": "我自己调更慢"},
            "mysite.example": {"delay": 7, "browser_first": True, "note": "新加的站"},
        }), encoding="utf-8")
        reg.merge_file(path)
    p36 = reg.match("https://36kr.com/p/1")
    pmine = reg.match("https://mysite.example/a")
    ok = p36.delay == 10 and p36.note == "我自己调更慢" and pmine and pmine.delay == 7
    print(f"  36kr 覆盖后 delay={p36.delay} note={p36.note!r}")
    print(f"  新增站点 mysite.example delay={pmine.delay} browser_first={pmine.browser_first}")
    print(f"  结果：{'通过' if ok else '有失败'}\n")
    return ok


if __name__ == "__main__":
    results = [
        test_match(),
        test_plan(),
        test_throttle_params(),
        test_custom_policy(),
    ]
    print("=" * 50)
    print("全部通过" if all(results) else "存在失败项")
