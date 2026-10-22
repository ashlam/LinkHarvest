"""抓取层：先用 httpx 静态抓，拿不到有效内容再回退到 Playwright 无头浏览器。

Playwright 的同步 API 不是线程安全的，所以浏览器抓取统一由一个后台线程串行处理，
上层多线程 worker 只是把任务丢进队列并等待结果。
"""

from __future__ import annotations

import queue
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .models import Options

# 命中这些特征说明拿到的是拦截页/验证页，而不是正文
BLOCK_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "cf-browser-verification",
    "安全验证",
    "环境异常",
    "请完成验证",
    "访问过于频繁",
    "滑动验证",
    "verify you are human",
    "attention required",
    "人机验证",
    # 国内站点常见的 JS 挑战页：页面本身有几十 KB，但正文是空的
    "正在进行安全检测",
    "系统正在检测当前网络环境",
    "该过程通常需要几秒钟",
    "请开启javascript",
    "请启用javascript",
    "enable javascript to continue",
)

BLOCK_STATUS = {401, 403, 407, 429, 451, 503}


@dataclass
class FetchResult:
    url: str
    html: str = ""
    mode: str = ""  # static / browser
    status_code: int = 0
    error: str = ""
    # 是否为"硬性拦截"（403/429/验证页）：这类失败重试无益，应立即熔断该域名
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.error


def _looks_blocked(status_code: int, html: str) -> bool:
    """判断响应是否是拦截页/验证页。

    注意：只看前 6000 字符，因为很多站点正文前面就有大量脚本。
    页面大小不能作为判据——36kr 这类站点的 JS 挑战页有几十 KB。
    """
    if status_code in BLOCK_STATUS:
        return True
    if not html:
        return True
    head = html[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS)


class HostCircuitBreaker:
    """按域名的熔断器：一旦某个域名返回硬性拦截（403 等），立即停手。

    这是从一次真实教训来的：对 36kr 连续发几十次请求做实验，结果整个 IP
    被风控封掉，连手动浏览器访问都变成 403，要等很久才恢复。

    所以遇到 403 这类"不是重试能解决"的拦截时，正确做法是**立刻放弃该域名的
    所有后续请求**——重试、换浏览器、换 UA 都只会让封锁更重、更久。
    已经拿到正文的条目不受影响，剩下的直接标记为"域名已被限流"。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped: dict[str, str] = {}

    def is_open(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        with self._lock:
            return host in self._tripped

    def reason(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        with self._lock:
            return self._tripped.get(host, "")

    def trip(self, url: str, reason: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            self._tripped.setdefault(host, reason)

    def tripped_hosts(self) -> dict[str, str]:
        with self._lock:
            return dict(self._tripped)


class HostThrottle:
    """按域名限速：同一域名的请求串行 + 最小间隔。

    36kr 这类站点有频率风控，短时间密集请求会被封 IP（返回 403 空响应，
    需要人工过验证才恢复）。整体降并发能缓解，但更精准的做法是
    对同一域名放慢节奏，而不同域名之间仍然并行——既不牺牲总速度，
    又不会把单个站点打疼。

    命中站点策略表的域名会用表里的更保守参数（更长间隔、强制串行），
    而不是用全局默认值。
    """

    def __init__(
        self,
        delay: float,
        serialize: bool = True,
        registry=None,
    ) -> None:
        self.delay = max(0.0, delay)
        self.serialize = serialize
        self.registry = registry
        self._meta_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_request: dict[str, float] = {}

    def policy_for(self, url: str):
        return self.registry.match(url) if self.registry else None

    def effective(self, url: str) -> tuple[float, bool]:
        """返回该 URL 实际生效的 (间隔秒数, 是否串行)。"""
        policy = self.policy_for(url)
        if policy is None:
            return self.delay, self.serialize
        # 已知反爬站点：取两者更保守的一个
        return max(self.delay, policy.delay), self.serialize or policy.serial

    def _host_lock(self, host: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    @contextmanager
    def guard(self, url: str):
        host = urlparse(url).netloc.lower()
        delay, serialize = self.effective(url)
        lock = self._host_lock(host)
        if serialize:
            lock.acquire()
        try:
            with self._meta_lock:
                last = self._last_request.get(host, 0.0)
            wait = delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            with self._meta_lock:
                self._last_request[host] = time.time()
            if serialize:
                lock.release()


CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([\w-]+)""", re.IGNORECASE)
# 声明为 gb2312/gbk 的页面实际常用到 gb18030 的超集字符，统一按 gb18030 解码
GB_FAMILY = {"gb2312", "gbk", "gb18030", "x-gbk", "euc-cn"}


