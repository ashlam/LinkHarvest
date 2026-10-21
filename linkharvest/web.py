"""本地网页界面：粘贴链接 → 抓取 → 下载 docx / md / xlsx / json。

启动：python -m linkharvest.web          （默认 http://127.0.0.1:8765）
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .export import export_all
from .models import Item, Options
from .pipeline import Harvest, parse_links
from .sites import SiteRegistry

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path.cwd() / "output" / "web"

app = FastAPI(title="LinkHarvest")


# --------------------------------------------------------------------------- 任务状态


@dataclass
class Task:
    id: str
    links: list[str]
    opts: Options
    registry: SiteRegistry = field(default_factory=SiteRegistry)
    status: str = "running"  # running / done / error
    done: int = 0
    total: int = 0
    items: list[Item] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    error: str = ""
    tripped: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    out_dir: Path = field(default_factory=Path)

    def snapshot(self, include_items: bool = True) -> dict:
        ok = sum(1 for it in self.items if it.status == "ok")
        bad = sum(1 for it in self.items if it.status == "failed")
        partial = len(self.items) - ok - bad
        data = {
            "id": self.id,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "ok": ok,
            "partial": partial,
            "failed": bad,
            "error": self.error,
            "tripped": self.tripped,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "files": [{"kind": k, "name": Path(v).name, "url": f"/api/tasks/{self.id}/file/{k}"} for k, v in self.files.items()],
        }
        if include_items:
            data["items"] = [
                {
                    "url": it.url,
                    "title": it.title,
                    "date": it.date,
                    "author": it.author,
                    "sitename": it.sitename,
                    "status": it.status,
                    "error": it.error,
                    "chars": len(it.content_text),
                    "images": len(it.images),
                    "mode": it.fetch_mode,
                }
                for it in self.items
            ]
        return data


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()


class HarvestRequest(BaseModel):
    links: str
    concurrency: int = 6
    use_browser: bool = True
    download_images: bool = False
    host_delay: float = 1.0
    formats: list[str] = ["docx", "md", "xlsx", "json"]
    timeout: float = 25.0
    # 直接粘贴 cookies.txt 内容或 storage_state JSON
    cookies: str = ""
    # 自定义站点策略 JSON 内容
    site_policy: str = ""


def _run_task(task: Task) -> None:
    def progress(done: int, total: int, item: Item) -> None:
        task.done = done
        task.items.append(item)

    try:
        harvest = Harvest(task.opts, on_progress=progress, registry=task.registry)
        items = harvest.run(task.links, task.out_dir / "images")
        task.items = items
        produced = export_all(items, task.out_dir, task.out_dir / "images", task.opts, "linkharvest")
        task.files = {k: str(v) for k, v in produced.items() if not k.startswith("failed_")}
        task.files.update({k: str(v) for k, v in produced.items() if k.startswith("failed_")})
        task.tripped = harvest.fetcher_breaker_state()
        task.status = "done"
    except Exception as exc:  # noqa: BLE001
        task.status = "error"
        task.error = f"{type(exc).__name__}: {exc}"
    finally:
        task.finished_at = time.time()


@app.post("/api/harvest")
def start_harvest(req: HarvestRequest) -> JSONResponse:
    links = parse_links(req.links)
    if not links:
        raise HTTPException(status_code=400, detail="没有找到任何 http(s) 链接")

    cookies: list[dict] = []
    if req.cookies.strip():
        # 支持直接粘贴 cookies.txt 内容或 storage_state JSON
        try:
            text = req.cookies.strip()
            if text.startswith("{"):
                import json as _json

                data = _json.loads(text)
                cookies = data.get("cookies", []) if isinstance(data, dict) else data
            else:
                from .cookies import parse_netscape_cookies

                cookies = parse_netscape_cookies(text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"cookie 解析失败：{exc}") from exc
        if not cookies:
            raise HTTPException(status_code=400, detail="cookie 内容里没有解析出任何条目")

    task_id = uuid.uuid4().hex[:12]
    opts = Options(
        concurrency=max(1, min(req.concurrency, 16)),
        per_host_delay=max(0.0, req.host_delay),
        use_browser=req.use_browser,
        download_images=req.download_images,
        include_images=req.download_images,
        timeout=req.timeout,
        cookies=cookies,
        formats=tuple(f for f in req.formats if f in {"docx", "md", "xlsx", "json"}) or ("docx",),
    )
    registry = SiteRegistry()
    if req.site_policy.strip():
        try:
            import json as _json
            import tempfile

            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(req.site_policy)
                tmp_path = fh.name
            registry.merge_file(tmp_path)
            Path(tmp_path).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"站点策略解析失败：{exc}") from exc

    out_dir = OUTPUT_ROOT / f"{time.strftime('%Y%m%d-%H%M%S')}-{task_id}"
    task = Task(
        id=task_id, links=links, opts=opts, registry=registry,
        total=len(links), out_dir=out_dir,
    )
    with TASKS_LOCK:
        TASKS[task_id] = task
    threading.Thread(target=_run_task, args=(task,), daemon=True).start()
    return JSONResponse({"id": task_id, "total": len(links)})


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str, items: bool = True) -> JSONResponse:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(task.snapshot(include_items=items))


@app.get("/api/tasks/{task_id}/file/{kind}")
def task_file(task_id: str, kind: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = task.files.get(kind)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")
    return FileResponse(path, filename=Path(path).name)


@app.get("/api/sites")
def list_sites() -> JSONResponse:
    """返回内置反爬站点策略表，供界面展示。"""
    registry = SiteRegistry()
    return JSONResponse(
        [
            {
                "host": p.host,
                "delay": p.delay,
                "serial": p.serial,
                "browser_first": p.browser_first,
                "note": p.note,
                "label": p.label,
            }
            for p in registry.all()
        ]
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "web" / "index.html").read_text(encoding="utf-8"))


class CaptureRequest(BaseModel):
    url: str
    cookies: str = ""


@app.post("/api/capture-session")
def capture_session_api(req: CaptureRequest) -> JSONResponse:
    """弹出可见浏览器让用户手动过验证，返回抓到的 cookie 文本。

    为什么需要这个接口：抓取用的隐藏浏览器和用户的日常浏览器是两个独立实例，
    用户在 Edge 里过验证对抓取没有帮助。必须让工具自己拿到放行 cookie。
    """
    from .session import capture_session

    cookies: list[dict] = []
    if req.cookies.strip():
        try:
            text = req.cookies.strip()
            if text.startswith("{"):
                import json as _json

                data = _json.loads(text)
                cookies = data.get("cookies", []) if isinstance(data, dict) else data
            else:
                from .cookies import parse_netscape_cookies

                cookies = parse_netscape_cookies(text)
        except Exception:  # noqa: BLE001
            cookies = []

    session_file = OUTPUT_ROOT / "session.json"
    ok = capture_session(req.url, session_file, Options().user_agent, existing_cookies=cookies)
    if not ok:
        raise HTTPException(status_code=400, detail="没有捕获到 cookie（可能取消了操作）")

    import json as _json

    state = _json.loads(session_file.read_text(encoding="utf-8"))
    from .cookies import to_netscape_cookies

    return JSONResponse({
        "cookies": to_netscape_cookies(state.get("cookies", [])),
        "count": len(state.get("cookies", [])),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linkharvest-web", description="启动 LinkHarvest 本地网页界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"LinkHarvest 界面：{url}　（Ctrl+C 退出）")
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
