# LinkHarvest · 链接 → 文档

把一堆超链接丢进去，自动访问、抽取**标题 / 正文 / 链接 / 发布时间**（外加作者、站点、摘要），
导出成 **Word / Markdown / Excel / JSON**。正文图片默认**不抓**，需要时用 `--images` 打开。

两种用法：**命令行**（`links.txt` 进、文档出）和**本地网页界面**（粘贴链接、看进度、点下载）。

---

## 快速开始

需要 **Python 3.10+**（用到了 `X | None` 这类新语法）。Windows / macOS / Linux 都能跑，
核心代码是纯 Python，没有任何平台绑定。

```bash
# 1. 安装依赖（建议先建虚拟环境）
python -m venv .venv
source .venv/bin/activate        # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt

# 2. 安装浏览器内核（只有需要抓 JS 渲染页面时才用得上，但建议装上）
python -m playwright install chromium

# 3A. 命令行：从文件读链接
python -m linkharvest samples/links.txt -o output

# 3B. 命令行：直接从剪贴板/终端粘贴
python -m linkharvest            # 粘贴链接，Ctrl+D 结束（Windows 用 Ctrl+Z 回车）

# 3C. 本地网页界面
python -m linkharvest.web        # 自动打开 http://127.0.0.1:8765
```

### 各平台说明

| 平台 | 启动方式 | 备注 |
| --- | --- | --- |
| **Windows** | 双击 `启动网页界面.bat` / `抓取链接.bat`，或直接用上面的命令 | `.bat` 只是给命令套了个壳，方便双击 |
| **macOS / Linux** | 用上面的 `python -m linkharvest...` 命令 | `.bat` 文件在这两个系统上无法运行，忽略即可 |

macOS / Linux 用户如果想有双击体验，可以自己写个 shell 脚本：

```bash
# 保存为 start-web.sh，然后 chmod +x start-web.sh
#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m linkharvest.web "$@"
```

链接文件格式很宽松：一行一个、Markdown 列表、甚至整段从聊天窗口复制出来的文字都能识别，
工具会用正则把其中所有 `http(s)://` 链接抓出来并自动去重。

---

## 输出

默认在 `output/<时间戳>/` 下生成：

| 文件 | 内容 |
| --- | --- |
| `xxx.docx` | 封面 + 目录表 + 每条链接一章（标题、链接、时间、作者、站点、摘要、正文） |
| `xxx.md` | 同上，带目录锚点 |
| `xxx.xlsx` | 一行一条记录，含完整正文，冻结表头 + 自动筛选，适合检索和二次加工 |
| `xxx.json` | 结构化数据（含正文 HTML 与纯文本），给程序用 |
| `failed.md` / `failed.csv` | 失败清单：哪条挂了、挂在哪一步、为什么 |

开启 `--images` 后才会额外生成 `images/` 目录并把图片嵌进 docx / md
（webp/avif 会转成 Word 能显示的格式，大图缩到 1400px）。

---

## 命令行参数

```text
python -m linkharvest [源文件] [-o 输出目录] [-n 文件名] [-f 格式] [-c 并发] [选项]

-o, --out         输出目录，默认 output
-n, --name        输出文件名（不含扩展名），默认时间戳
-f, --formats     输出格式，逗号分隔，默认 docx,md,xlsx,json
-c, --concurrency 并发数，默认 6
--host-delay      同一域名两次请求的最小间隔秒数，默认 1.0（设为 0 关闭限速）
--no-host-serialize  不强制同一域名串行
--site-policy     JSON 文件，覆盖/扩充内置反爬站点策略表
--list-sites      打印当前生效的反爬站点策略表后退出
--no-sensitive-serial  不强制反爬站点串行（默认串行，最保守）
--cookies         cookie 文件（cookies.txt 或 session.json），复用已通过验证的会话
--save-session    弹出可见浏览器让你手动过验证/登录，保存会话到指定文件
--login-url       配合 --save-session，指定先打开哪个地址（此时可省略链接文件）
--timeout         单页超时秒数，默认 25
--retries         静态抓取重试次数，默认 2
--no-browser      禁用浏览器回退（更快，但 JS 页面会抓不到）
--images          下载正文图片并嵌入文档（默认不下载）
--show-browser    浏览器回退时显示窗口，方便观察被反爬的页面
-q, --quiet       不打印逐条进度
```

退出码：全部成功为 `0`，有失败条目为 `1`，没找到链接为 `2`。

---

## 它是怎么工作的

