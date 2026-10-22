"""LinkHarvest 分发版入口。

和 `python -m linkharvest` 的区别：
  - 打包成 exe 后双击即可运行，不需要用户装 Python
  - 没有参数时进入交互式菜单，适合不熟悉命令行的用户
  - 所有输出放在 exe 同目录的 output/ 下，方便找到
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


def _fix_console_encoding() -> None:
    """修正 Windows 控制台的中文乱码。

    控制台默认代码页常是 GBK(936)，而 Python 3 按 UTF-8 输出，
    两者不一致时中文就显示成乱码。这里把代码页切到 UTF-8(65001)，
    同时把 stdout/stderr 也统一成 UTF-8。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - 没有真实控制台时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 打包后可能没有 reconfigure
            pass


_fix_console_encoding()


def base_dir() -> Path:
    """exe 所在目录（打包后）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def banner() -> None:
    print("=" * 58)
    print("  LinkHarvest · 链接 → 文档")
    print("  把一批网页链接抓取成 Word / Excel / Markdown")
    print("=" * 58)
    print()


def interactive() -> int:
    """给小白的交互式流程：不用记参数。"""
    banner()
    print("请把要抓取的链接粘贴到下面，一行一个。")
    print("粘贴完成后，另起一行输入 END 再回车（或直接按 Ctrl+Z 回车）。")
    print()

    lines: list[str] = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip().upper() == "END":
            break
        lines.append(line)

    raw = "\n".join(lines)
    if not raw.strip():
        print("\n没有输入任何链接，已退出。")
        return 1

    out = base_dir() / "output"
    print(f"\n开始抓取，结果会保存到：{out}")
    print("（链接多的话需要等一会儿，请勿关闭窗口）\n")

    from linkharvest.cli import main as cli_main

    # 把收集到的链接写进临时文件再交给 CLI：
    # 此时 stdin 已经读到 EOF，直接调 cli_main 会读到空内容。
    import tempfile

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="lh-links-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
        rc = cli_main([tmp_path, "-o", str(out)])
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    print("\n" + "=" * 58)
    if rc in (0, 1):
        print("抓取完成！打开下面的文件夹查看结果：")
        print(f"  {out}")
        try:
            if sys.platform == "win32":
                os.startfile(out)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(out)], check=False)
            else:
                subprocess.run(["xdg-open", str(out)], check=False)
        except Exception:  # noqa: BLE001
            pass
    else:
        print("运行出错了，请把上面的信息截图反馈。")
    return rc


def web_mode() -> int:
    """启动本地网页界面。"""
    from linkharvest.web import main as web_main

    return web_main(["--no-open"]) or 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 有参数就按命令行模式走，保持和 python -m linkharvest 一致
    if argv:
        if argv[0] in ("web", "--web"):
            return web_mode()
        from linkharvest.cli import main as cli_main

        return cli_main(argv)

    # 无参数：进入交互菜单
    banner()
    print("请选择模式：")
    print("  1  粘贴链接，抓取成文档（推荐）")
    print("  2  打开图形界面（浏览器里操作）")
    print("  3  退出")
    print()
    try:
        choice = input("输入 1 / 2 / 3 后回车：").strip()
    except (EOFError, KeyboardInterrupt):
        return 0

    if choice == "2":
        print("\n正在启动图形界面，稍后浏览器会自动打开…")
        print("（关闭这个黑窗口即退出程序）\n")
        return web_mode()
    if choice == "3":
        return 0
    return interactive()


if __name__ == "__main__":
    raise SystemExit(main())
