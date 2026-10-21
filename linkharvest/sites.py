"""已知反爬站点的策略表。

为什么需要这张表：反爬站点和普通站点要区别对待。对普通站点，多线程并发是最优解；
但对 36kr 这类有 IP 级频率风控的站点，并发只会更快把 IP 打进黑名单，
而且封了之后连人工浏览器访问都会被拦，代价很高。

所以这里为每个已知站点单独配置：
  - serial       是否串行处理（同一站点一次只发一个请求）
  - delay        两次请求之间的最小间隔（秒）
  - browser_first 是否直接用浏览器（静态抓取注定失败、只会白白消耗请求配额的站点）
  - note         为什么这么配，给人看的

表可以通过 --site-policy 传入 JSON 覆盖或扩充，见 README。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class SitePolicy:
    """单个站点的抓取策略。"""

    host: str
    serial: bool = True
    delay: float = 3.0
    browser_first: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return f"串行/{self.delay:g}s" if self.serial else f"并发/{self.delay:g}s"


# 内置表。按域名后缀匹配，www./m./news. 等子域都会命中。
BUILTIN_SITES: tuple[SitePolicy, ...] = (
    SitePolicy(
        host="36kr.com",
        serial=True,
        delay=5.0,
        note="IP 级频率风控：密集请求会返回 403 空响应，且连人工浏览器也被拦，需过验证才恢复",
    ),
    SitePolicy(
        host="huxiu.com",
        serial=True,
        delay=5.0,
        browser_first=True,
        note="阿里云 WAF 滑块：指纹层面拦新浏览器，换 UA/有头无头都无效，"
             "需 --cookies 复用已验证会话",
    ),
    SitePolicy(
        host="zhihu.com",
        serial=True,
        delay=4.0,
        note="安全验证/登录墙，未登录时正文拿不到",
    ),
    SitePolicy(
        host="xiaohongshu.com",
        serial=True,
        delay=4.0,
        note="强制登录 + 风控，公开抓取基本拿不到正文",
    ),
    SitePolicy(
        host="weibo.com",
        serial=True,
        delay=3.0,
        note="未登录会被跳转到登录页",
    ),
    SitePolicy(
        host="douban.com",
        serial=True,
        delay=3.0,
        note="对高频访问返回 403，需要低频",
    ),
    SitePolicy(
        host="toutiao.com",
        serial=False,
        delay=2.0,
        browser_first=True,
        note="正文靠 JS 渲染，静态页是空壳；浏览器可正常拿到",
    ),
    SitePolicy(
        host="jianshu.com",
        serial=True,
        delay=2.0,
        note="有频率限制，过快会返回验证页",
    ),
    SitePolicy(
        host="csdn.net",
        serial=False,
        delay=1.5,
        note="有反爬，但容忍度较高，适当放慢即可",
    ),
    SitePolicy(
        host="wallstreetcn.com",
        serial=True,
        delay=2.0,
        browser_first=True,
        note="正文由 JS 渲染，静态页为空",
    ),
    SitePolicy(
        host="mp.weixin.qq.com",
        serial=True,
        delay=2.0,
        note="单页 HTML 可达数 MB，建议串行避免内存压力；正文可正常提取",
    ),
    SitePolicy(
        host="caixin.com",
        serial=True,
        delay=3.0,
        note="付费墙，未订阅只能拿到摘要",
    ),
    SitePolicy(
        host="guancha.cn",
        serial=False,
        delay=1.5,
        note="正文结构较杂，需容器回退提取",
    ),
)


class SiteRegistry:
    """站点策略表：内置规则 + 用户 JSON 覆盖。"""

    def __init__(self, policies: list[SitePolicy] | None = None) -> None:
        self._policies: dict[str, SitePolicy] = {}
        for policy in policies or BUILTIN_SITES:
            self._policies[self._norm(policy.host)] = policy

    @staticmethod
    def _norm(host: str) -> str:
        host = host.lower().strip()
        for prefix in ("www.", "m.", "wap.", "news."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host

    def match(self, url: str) -> SitePolicy | None:
        """按域名后缀匹配策略；没有匹配返回 None（走默认并发）。"""
        try:
            host = self._norm(urlparse(url).netloc)
        except Exception:  # noqa: BLE001
            return None
        if not host:
            return None
        # 先精确匹配，再按后缀匹配（news.youth.cn 这类）
        if host in self._policies:
            return self._policies[host]
        best: SitePolicy | None = None
        best_len = 0
        for pattern, policy in self._policies.items():
            if host == pattern or host.endswith("." + pattern):
                if len(pattern) > best_len:
                    best, best_len = policy, len(pattern)
        return best

    def all(self) -> list[SitePolicy]:
        return sorted(self._policies.values(), key=lambda p: p.host)

    def merge_file(self, path: str | Path) -> None:
        """从 JSON 合并自定义策略。

        格式：{"example.com": {"delay": 5, "serial": true, "browser_first": false,
                              "note": "..."}}
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("站点策略文件应为 {host: {...}} 形式的对象")
        for host, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            key = self._norm(host)
            base = self._policies.get(key) or SitePolicy(host=key)
            self._policies[key] = replace(
                base,
                host=key,
                serial=bool(cfg.get("serial", base.serial)),
                delay=float(cfg.get("delay", base.delay)),
                browser_first=bool(cfg.get("browser_first", base.browser_first)),
                note=str(cfg.get("note", base.note)),
            )


def format_table(registry: SiteRegistry) -> str:
    """给 CLI 展示用的表格。"""
    rows = registry.all()
    if not rows:
        return "（站点策略表为空）"
    w_host = max(len(r.host) for r in rows)
    w_pol = max(len(r.label) for r in rows)
    lines = [f"{'站点'.ljust(w_host)}  {'策略'.ljust(w_pol)}  说明", "-" * (w_host + w_pol + 30)]
    for r in rows:
        lines.append(f"{r.host.ljust(w_host)}  {r.label.ljust(w_pol)}  {r.note}")
    return "\n".join(lines)
