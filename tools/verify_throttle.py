"""验证限速与熔断机制（不碰 36kr）。

1. 同一域名的 N 个请求是否被拉开到 >= per_host_delay 间隔
2. 不同域名是否仍能并行（不被无谓地串行化）
3. 熔断：给一个必然返回 403 的域名，确认只发 1 次请求就停手
"""

import sys
import threading
import time

sys.path.insert(0, ".")

from linkharvest.extract import probe_text_length
from linkharvest.fetch import Fetcher
from linkharvest.models import Options


def test_throttle():
    print("=== 1. 同域限速（4 个同域链接，delay=1.0s）===")
    opts = Options(per_host_delay=1.0, serialize_per_host=True, use_browser=False)
    fetcher = Fetcher(opts)
    urls = [
        "https://example.com",
        "https://example.com/?a=1",
        "https://example.com/?a=2",
        "https://example.com/?a=3",
    ]
    stamps = []
    lock = threading.Lock()

    def run(u):
        t0 = time.time()
        fetcher.fetch(u, probe_text_length)
        with lock:
            stamps.append((t0, time.time()))

    started = time.time()
    threads = [threading.Thread(target=run, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.time() - started
    fetcher.close()

    stamps.sort()
    gaps = [round(stamps[i + 1][0] - stamps[i][0], 2) for i in range(len(stamps) - 1)]
    print(f"  总耗时 {total:.1f}s，相邻请求起始间隔 {gaps}")
    print(f"  期望：间隔 >= 1.0s，总耗时 >= 3.0s")
    print(f"  结果：{'通过' if total >= 2.8 else '未生效'}")


def test_parallel_hosts():
    print("\n=== 2. 不同域名仍可并行（3 个不同域名）===")
    opts = Options(per_host_delay=2.0, serialize_per_host=True, use_browser=False)
    fetcher = Fetcher(opts)
    urls = ["https://example.com", "https://example.org", "https://example.net"]
    started = time.time()
    threads = [threading.Thread(target=lambda u=u: fetcher.fetch(u, probe_text_length)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.time() - started
    fetcher.close()
    print(f"  总耗时 {total:.1f}s（若串行化会 >= 4s）")
    print(f"  结果：{'通过，未互相阻塞' if total < 3.5 else '被串行化了'}")


def test_breaker():
    print("\n=== 3. 熔断：httpbin 的 403 端点，3 个同域链接 ===")
    opts = Options(per_host_delay=0, retries=2, use_browser=True)
    fetcher = Fetcher(opts)
    urls = [
        "https://httpbin.org/status/403",
        "https://httpbin.org/status/403?a=1",
        "https://httpbin.org/status/403?a=2",
    ]
    started = time.time()
    for u in urls:
        result = fetcher.fetch(u, probe_text_length)
        print(f"  {result.blocked=} {result.status_code=} err={result.error[:50]}")
    elapsed = time.time() - started
    print(f"  总耗时 {elapsed:.1f}s，熔断域名: {fetcher.breaker.tripped_hosts()}")
    fetcher.close()
    print(f"  结果：{'通过（后续请求被跳过）' if elapsed < 10 else '未熔断'}")


if __name__ == "__main__":
    test_throttle()
    test_parallel_hosts()
    test_breaker()
