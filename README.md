# zhipin-screening

把招聘平台「推荐牛人」列表上的候选人抓成结构化文本，交给 LLM 做**简历筛选**。

**它不是爬虫，也不是数据采集器。** 它解决的是一个具体问题：
招聘者面对几百个推荐候选人，一个个点开看简历很慢——
能不能让 AI 先筛一遍，人只看筛出来的?

```bash
python3 zhipin.py run \
  --jd "高级云原生开发工程师（Go），成都 15-25K。3年以上 Go 后端经验，熟悉 K8s 云原生生态。" \
  --city 成都 --must-pos Golang --min-exp 3 --limit 8 \
  --out payload.json
```

输出的 `payload.json` 里带完整简历正文 + 预设好的 instruction，
直接丢给 LLM 就得到每人一份「匹配度评分 / 优势 / 疑点 / 是否建议沟通 / 打招呼话术」。

> **目标站点**：当前适配某招聘平台的「推荐牛人」页（`zhipin.com`，见 `cli.py` 的 `SITE_HOST`）。
> 架构是通用的，换平台只需改 `SITE_HOST` 与两个页面模块的选择器。
>
> **开源前请先读 [合规与伦理](#合规与伦理)。** 这个项目的使用边界比它的代码更需要被理解。

---

## 目录

- [原理](#原理) —— 为什么详情页读不到 DOM，以及怎么破
  - [为什么需要它](#为什么需要它) · [怎么破的](#怎么破的) · [数据流](#数据流) · [分层结构](#分层结构)
- [安装与使用](#安装与使用)
  - [起一个「受控 Chrome」](#1-起一个受控-chrome) · [跑](#2-跑) · [分步用法](#3-分步用法) · [输出契约](#4-输出契约面向脚本agent)
- [作为 agent skill 使用](#作为-agent-skill-使用)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [⚠️ 合规与伦理](#合规与伦理)
- [开发](#开发) · [贡献](#贡献) · [License](#license)

---

## 原理

### 为什么需要它

清单页很好抓（就是个 iframe 里的 DOM 列表）。但**详情页**有三层反爬：

| 层 | 做法 | 直接后果 |
|---|---|---|
| 1 | 接口 `/wapi/zpjob/view/geek/info/v2` 返回的简历字段是**加密**的 | 抓接口只拿到密文 |
| 2 | 前端用 **WASM** (`wasm-resume-container`) 解密 | 解密在 wasm 内存里，读不到 |
| 3 | 解密结果**不写 DOM**，直接绘制到 `<canvas>` | `innerText` 恒为空 |

所以常规手段（读 DOM、抓 XHR）在详情页全部失效。截图 OCR 也行，
但会丢失字段结构，而且慢。

### 怎么破的

WASM 要画出文字，就**必须**调用 `CanvasRenderingContext2D.fillText`。
于是我们不跟加密较劲，直接监听绘制：

```
钩住 fillText/strokeText
        ↓  记下每次调用的 (text, x, y)
滚动简历 → WASM 分片重绘
        ↓
canvas 的 CSS transform: translateY(T) 给出滚动偏移
        ↓  绝对 y = T + 局部 y
按绝对 y 聚类成行（容差 6px）→ 行内按 x 排序 → 还原版面
```

这是一个通用技巧：**对"渲染到 canvas 的加密内容"，监听绘制调用比破解加密划算得多。**
同样的思路可以用在别的 canvas 渲染场景（地图、图表、部分电子书阅读器）。

### 数据流

```
控制端口(CDP) ──> 清单页 iframe ──> 清洗(剔非候选人) ──> 机械预筛
                     │
                     └──> 逐个点开详情 ──> fillText 钩子 ──> 简历正文
                                                              │
                          筛选载荷 (JD + 简历 + instruction) <─┘
                                        │
                                        └──> 你的 LLM
```

### 分层结构

```
src/zhipin_screening/
├── cdp.py          Chrome DevTools Protocol 客户端（纯标准库，可独立复用）
├── list_page.py    清单页：iframe 穿透 + 滚动加载 + 字段解析
├── detail_page.py  详情页：canvas fillText 钩子 + 滚动累积 + 版面还原
├── filters.py      清洗非候选人卡片 + 机械预筛
├── export.py       可选：导出 Excel / Markdown
└── cli.py          统一入口
```

**核心零依赖**，只用 Python 标准库。只有 `export` 子命令需要 `openpyxl`。
没有 Playwright / Selenium —— CDP 客户端（含 WebSocket 分帧）是手写的，
这样部署时不需要几百 MB 的浏览器驱动。

---

## 安装与使用

### 1. 起一个「受控 Chrome」

需要在浏览器里登录你自己的账号。**不能用你日常那个 Chrome** ——
Chrome 136+ 出于安全考虑，对默认用户目录会直接忽略 `--remote-debugging-port`，
端口根本不开。必须指定别的 `--user-data-dir`：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/zhipin-profile \
  --no-first-run --no-default-browser-check
```

> ⚠️ **如果 Chrome 是在受限沙箱里启动的**（比如某些 agent 运行环境），
> 需要额外加 `--no-sandbox --disable-gpu`。否则 Chrome 无法嵌套自己的沙箱，
> 会 `FATAL: GPU process isn't usable. Goodbye.` 直接崩（SIGTRAP）。
> 由你自己在 Terminal 启动则不需要 —— **优先用这种方式**，浏览器沙箱更完整。

然后在那个窗口里**登录你的招聘账号**，停在「推荐牛人」页（`/web/chat/recommend`）。

### 2. 跑

无需安装，直接：

```bash
# 永远先自检，它能告诉你 端口/登录/iframe/卡片渲染 哪一层坏了
python3 zhipin.py doctor --stdout

# 一条命令跑完（推荐）
python3 zhipin.py run --jd "岗位要求……" --limit 8 --city 成都 --out payload.json
```

或 `pip install -e .` 后用 `zhipin-screening` 命令。

### 3. 分步用法

需要中间产物时（比如想先看看清单再决定）：

```bash
python3 zhipin.py list --scroll --max-rounds 10 --out list.json
python3 zhipin.py shortlist --in list.json --min-exp 5 --city 成都 --out pick.json
python3 zhipin.py detail --in pick.json --out details.json
python3 zhipin.py screen --in details.json --out payload.json
```

| 参数 | 说明 |
|---|---|
| `--limit` | 取多少人简历。**详情页每人约 3–8 秒**，别一次设太大 |
| `--max-rounds` | 清单滚动轮数，每轮约 +15 人 |
| `--min-exp` | 经验年限下限，`0` 表示不筛（应届生始终排除） |
| `--city` / `--must-pos` | 城市 / 期望职位关键词，不填不筛 |
| `--delay` | 每个候选人之间的间隔，默认 0.8s |

### 4. 输出契约（面向脚本/agent）

- **JSON 走 stdout，进度走 stderr** —— 可以安全地 `--stdout > x.json`
- 失败时退出码非 0，且 stdout 上**仍是合法 JSON**（含 `error.code` 和 `error.hint`）
- 退出码：`0` 成功 / `1` 端口不通 / `2` 环境未就绪 / `3` 详情全失败 / `4` 参数错 / `5` 意外

```json
{
  "ok": true,
  "task": "screen_resumes",
  "job_description": "……",
  "instruction": "你是招聘助手……必须返回 JSON 数组，字段: no, name, score, …",
  "count": 8,
  "candidates": [{
    "no": 1, "name": "（示例）张三", "age": 30, "experience": "7年", "degree": "本科",
    "expected_salary": "27-30K", "expected_city": "成都", "expected_role": "Golang",
    "tags": ["211院校", "微服务架构"],
    "resume_text": "完整简历正文（按版面还原）",
    "resume_available": true
  }]
}
```

---

## 作为 agent skill 使用

`skill/SKILL.md` 是给 AI agent 的说明书（触发条件、调用方式、安全红线、排障表）。
它**只做接口层，不自带脚本副本** —— 实现就是本仓库的 `zhipin.py`，
这样只有一个代码来源，不会出现"skill 里那份和仓库那份不一致"的漂移。

安装（把 `<REPO>` 换成你的克隆路径）：

```bash
REPO="$(pwd)"                                   # 在仓库根目录执行
mkdir -p ~/.agents/skills/boss-zhipin-screening
sed "s|<REPO>|$REPO|g" skill/SKILL.md > ~/.agents/skills/boss-zhipin-screening/SKILL.md
```

这样 agent 加载 skill 后，会直接用你克隆的仓库执行命令。
**移动或删除仓库后需要重新安装**，因为 skill 里记的是绝对路径。

> 如果你想做成自包含的 skill（不依赖仓库位置），把 `src/` 和 `zhipin.py`
> 一起拷进 skill 目录，并把 SKILL.md 里的 `<REPO>` 换成相对路径 ——
> 但要自行承担两份代码漂移的风险。

---

## 已知限制

- **强依赖页面结构**。目标站点前端一改版，选择器就会失效。
  `doctor` 能快速定位是哪一层坏了。这是所有 DOM 自动化的固有代价。
- **简历正文是按画布断行还原的**，换行跟随画布，不是原始段落。
  对 LLM 理解语义没影响，别当精确排版用。
- **详情弹层会堆叠**：每点开一个人就新建一个 iframe 并保留在 DOM 里。
  连抓几十人后内存上涨。长时间运行建议分批、中间刷新页面。
- **姓名可能是匿名的**（`李先生`、`王**`），这是平台的展示策略，不是解析问题。
- 目前主要在 macOS + Chrome 155 上验证过。CDP 协议本身跨平台，理论可用。

完整的技术细节与所有已踩过的坑（**包括几个会安静地给出错误数据的解析陷阱**）见
[docs/anti-scraping.md](docs/anti-scraping.md)。**改动解析逻辑前请务必读它。**

---

## 合规与伦理

> ⚠️ **这一节请认真读，不是形式主义。** 这个项目的使用边界比它的代码更需要被理解。


**它做什么**：自动化操作**你自己已登录的账号**，把你本来就能看到的信息整理成文本。
**它不做什么**：不绕过登录、不绕过权限、不访问他人账号、不包含任何候选人数据。

但"能技术上做到"不等于"可以随便做"。使用者需自行确保合规：

- **遵守平台协议。** 目标平台的服务条款与 robots 规则对自动化访问有约束。
  本项目采用了主动限速（`--delay`）和不点击"打招呼"的设计，但这不构成合规背书。
- **候选人个人信息受法律保护。** 在中国大陆受《个人信息保护法》《数据安全法》约束。
  辅助自己筛选、内部参考，通常属于合理使用范围；
  **批量留存、对外流转、用于训练、转卖**则会落入告知-同意的要求之下。
- **别把它变成群发工具。** 本项目刻意**不实现**自动打招呼/群发功能，
  并且在点击逻辑里加了守卫，一旦目标落在"打招呼"按钮区域就直接中止。
  给真人发消息是不可撤销的动作，不该被批量自动化。
- **规模化请走官方渠道。** 如果要在正式招聘流程里大规模使用，
  平台官方的 API / 企业版导出功能比逆向页面稳妥得多，也更可持续。

如果这些约束与你的使用场景冲突，那这个项目不适合你。

---

## 常见问题

**Q：为什么非要另起一个 Chrome？不能直接用我平时那个吗？**
不能。Chrome 136+ 出于安全考虑，对**默认用户目录**会直接忽略
`--remote-debugging-port`，端口根本不开。而且让自动化脚本接管你日常浏览器的
全部登录态（所有站点）本身也不安全。用一次性 profile 是刻意的隔离。

**Q：那我不是要重新登录一次？**
是，一次。之后登录态保存在 `--user-data-dir` 指定的 profile 里，重启 Chrome
不用重登（只要别删那个目录）。

**Q：为什么详情页读不到文字？是不是解析写错了？**
不是 bug，是**预期行为**。详情页的简历被加密后用 WASM 解密，然后**直接画到
`<canvas>`**，不写 DOM。所以 `document.body.innerText` 恒为空是正常的。
本工具靠监听 `fillText` 调用还原文本。**别试图"修"它，也别改用截图 OCR** ——
OCR 会丢字段结构且有识别错误。

**Q：能用 Selenium / Playwright 吗？**
能，但没必要。本项目的 CDP 客户端是手写的（含 WebSocket 分帧，约 170 行），
好处是**核心零依赖** —— 不需要拖几百 MB 的浏览器驱动。而且这里要用的
几个能力（`Runtime.evaluate` 注入钩子、`Network` 域）都很底层，
用框架反而绕远。

**Q：抓一个人要多久？**
详情页约 3–8 秒（取决于简历长度，要滚动逐片触发重绘）。所以 `--limit`
别一次设太大，`--delay` 也别设太小。

**Q：为什么姓名是「李先生」「王**」？**
这是平台自己的匿名展示策略，不是解析问题。这类候选人可能未完整填写
实名信息，或平台对企业账号做了脱敏。

**Q：会不会不小心给候选人发消息？**
不会。工具只点「姓名区」打开详情弹层，并且 `click_candidate()` 里有
`closest('.operate-side')` 守卫 —— 一旦目标落在「打招呼」按钮区域内会直接
返回 `ABORT_IN_GREET_AREA` 中止。**本项目刻意不实现自动打招呼/群发。**
也请不要绕过这个守卫。

**Q：能抓其他招聘平台吗？**
架构上可以（`cdp.py` 是通用的），但 `list_page.py` / `detail_page.py` 是
站点相关的。换成别的平台需要重写这两个模块。canvas 还原那套技法
（`detail_page.py` 的钩子 + 坐标还原）在**任何用 canvas 渲染内容的站点**上都适用。

**Q：会不会被平台封号？**
自动化访问本身违反大多数平台的使用协议，**有风险**。本项目做了主动限速、
单次少量、不修改任何数据等缓和措施，但**不承诺安全**。请自行评估，
并优先考虑官方 API / 企业版功能。

**Q：页面改版了就完全不能用了吗？**
选择器会失效，但 `doctor` 能立刻告诉你**是哪一层**坏的（端口/登录/iframe/
卡片），不用盲目排查。选择器清单见
[docs/anti-scraping.md](docs/anti-scraping.md) 第六节。

---

## 开发

```bash
# 语法检查（不落地字节码）
python3 -c "import pathlib;[compile(f.read_text(),str(f),'exec') for f in pathlib.Path('src').rglob('*.py')]"

# CDP 客户端自测：用伪造的 CDP 服务端验证握手与分帧，不需要真浏览器
python3 tests/test_cdp_client.py
```

改完 `cdp.py` 一定要跑自测 —— 它能在不碰真浏览器的情况下确认协议层没坏，
否则出问题时会分不清是客户端写错了还是页面改版了。

### 改动解析逻辑前必读

[docs/anti-scraping.md](docs/anti-scraping.md) 记录了 **7 个不会报错、
只会安静地给出错误数据的坑**（`idx` 与 DOM 序号错位导致抓错人、
加粗文字画两遍导致字符重叠、`27年应届生` 被当成 27 年经验等）。

改完务必做**统计量交叉验证**：平均工作年限、应届生比例、城市分布、
学历分布是否都落在合理区间。第一版实现时"平均经验"算出来是 15.2 年，
修正后是 3.7 年 —— 没有任何报错。

---

## 贡献

欢迎 issue 和 PR。几个约定：

1. **不要提交任何真实候选人数据。** `.gitignore` 已经排除了常见产物名，
   但请自己再确认一次。示例一律用虚构数据。
2. **不要实现自动打招呼 / 群发 / 批量消息类功能。** 这是项目的明确边界，见合规章节。
3. 改解析逻辑请一并跑统计量交叉验证，并在 PR 描述里贴出验证结果。
4. 保持**核心零依赖**：新依赖请放进 `pyproject.toml` 的
   `[project.optional-dependencies]`，不要加到 `dependencies`。
5. 页面改版导致选择器更新时，请同时更新 `docs/anti-scraping.md` 第六节的清单。

## License

MIT，另附使用声明，见 [LICENSE](LICENSE)。
