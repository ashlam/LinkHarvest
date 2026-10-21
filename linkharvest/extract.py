"""提取层：从 HTML 里抽出标题、作者、站点、时间、摘要、正文，并把正文里的图片下载到本地。"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup, NavigableString, Tag
from lxml import html as lxml_html

from .models import Options

IMG_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}

AUTHOR_PREFIX = re.compile(r"^(作者|撰文|文|By|BY|by)\s*[:：]?\s*")


def _clean_author(author: str | None) -> str:
    if not author:
        return ""
    text = AUTHOR_PREFIX.sub("", author.strip())
    return text.strip(" /|、,，")


def _norm_date(date: str | None) -> str:
    if not date:
        return ""
    date = date.strip()
    # htmldate 偶尔会给出带时间的完整 ISO 串
    return date.replace("T", " ").split("+")[0].strip()


def _fallback_title(soup: BeautifulSoup, url: str) -> str:
    og = soup.find("meta", attrs={"property": "og:title"}) or soup.find(
        "meta", attrs={"name": "og:title"}
    )
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return url


# 明显是页面噪声、不可能作为标题的文本
BAD_TITLE_PATTERNS = (
    "相关阅读", "相关推荐", "热门推荐", "延伸阅读", "推荐阅读",
    "404", "403 forbidden", "页面不存在", "页面找不到", "出错啦",
    "安全验证", "滑动验证", "正在进行安全检测", "just a moment",
    "access denied", "not found",
)


def _is_bad_title(text: str) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    if len(t) < 4:
        return True
    return any(p in t for p in BAD_TITLE_PATTERNS)


def _pick_title(meta_title: str, soup: BeautifulSoup, url: str) -> str:
    """在 trafilatura 给的标题和页面 <title>/og:title 之间挑一个可信的。

    有些页面（比如中青网）trafilatura 会把"相关阅读"当成标题，
    这类明显是噪声的候选要丢掉，改用 <title>。
    """
    candidates = [meta_title, _fallback_title(soup, url)]
    for cand in candidates:
        if not _is_bad_title(cand) and not _looks_mojibake(cand):
            return re.sub(r"\s+", " ", cand).strip()
    for cand in candidates:
        if cand and not _looks_mojibake(cand):
            return re.sub(r"\s+", " ", cand).strip()
    return url


def _fallback_sitename(soup: BeautifulSoup, url: str) -> str:
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        return og["content"].strip()
    return urlparse(url).netloc


def _fallback_summary(soup: BeautifulSoup) -> str:
    for attrs in (
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return re.sub(r"\s+", " ", tag["content"]).strip()
    return ""


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(text, "lxml").get_text(" ")).strip()


# 常见站点的正文容器。整页提取失败时，先把容器抠出来再提取一次，
# 对超大页面（微信 4.5MB）和"正文在深层 div 里"的站点都有效。
CONTENT_SELECTORS = (
    "#js_content",              # 微信公众号
    ".rich_media_content",      # 微信公众号
    "#page-content",
    "article",
    "[itemprop=articleBody]",
    "[class*=article-content]",
    "[class*=articleContent]",
    "[class*=article_content]",
    "[class*=post-content]",
    "[class*=entry-content]",
    "[class*=detail-content]",
    "[class*=rich_media]",
    "main",
)

SELECTOR_RE = re.compile(r"^([#.]?)([\w-]+)$")


def _trafilatura_html(html: str, url: str, opts: Options) -> str:
    try:
        return (
            trafilatura.extract(
                html,
                url=url,
                output_format="html",
                include_images=opts.include_images,
                include_links=opts.include_links,
                include_formatting=True,
                include_tables=True,
                include_comments=False,
                favor_recall=True,
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def _candidate_fragments(html: str) -> list[str]:
    """按优先级挑出可能的正文容器片段。"""
    fragments: list[str] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return fragments

    for sel in CONTENT_SELECTORS:
        m = SELECTOR_RE.match(sel)
        if not m:
            continue
        prefix, name = m.group(1), m.group(2)
        if prefix == "#":
            nodes = soup.find_all(id=name)
        elif prefix == ".":
            nodes = soup.find_all(class_=re.compile(rf"(^|\s){re.escape(name)}(\s|$)"))
        else:
            nodes = soup.find_all(name)
        for node in nodes:
            text_len = len(node.get_text(strip=True))
            if text_len >= 300:
                fragments.append(str(node))
        if len(fragments) >= 4:
            break
    return fragments


def extract_body(html: str, url: str, opts: Options) -> str:
    """提取正文 HTML：整页提取 → 不理想时退回正文容器提取。"""
    body = _trafilatura_html(html, url, opts)
    if len(_text_len(body)) >= opts.min_text_length:
        return body

    # 整页提取失败/太短：试试逐个正文容器
    best, best_len = body, len(_text_len(body))
    for frag in _candidate_fragments(html):
        candidate = _trafilatura_html(frag, url, opts)
        length = len(_text_len(candidate))
        if length > best_len:
            best, best_len = candidate, length
            if best_len >= 500:  # 已经足够好，不用再试
                break
    return best


def _text_len(body_html: str) -> str:
    return html_to_text(body_html)


def probe_text_length(html: str) -> int:
    """给抓取层用的轻量探针：只要正文长度，不做图片/链接处理。"""
    try:
        return len(trafilatura.extract(html, output_format="txt", favor_recall=True) or "")
    except Exception:  # noqa: BLE001
        return 0


def extract_content(html: str, url: str, opts: Options) -> tuple[dict, str]:
    """返回 (元数据 dict, 正文 HTML 字符串)。"""
    meta: dict = {}
    try:
        doc = trafilatura.extract_metadata(html, default_url=url)
        if doc is not None:
            meta = doc.as_dict()
    except Exception:  # noqa: BLE001 - 元数据抽取失败不影响正文
        meta = {}

    body_html = extract_body(html, url, opts)

    soup = BeautifulSoup(html, "lxml")
    title = _pick_title((meta.get("title") or "").strip(), soup, url)
    data = {
        "title": title,
        "author": _clean_author(meta.get("author")),
        "sitename": (meta.get("sitename") or "").strip() or _fallback_sitename(soup, url),
        "date": _norm_date(meta.get("date")),
        "summary": (meta.get("description") or "").strip() or _fallback_summary(soup),
    }
    # 兜底：编码没识别对时整页都是替换字符，与其产出乱码不如清空
    if _looks_mojibake(data["summary"]):
        data["summary"] = ""
    if _looks_mojibake(body_html):
        body_html = ""
    return data, body_html


def _looks_mojibake(text: str) -> bool:
    """替换字符（U+FFFD）占比过高即视为解码失败。"""
    if not text:
        return False
    bad = text.count("\ufffd")
    return bad >= 3 and bad / len(text) > 0.02


def html_to_text(body_html: str) -> str:
    """把正文 HTML 转成纯文本。

    不能只挑 <p>/<h*>/<li>：有些站点（比如微信公众号）的正文是一大段
    没有标签包裹的裸文本，直接挂在容器 div 下面，只按标签抓会漏掉绝大部分内容。
    所以这里按文档顺序遍历，遇到块级元素就收集它自己的文本（不含嵌套块），
    再继续下钻子节点；裸文本也照样收集。
    """
    if not body_html:
        return ""
    soup = BeautifulSoup(body_html, "lxml")
    root = soup.body or soup

    block_tags = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre",
        "div", "section", "article", "main", "figure", "figcaption", "td", "th",
        "ul", "ol", "dd", "dt", "header", "footer", "aside", "table", "tr",
    }
    skip_tags = {"script", "style", "noscript", "svg", "iframe", "head"}

    parts: list[str] = []

    def own_text(el: Tag) -> str:
        """只取该元素直接包含的文本（跳过嵌套的块级子元素）。"""
        chunks: list[str] = []
        for child in el.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    chunks.append(t)
            elif isinstance(child, Tag):
                if child.name in skip_tags:
                    continue
                if child.name in block_tags:
                    continue  # 交给下钻处理
                t = child.get_text(" ", strip=True)
                if t:
                    chunks.append(t)
        return re.sub(r"\s+", " ", " ".join(chunks)).strip()

    def walk(node) -> None:
        for child in getattr(node, "children", []):
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    parts.append(re.sub(r"\s+", " ", t))
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in skip_tags:
                continue
            if name in block_tags:
                text = own_text(child)
                if text:
                    parts.append(text)
                walk(child)
            else:
                walk(child)

    walk(root)
    if not parts:
        return _strip_tags(body_html)
    return "\n\n".join(_dedupe_paragraphs(parts))


def _dedupe_paragraphs(parts: list[str]) -> list[str]:
    """去掉连续重复段落，以及"整段等于前一段前缀"这类嵌套产生的重复。"""
    out: list[str] = []
    for text in parts:
        if not text:
            continue
        if out:
            prev = out[-1]
            if text == prev:
                continue
            # 嵌套容器经常把同一段话再吐一次，长度接近就视为重复
            if len(text) > 20 and (text in prev or prev in text):
                if len(text) <= len(prev):
                    continue
                out[-1] = text
                continue
        out.append(text)
    return out


# --------------------------------------------------------------------------- 图片本地化


def _to_docx_safe(blob: bytes, ext: str) -> tuple[bytes, str]:
    """统一整理图片：webp/avif/tiff 转 PNG，照片类大图转 JPEG 并缩到 1400px 内。

    不处理的话，几十张网页 PNG 会让 docx 变成几十 MB。
    """
    try:
        from PIL import Image

        needs_format_fix = ext in {".webp", ".avif", ".tiff", ".tif", ".svg"}
        if not needs_format_fix and len(blob) < 200 * 1024:
            return blob, ext
        with Image.open(io.BytesIO(blob)) as im:
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            if has_alpha and im.mode in ("RGBA", "LA"):
                # 很多 PNG 名义上是 RGBA，实际全不透明，这种情况按普通图处理
                try:
                    lo, _hi = im.getchannel("A").getextrema()
                    has_alpha = lo < 250
                except Exception:  # noqa: BLE001
                    pass
            if max(im.size) > 1400:
                im.thumbnail((1400, 1400), Image.LANCZOS)
            if has_alpha:
                if im.mode != "RGBA":
                    im = im.convert("RGBA")
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                return buf.getvalue() or blob, ".png"
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            if needs_format_fix:
                # 格式本来就不被 Word 支持，转出来一定比原图更可用
                im.save(buf, format="JPEG", quality=85, optimize=True)
                return buf.getvalue() or blob, ".jpg"
            im.save(buf, format="JPEG", quality=85, optimize=True)
            data = buf.getvalue()
            return (data, ".jpg") if data and len(data) < len(blob) else (blob, ext)
    except Exception:  # noqa: BLE001 - 转不了就原样保留
        return blob, ext


class ImageDownloader:
    def __init__(self, opts: Options, image_dir: Path) -> None:
        self.opts = opts
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            timeout=opts.image_timeout,
            follow_redirects=True,
            verify=False,
            headers={
                "User-Agent": opts.user_agent,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": "",
            },
        )

    def close(self) -> None:
        self._client.close()

    def localize(self, body_html: str, page_url: str) -> tuple[str, list[str]]:
        """把正文里的 <img> 下载到本地，src 改写为相对路径。返回 (新 HTML, 本地文件名列表)。"""
        if not body_html or not self.opts.download_images:
            return body_html, []
        root = lxml_html.fromstring(body_html)
        saved: list[str] = []
        for img in root.xpath("//img"):
            raw = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            if not raw or raw.startswith("data:"):
                if raw.startswith("data:"):
                    name = self._save_data_uri(raw)
                    if name:
                        img.set("src", name)
                        saved.append(name)
                continue
            absolute = urljoin(page_url, raw)
            name = self._download(absolute, page_url)
            if name:
                img.set("src", name)
                img.attrib.pop("srcset", None)
                img.attrib.pop("data-src", None)
                saved.append(name)
            else:
                # 下载失败就保留原图地址，至少不会丢图
                img.set("src", absolute)
        return lxml_html.tostring(root, encoding="unicode"), saved

    def _save_data_uri(self, uri: str) -> str:
        m = re.match(r"data:(image/[\w.+-]+);base64,(.*)", uri, re.S)
        if not m:
            return ""
        import base64

        try:
            blob = base64.b64decode(m.group(2))
        except Exception:  # noqa: BLE001
            return ""
        if len(blob) > self.opts.max_image_bytes:
            return ""
        ext = IMG_EXT_BY_TYPE.get(m.group(1).lower(), ".png")
        blob, ext = _to_docx_safe(blob, ext)
        name = f"{hashlib.sha1(blob).hexdigest()[:16]}{ext}"
        (self.image_dir / name).write_bytes(blob)
        return name

    def _download(self, url: str, referer: str) -> str:
        if urlparse(url).scheme not in ("http", "https"):
            return ""
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        for existing in self.image_dir.glob(f"{key}.*"):
            return existing.name
        try:
            resp = self._client.get(url, headers={"Referer": referer})
            if resp.status_code >= 400 or not resp.content:
                return ""
            if len(resp.content) > self.opts.max_image_bytes:
                return ""
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ext = IMG_EXT_BY_TYPE.get(ctype)
            if not ext:
                ext = os.path.splitext(urlparse(url).path)[1].lower()
                if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}:
                    ext = mimetypes.guess_extension(ctype or "") or ".jpg"
            blob, ext = _to_docx_safe(resp.content, ext)
            name = f"{key}{ext}"
            (self.image_dir / name).write_bytes(blob)
            return name
        except Exception:  # noqa: BLE001
            return ""


def find_favicon(html: str, page_url: str) -> str:
    """尽力找出站点 logo/favicon 的绝对地址，用于文档里的站点标识。"""
    try:
        soup = BeautifulSoup(html, "lxml")
        for rel in ("apple-touch-icon", "shortcut icon", "icon"):
            tag = soup.find("link", rel=lambda v: v and rel in v)
            if tag and tag.get("href"):
                return urljoin(page_url, tag["href"])
        for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                return urljoin(page_url, tag["content"])
    except Exception:  # noqa: BLE001
        pass
    return urljoin(page_url, "/favicon.ico")
