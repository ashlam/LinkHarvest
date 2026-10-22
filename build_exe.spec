# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 LinkHarvest 打成免安装的 Windows 程序。

用法（在项目根目录）：
    python -m PyInstaller build_exe.spec --noconfirm

产物：
    dist/LinkHarvest/LinkHarvest.exe    双击即可运行
    dist/LinkHarvest/                   整个文件夹打包发给别人

设计取舍：
  - 用 onedir 而不是 onefile：启动快得多，且杀毒软件误报率低
  - 不打包 Playwright 的 Chromium（325MB）：程序会优先用本机已装的
    Chrome/Edge，用户没装浏览器时才提示安装
  - 排除 tkinter/matplotlib 等用不到的大块依赖，减小体积
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# 网页界面的静态文件必须一起打进去，否则 exe 里读不到
datas = [
    (str(ROOT / "linkharvest" / "web" / "index.html"), "linkharvest/web"),
]

# 关键：这些包的"数据文件"不是 .py，PyInstaller 默认不会收集，
# 缺了它们正文提取会直接返回空（trafilatura 依赖 justext 的停用词表）。
datas += collect_data_files("justext")            # stoplists/*.txt
datas += collect_data_files("trafilatura")        # settings.cfg
datas += collect_data_files("htmldate")
datas += collect_data_files("courlan")
datas += collect_data_files("charset_normalizer")
datas += collect_data_files("dateparser")

hiddenimports = [
    # uvicorn 的 worker 是动态加载的，必须显式声明
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # 编码探测
    "charset_normalizer",
    "charset_normalizer.md",
    # 图片处理
    "PIL._tkinter_finder",
    # trafilatura 的元数据抽取
    "trafilatura.metadata",
    "htmldate",
    "courlan",
    "justext",
    "dateparser",
]

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "pytest",
    "setuptools",
    "pip",
    "PyInstaller",
]

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LinkHarvest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台：小白能看到进度和报错
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LinkHarvest",
)