1. **抓取（混合模式）**：先用 `httpx` 静态请求，带真实 UA 和重试。
   关键在于**用"抽出来的正文长度"判断要不要回退浏览器，而不是看 HTTP 状态**——
   36kr、今日头条、中青网这类站点静态请求会返回 200 + 几十 KB 的 JS 空壳或安全检测页，
   按状态码判断会误以为成功。正文不够长就回退到 Playwright 无头 Chromium。
   浏览器这边不睡固定时间，而是**轮询正文长度直到不再增长**（最多 `browser_max_wait` 毫秒），
   必要时滚动页面触发懒加载，取"内容最全的那一版" DOM。
   浏览器由一个后台线程串行驱动（Playwright 同步 API 非线程安全），上层 worker 仍可多线程并发。
2. **提取**：用 [trafilatura](https://github.com/adbar/trafilatura) 做正文与元数据抽取——
   它在正文识别和「去导航/去广告」上比 readability 干净，并且自带 `htmldate` 做发布时间识别
   （会综合 meta、JSON-LD、URL 路径、页面可见时间等多种线索）。
   整页提取不理想时，会再按 `#js_content`、`article`、`[class*=article-content]` 等
   常见正文容器逐个重试（微信公众号 4.5MB 的页面就是靠这一步救回来的）。
3. **文本化**：按块级元素遍历生成纯文本，既能处理 `<p>` 包裹的正文，
   也能处理微信公众号那种"一大段裸文本挂在 div 下"的结构。
4. **导出**：`python-docx` 手写一个极简 HTML→DOCX 渲染器（标题/段落/列表/引用/代码块/表格/图片），
   `markdownify` 转 Markdown，`openpyxl` 写 Excel。

### 关于成功率

在 127 条真实中文链接上的实测：**成功 119 条（94%），失败 8 条**。
其余如微信公众号、今日头条、华尔街见闻、中青网、观察者网、财新等都能正常拿到正文。

失败的是 **虎嗅**（滑动验证码）和 **36氪**（频率风控），见下一节。

---

## 反爬：怎么用才不会被封

有些站点（以 36氪 为代表）有**基于 IP 的频率风控**。它的表现很有迷惑性：

- 静态请求返回 `200` + 一个「火山引擎安全检测」页面，看起来不像失败；
- 请求密集到一定程度后，**整个 IP 被封**，此后所有请求返回 `403` 空响应；
- 封了之后**连手动打开浏览器也会被拦**，需要人工过验证才恢复。

所以"降并发"是对的方向，但不够。工具把防护做成了**三道**，并按站点分档处理：

### 1. 站点策略表：反爬站点单独对待

内置一份已知反爬站点表（`--list-sites` 可查看，网页界面上也有）：

```
站点                策略       说明
36kr.com          串行/5s    IP 级频率风控：密集请求会返回 403 空响应，且连人工浏览器也被拦
huxiu.com         串行/5s    返回滑动验证码页，无头浏览器过不了；直接走浏览器并放慢节奏
zhihu.com         串行/4s    安全验证/登录墙，未登录时正文拿不到
toutiao.com       并发/2s    正文靠 JS 渲染，静态页是空壳；浏览器可正常拿到
mp.weixin.qq.com  串行/2s    单页 HTML 可达数 MB，建议串行避免内存压力
...
```

命中这张表的 URL 会被**自动特殊处理**：

| 机制 | 说明 |
| --- | --- |
| 更长的间隔 | 用表里的 `delay`（如 36kr 是 5 秒），而不是全局默认的 1 秒 |
| 强制串行 | 同一站点一次只发一个请求 |
| 前置分批 | 反爬站点排到**任务最后**单独跑，万一触发风控，前面的正常站点已经抓完了 |
| 直连浏览器 | 标了 `browser_first` 的（虎嗅、今日头条等）跳过注定失败的静态请求，省一次配额 |

这张表是**可编辑的**。不确定自动识别是否可靠时，用 JSON 覆盖或扩充：

```json
{
  "example.com":  {"delay": 6, "serial": true,  "note": "我自己踩过坑"},
  "36kr.com":     {"delay": 10, "note": "调得更慢一点"}
}
```

```bash
python -m linkharvest links.txt --site-policy my-policy.json
python -m linkharvest --list-sites                    # 查看当前生效的表
```

### 2. 按域名限速（默认开启）

同一域名两次请求之间至少间隔 `--host-delay` 秒（默认 1.0），且同域请求串行。
不同域名之间仍然并行，所以总速度不会被拖垮：

```bash
python -m linkharvest links.txt --host-delay 2      # 更保守
python -m linkharvest links.txt --host-delay 0.5    # 更快，但更容易触发风控
python -m linkharvest links.txt --host-delay 0      # 关闭限速（只建议抓自己的站/内网）
```

### 3. 熔断：遇到 403 立刻停手

这是最重要的一条。碰到 403/429 这类**重试解决不了**的拦截时，工具会：

- 不再重试、不再换浏览器、不再换 UA；
- 直接放弃该域名的所有后续链接，把它们标记为"该域名已被限流"；
- 已经成功抓到的条目不受影响。

这样一次任务最多对某个域名发 1 次会被拒的请求，不会像反复重试那样把封禁越拖越久。
任务结束时会列出被熔断的域名。

### 4. 分批跑

如果一次要抓几十上百条同站链接，建议拆成几批、中间隔一段时间，
比一次性跑完安全得多。被熔断的域名等风控解除后再补跑那几条即可。

### 5. 导入浏览器 Cookie（对付滑块验证）

有些站点（典型是**虎嗅**，用的是阿里云 WAF）会在**浏览器指纹层面**拦截全新浏览器：
不管有头无头、换不换 UA、等多久，都会卡在「请按住滑块，拖动到最右边」的验证页。
但你自己日常用的浏览器能直接打开——因为里面已经有"通过验证"的 cookie。

这类站点只能靠复用你的会话。工具提供两种方式：

**方式 A：让工具帮你捕获（最省事）**

```bash
# 只捕获会话，不抓取
python -m linkharvest --save-session session.json --login-url https://www.huxiu.com/article/4715388.html

# 也可以直接从链接文件里取第一条
python -m linkharvest links.txt --save-session session.json
```

会弹出一个可见浏览器窗口，你手动把滑块拖过去、确认正文能正常显示，
然后回到终端按回车。会话存进 `session.json`，之后一直复用：

```bash
python -m linkharvest links.txt --cookies session.json
```

网页界面里也有「验证并自动获取 Cookie」按钮：点一下会弹出浏览器，
你过完验证后 cookie 会自动填进输入框，直接开始抓取即可。

> **重要**：抓取用的隐藏浏览器和你的日常浏览器（Edge/Chrome）是两个完全独立的实例，
> **不共享 cookie**。所以你在 Edge 里手动过验证，对抓取没有任何帮助——
> 必须让工具自己拿到那份放行 cookie。这是很多人会踩的坑。

**方式 B：用浏览器扩展导出 cookies.txt**

装任意一款「Get cookies.txt」类扩展，在目标站点上导出，然后：

```bash
python -m linkharvest links.txt --cookies cookies.txt
```

两种格式都支持（Netscape `cookies.txt` 和 Playwright 的 `storage_state.json`），
工具会自动只挑与目标链接域名匹配的 cookie，不会把无关站点的凭证也带上。

> cookie 会过期，失效了就重新捕获一次。
> `session.json` / `cookies.txt` 相当于你的登录凭证，不要提交到代码仓库。

> 反爬没有"一定不被封"的参数。上面这些是**降低触发概率、并且触发后不加重后果**的做法。
> 抓取频率请结合目标站点的 robots.txt 与使用条款自行把握。


---

## 二次开发

```text
linkharvest/
  sites.py     反爬站点策略表（可编辑的已知站点配置）
  cookies.py   cookie 加载/过滤（Netscape txt 与 storage_state json）
  session.py   交互式捕获会话：弹浏览器让用户过验证，保存 cookie
  fetch.py     抓取层：静态 + 浏览器回退 + 编码识别 + 同域限速 + 熔断
  extract.py   提取层：元数据、正文 HTML、正文容器回退、图片下载与压缩
  pipeline.py  并发流水线、链接解析、普通/反爬站点分批调度
  export.py    docx / md / xlsx / json 导出与失败清单
  cli.py       命令行
  web.py       FastAPI 本地界面（前端在 web/index.html，无构建步骤）
```

常见改法：

- **换提取器**：只改 `extract.extract_content()`，输入 HTML、输出 `(元数据 dict, 正文 HTML)` 即可。
- **加输出格式**：在 `export.py` 加一个 `write_xxx()`，再在 `export_all()` 里挂上。
- **加反爬站点**：在 `sites.py` 的 `BUILTIN_SITES` 里加一条 `SitePolicy`，
  或直接用 `--site-policy` 传 JSON，不用改代码。
- **接自己的业务**：直接 `from linkharvest.pipeline import Harvest`，传 `Options`、`SiteRegistry` 和回调，拿 `list[Item]`。

依赖：`httpx`、`trafilatura`、`beautifulsoup4`、`lxml`、`python-docx`、`openpyxl`、`markdownify`、
`Pillow`（图片转换）、`fastapi`/`uvicorn`（网页界面）、`playwright`（浏览器回退）。

## 说明

- 只抓公开可访问的页面，请自行确认目标站点的 robots.txt 与使用条款，控制并发与频率。
- 抓取时会跳过 TLS 证书校验（`verify=False`），便于处理证书配置不规范的站点；如需严格校验可自行打开。
