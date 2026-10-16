# zhipin-screening

把 BOSS直聘「推荐牛人」页面的候选人抓成结构化文本，交给 LLM 做**简历筛选**。

**它不是爬虫，也不是数据采集器。** 它解决的是一个具体问题：
招聘者在 BOSS直聘 上面对几百个推荐候选人，一个个点开看简历很慢——
能不能让 AI 先筛一遍，人只看筛出来的?

```bash
python3 zhipin.py run \
  --jd "高级云原生开发工程师（Go），成都 15-25K。3年以上 Go 后端经验，熟悉 K8s 云原生生态。" \
  --city 成都 --must-pos Golang --min-exp 3 --limit 8 \
  --out payload.json
```

输出的 `payload.json` 里带完整简历正文 + 预设好的 instruction，
直接丢给 LLM 就得到每人一份「匹配度评分 / 优势 / 疑点 / 是否建议沟通 / 打招呼话术」。

---

## 原理

### 为什么需要它

BOSS直聘 的**清单页**很好抓（就是个 iframe 里的 DOM 列表）。但**详情页**有三层反爬：

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

需要在浏览器里登录你自己的 BOSS直聘 账号。**不能用你日常那个 Chrome** ——
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

然后在那个窗口里**登录 BOSS直聘**，停在 `https://www.zhipin.com/web/chat/recommend`。

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

`skill/SKILL.md` 是给 AI agent 的说明书（触发条件、调用方式、红线、排障表）。
把 `skill/` 整个目录放进你的 agent skills 目录即可被发现调用，例如：

```bash
cp -r skill ~/.agents/skills/boss-zhipin-screening
```

---

## 已知限制

- **强依赖页面结构**。BOSS直聘 前端一改版，选择器就会失效。
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

## ⚠️ 合规与伦理

这一节请认真读，不是形式主义。

**它做什么**：自动化操作**你自己已登录的账号**，把你本来就能看到的信息整理成文本。
**它不做什么**：不绕过登录、不绕过权限、不访问他人账号、不包含任何候选人数据。

但"能技术上做到"不等于"可以随便做"。使用者需自行确保合规：

- **遵守平台协议。** BOSS直聘 的用户协议与 robots 规则对自动化访问有约束。
  本项目采用了主动限速（`--delay`）和不点击"打招呼"的设计，但这不构成合规背书。
- **候选人个人信息受法律保护。** 在中国大陆受《个人信息保护法》《数据安全法》约束。
  辅助自己筛选、内部参考，通常属于合理使用范围；
  **批量留存、对外流转、用于训练、转卖**则会落入告知-同意的要求之下。
- **别把它变成群发工具。** 本项目刻意**不实现**自动打招呼/群发功能，
  并且在点击逻辑里加了守卫，一旦目标落在"打招呼"按钮区域就直接中止。
  给真人发消息是不可撤销的动作，不该被批量自动化。
- **规模化请走官方渠道。** 如果要在正式招聘流程里大规模使用，
  BOSS直聘 官方的 API / 企业版导出功能比逆向页面稳妥得多，也更可持续。

如果这些约束与你的使用场景冲突，那这个项目不适合你。

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

## License

MIT，另附使用声明，见 [LICENSE](LICENSE)。
