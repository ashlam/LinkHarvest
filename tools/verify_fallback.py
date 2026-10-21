"""快速验证：用新的回退逻辑跑之前失败的链接。"""

import sys

sys.path.insert(0, ".")

from linkharvest.extract import extract_content, probe_text_length
from linkharvest.fetch import Fetcher
from linkharvest.models import Options

URLS = [
    "https://36kr.com/p/3868228456011012",
    "https://mp.weixin.qq.com/s/Jt0EMh-5SBSQigCz7xownw",
    "https://www.toutiao.com/article/7695286901158429186/",
    "https://wap.cyol.com/commonDetail/1406991",
    "https://wallstreetcn.com/livenews/3146149",
    "https://www.huxiu.com/article/4715388.html",
]

opts = Options()
fetcher = Fetcher(opts)
try:
    for url in URLS:
        result = fetcher.fetch(url, probe_text_length)
        if not result.ok:
            print(f"{'FAIL':8} {url}  {result.error[:70]}")
            continue
        meta, body = extract_content(result.html, url, opts)
        title = (meta.get("title") or "")[:44]
        print(f"{result.mode:8} text={len(body):>6}  {title}")
finally:
    fetcher.close()