def _detect_charset(resp: httpx.Response) -> str | None:
    """按 HTTP 头 → 页面 meta → 内容探测的顺序确定编码。

    很多中文站点不发 charset 头，httpx 会退回 UTF-8，导致整页乱码；
    这里优先读 meta，最后用 charset_normalizer 做内容探测。
    """
    ctype = (resp.headers.get("content-type") or "").lower()
    m = CHARSET_RE.search(ctype.encode("latin-1", "ignore"))
    if m:
        return m.group(1).decode("ascii", "ignore")

    head = resp.content[:4096]
    m = CHARSET_RE.search(head)
    if m:
        return m.group(1).decode("ascii", "ignore")

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(resp.content).best()
        if best and best.encoding:
            return best.encoding
    except Exception:  # noqa: BLE001
        pass
    return None


def _decode(resp: httpx.Response) -> str:
    charset = _detect_charset(resp)
    if charset:
        charset = charset.lower()
        if charset in GB_FAMILY:
            charset = "gb18030"
        try:
            return resp.content.decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return resp.text


def _cookie_header(url: str, cookies: list[dict] | None) -> str:
    """把匹配该 URL 的 cookie 拼成 Cookie 请求头。"""
    if not cookies:
        return ""
    host = urlparse(url).netloc.lower()
    pairs: list[str] = []
    for c in cookies:
        domain = (c.get("domain") or "").lower().lstrip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            name, value = c.get("name"), c.get("value")
            if name and value is not None:
                pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def fetch_static(url: str, opts: Options) -> FetchResult:
    headers = {
        "User-Agent": opts.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    cookie_header = _cookie_header(url, getattr(opts, "cookies", None))
    if cookie_header:
        headers["Cookie"] = cookie_header
    last_err = ""
    for attempt in range(opts.retries + 1):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=opts.timeout,
                headers=headers,
                verify=False,
            ) as client:
                resp = client.get(url)
            html = _decode(resp)
            # 403/429 这类硬性拦截：重试只会让封锁更重，直接返回让上层熔断
            if resp.status_code in BLOCK_STATUS:
                return FetchResult(
                    url=url,
                    status_code=resp.status_code,
                    blocked=True,
                    error=f"HTTP {resp.status_code}（站点拒绝访问，已停止对该站点的请求）",
                )
            if _looks_blocked(resp.status_code, html):
                last_err = f"HTTP {resp.status_code}（疑似拦截页）"
            elif resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}"
            else:
                return FetchResult(url=url, html=html, mode="static", status_code=resp.status_code)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一兜底
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < opts.retries:
            time.sleep(0.6 * (attempt + 1))
    return FetchResult(url=url, status_code=0, error=last_err or "未知错误")


class BrowserFetcher:
    """单线程串行的 Playwright 抓取器，按需启动。"""

    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._failed_reason: str | None = None

    # ---- 生命周期 ----
    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="lh-browser", daemon=True)
            self._thread.start()

    def _start_browser(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]

        # 优先复用本机已装的 Chrome/Edge，没有再用 Playwright 自带内核。
        # 好处：打包分发时不必附带几百 MB 的浏览器，用户装了 Chrome 就能用；
        # 而且真实浏览器的指纹更像真人，不容易被 WAF 判定为机器人。
        launched = None
        last_err: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = {"channel": channel} if channel else {}
                launched = self._playwright.chromium.launch(
                    headless=self.opts.headless, args=args, **kwargs
                )
                break
            except Exception as exc:  # noqa: BLE001 - 换下一个渠道
                last_err = exc
                continue

        if launched is None:
            raise RuntimeError(
                "找不到可用的浏览器。请安装 Chrome/Edge，"
                "或执行 `python -m playwright install chromium`。"
                f"（最后一次错误：{last_err}）"
            )
        self._browser = launched
        self._context = self._browser.new_context(
            user_agent=self.opts.user_agent,
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        # 注入用户浏览器的 cookie，复用已经通过验证的会话
        if self.opts.cookies:
            try:
                self._context.add_cookies(self.opts.cookies)
            except Exception:  # noqa: BLE001 - cookie 格式问题不该让整个任务失败
                pass

    def _run(self) -> None:
        try:
            self._start_browser()
        except Exception as exc:  # noqa: BLE001
            self._failed_reason = (
                f"浏览器不可用（{type(exc).__name__}: {exc}）。"
                "请执行 `python -m playwright install chromium` 后重试。"
            )
        while True:
            job = self._queue.get()
            if job is None:
                break
            url, holder, probe = job
            try:
                if self._failed_reason:
                    holder["error"] = self._failed_reason
                else:
                    holder["result"] = self._fetch(url, probe)
            except Exception as exc:  # noqa: BLE001
                holder["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                holder["event"].set()
        self._shutdown()

    def _fetch(self, url: str, probe) -> FetchResult:
        """打开页面，等正文出现并稳定后再取 HTML。

        固定 sleep 在两类页面上都不够用：一类是安全检测/跳转页，2 秒后才有内容；
        另一类是纯 JS 渲染，正文要等接口回来。所以这里改成轮询正文长度，
        拿到"最长的那一版" HTML，而不是盲目等固定时间。
        """
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(self.opts.timeout * 1000))
            status = resp.status if resp else 0

            try:
                page.wait_for_load_state("networkidle", timeout=min(6000, int(self.opts.timeout * 300)))
            except Exception:  # noqa: BLE001 - networkidle 超时很常见，忽略
                pass

            if self.opts.browser_wait:
                page.wait_for_timeout(self.opts.browser_wait)

            best_html, best_len = "", 0
            deadline = time.time() + self.opts.browser_max_wait / 1000.0
            scrolled = False
            stable = 0
            while True:
                html = page.content()
                length = probe(html)
                if length > best_len:
                    best_html, best_len = html, length
                    stable = 0
                else:
                    stable += 1

                if best_len >= self.opts.min_text_length:
                    # 连续两次没有变长，认为内容已经稳定
                    if stable >= 2:
                        break
                else:
                    # 还没拿到正文：滚一下触发懒加载，再给页面一点时间
                    if not scrolled and time.time() > deadline - self.opts.browser_max_wait / 2000.0:
                        try:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
                            page.wait_for_timeout(600)
                            page.evaluate("window.scrollTo(0, 0)")
                        except Exception:  # noqa: BLE001
                            pass
                        scrolled = True

                if time.time() >= deadline:
                    break
                page.wait_for_timeout(700)

            html = best_html or page.content()
            if _looks_blocked(status, html):
                return FetchResult(
                    url=url,
                    status_code=status,
                    blocked=True,
                    error=f"HTTP {status}（浏览器仍被拦截，已停止对该站点的请求）",
                )
            return FetchResult(url=url, html=html, mode="browser", status_code=status)
        finally:
            page.close()

    def _shutdown(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---- 对外接口 ----
    def fetch(self, url: str, probe) -> FetchResult:
        """probe(html) -> int：给一段 HTML 打"正文长度"分，用来判断内容是否已经加载好。"""
        self._ensure_started()
        holder: dict = {"event": threading.Event(), "result": None, "error": ""}
        self._queue.put((url, holder, probe))
        holder["event"].wait()
        if holder["error"]:
            return FetchResult(url=url, error=holder["error"])
        return holder["result"] or FetchResult(url=url, error="浏览器抓取无结果")

    def close(self) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None


class Fetcher:
    """对外统一的抓取入口：静态优先，正文拿不到时回退浏览器。"""

    def __init__(self, opts: Options, registry=None) -> None:
        self.opts = opts
        self.registry = registry
        self._browser: BrowserFetcher | None = None
        self._browser_lock = threading.Lock()
        self.throttle = HostThrottle(opts.per_host_delay, opts.serialize_per_host, registry)
        self.breaker = HostCircuitBreaker()

    def _get_browser(self) -> BrowserFetcher:
        with self._browser_lock:
            if self._browser is None:
                self._browser = BrowserFetcher(self.opts)
            return self._browser

    def policy_for(self, url: str):
        return self.registry.match(url) if self.registry else None

    def fetch(self, url: str, probe) -> FetchResult:
        """probe(html) -> int：评估一段 HTML 里的正文长度。

        关键点：静态抓取"成功"不等于"拿到了正文"。36kr、今日头条这类站点
        静态请求返回的是 200 + 几十 KB 的 JS 空壳或安全检测页，
        所以这里用正文长度而不是 HTTP 状态来判断要不要回退浏览器。

        命中站点策略表且标了 browser_first 的，直接走浏览器：
        这类站点的静态页注定拿不到正文，先请求一次静态页纯属浪费配额，
        对 36kr 这种 IP 级风控的站点尤其不划算。
        """
        # 该域名已经被判定为硬性拦截：直接放弃，不再发任何请求
        if self.breaker.is_open(url):
            return FetchResult(
                url=url,
                error=f"该域名已被限流（{self.breaker.reason(url)}），为避免加重封锁已跳过后续请求",
            )

        policy = self.policy_for(url)
        skip_static = bool(policy and policy.browser_first and self.opts.use_browser)

        static = FetchResult(url=url, error="")
        if not skip_static:
            with self.throttle.guard(url):
                static = fetch_static(url, self.opts)

            if static.ok and static.html and probe(static.html) >= self.opts.min_text_length:
                return static

            # 硬性拦截（403/429 等）：不重试、不换浏览器，直接熔断该域名
            if static.blocked:
                reason = static.error or "站点返回拦截响应"
                self.breaker.trip(url, reason)
                return static

            if not self.opts.use_browser:
                return static

        with self.throttle.guard(url):
            browser = self._get_browser().fetch(url, probe)
        if browser.ok:
            return browser
        if browser.blocked:
            self.breaker.trip(url, browser.error or "浏览器也被拦截")
            return browser
        if static.ok:
            # 浏览器也没改善，那就用静态结果（至少元数据可能还有用）
            return static
        if skip_static:
            return FetchResult(url=url, error=f"浏览器抓取失败：{browser.error}")
        return FetchResult(
            url=url,
            error=f"静态抓取失败：{static.error}；浏览器回退失败：{browser.error}",
        )

    def close(self) -> None:
        with self._browser_lock:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
