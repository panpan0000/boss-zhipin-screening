---
name: boss-zhipin-screening
description: >
  从 BOSS直聘（zhipin.com）「推荐牛人」抓取候选人清单并进入详情页取简历正文，
  整理成一份可直接交给 LLM 的**筛选载荷**，实现「让 AI 筛简历」。
  当用户提到 BOSS直聘 / 直聘 / zhipin / 推荐牛人 / 牛人推荐 / 筛简历 / 筛选候选人 /
  批量看候选人 / 帮我看下这些候选人 / 简历筛选 / 招人筛选 / AI 筛简历时使用。
  也适用于用户说「我浏览器里开着 BOSS直聘，你帮我看看谁合适」这类请求。
  注意：本 skill 用于**辅助人做筛选判断**，不是数据采集/存档工具。
---

# 招聘简历 AI 筛选

把「推荐牛人」页面上的候选人变成一份带简历正文的 JSON，交给 LLM 输出
匹配度评分、优势、疑点、是否建议沟通。**判断由 AI 做，决策由人做。**

> 定位很重要：这个 skill **不做数据囤积**。产物是一份给 LLM 的**判断载荷**，
> 不是候选人数据库。别拿它去导出几百人的 Excel 归档。

所有命令都用 `<REPO>/zhipin.py`。（`<REPO>` 指本仓库根目录。）

---

## 先决条件：一个「受控 Chrome」

本工具通过 Chrome DevTools Protocol 操作浏览器，需要一个开着调试端口的
Chrome 实例。

```bash
# 让用户在自己的 Terminal 里执行（Chrome 沙箱完好，最安全）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/zhipin-profile \
  --no-first-run --no-default-browser-check
```

然后**用户必须在那个窗口里登录目标招聘平台**，并停在
`https://www.zhipin.com/web/chat/recommend`。

### 两个坑，不踩会白折腾

1. **不能用日常那个 Chrome 的 profile。** Chrome 136+ 对**默认用户目录**
   会直接忽略 `--remote-debugging-port`，端口根本不开。必须指定别的
   `--user-data-dir`。这也意味着复用不了用户平时的登录态，需要重登一次。
2. **在 agent 沙箱里启动 Chrome 必须加 `--no-sandbox --disable-gpu`。**
   否则 Chrome 无法在 seatbelt 里嵌套自己的沙箱，会
   `FATAL: GPU process isn't usable. Goodbye.` 直接崩（SIGTRAP）。
   由用户自己启动则不需要 —— **优先让用户启动**。

---

## 用法

### 0. 永远先自检

```bash
python3 <REPO>/zhipin.py doctor --stdout
```

逐项返回 端口 / 标签页 / 登录态 / 推荐页 iframe / 卡片渲染 哪一层坏了。
**不要跳过这步直接抓** —— 它能区分「没登录」和「页面结构变了」
这两种完全不同的故障，否则你会浪费很多时间在错误的方向上排查。

### 1. 主用法：一条命令跑完

```bash
python3 <REPO>/zhipin.py run \
  --jd "岗位要求正文……" \
  --limit 8 \
  --city 成都 --must-pos Golang --min-exp 3 \
  --out payload.json
```

依次执行：抓清单 → 机械预筛 → 逐个进详情页取简历正文 → 生成筛选载荷。

然后把 `payload.json` 交给 LLM。里面**已经写好 instruction**，
要求返回 `{no, name, score, strengths[], concerns[], recommend_contact, greeting}`
的 JSON 数组，按 score 降序。

> `--jd` 是必须的。没有岗位要求，AI 只能凭通用判断打分，结果没意义。

### 2. 分步用法（需要中间产物时）

```bash
python3 <REPO>/zhipin.py list --scroll --max-rounds 10 --out list.json
python3 <REPO>/zhipin.py shortlist --in list.json --min-exp 5 --city 成都 --out pick.json
python3 <REPO>/zhipin.py detail --in pick.json --out details.json
python3 <REPO>/zhipin.py screen --in details.json --out payload.json
```

调参要点：

| 参数 | 说明 |
|---|---|
| `--limit` | 取多少人简历。**详情页每人约 3–8 秒**，别一次设太大 |
| `--max-rounds` | 清单滚动轮数，每轮约 +15 人。默认 10 |
| `--min-exp` | 经验年限下限，`0` 表示不筛（应届生始终排除） |
| `--city` / `--must-pos` | 不填则不筛 |
| `--delay` | 每个候选人之间的间隔，默认 0.8s。别设太小 |

---

## 输出契约

所有子命令：**JSON 走 stdout，进度走 stderr**。失败时退出码非 0，
且 stdout 上仍是合法 JSON（含 `error.code` 和 `error.hint`）。

退出码：`0` 成功 / `1` 端口不通 / `2` 环境未就绪 / `3` 详情全失败 / `4` 参数错 / `5` 意外。

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
    "activity": "刚刚活跃", "tags": ["211院校", "微服务架构"],
    "resume_text": "完整简历正文（按版面还原）",
    "resume_available": true
  }]
}
```

拿到载荷后，**由你（agent）来做筛选判断**，不要只是把 JSON 转手丢给用户 ——
用户要的是判断结果，不是数据。

---

## 详情页有反爬，读不到 DOM 是正常的

清单页很好抓；详情页有三层反爬：接口数据**加密** → 前端用 **WASM** 解密 →
结果**不写 DOM**，直接画到 `<canvas>`。所以 `innerText` 恒为空，
抓接口也只能拿到密文。

破法是钩住 `CanvasRenderingContext2D.fillText`，边滚边收集 `(text, x, y)`，
再用 canvas 的 `transform: translateY(T)` 还原绝对坐标（绝对 y = `T + y`），
最后按 y 聚行、按 x 排序还原版面。

**这不是 bug，是预期行为 —— 不要试图去"修"它，也不要改用截图 OCR。**

完整原理、以及另外 7 个会安静地给出错误数据的坑（iframe 堆叠、
加粗文字画两遍、geekid vs idx 错位、`27年应届生` 误当 27 年经验等），
见 `<REPO>/docs/anti-scraping.md`。**改动解析逻辑前务必读它，
并跑统计量交叉验证。**

---

## 安全红线

- **绝不点击「打招呼」**。那会真的给候选人发消息，不可撤销。
  `click_candidate()` 里有 `closest('.operate-side')` 守卫，命中打招呼区域会
  直接返回 `ABORT_IN_GREET_AREA`。**不要绕过这个守卫，也不要帮用户实现
  自动批量打招呼。**
- 抓的是**真实候选人的个人信息**。辅助筛选、内部参考没问题；
  批量留存或对外流转会进入《个人信息保护法》的告知-同意框架。
- 控制频率（`--delay`）和总量，别给平台压力。
- 会话数据落在临时 profile 里，用完即弃；别把 cookie 复制到别处。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `NO_DEBUG_PORT` | Chrome 没带调试端口，或用了默认 profile | 按上面「先决条件」重启 Chrome |
| `NOT_LOGGED_IN` | 受控 Chrome 里没登录 | 让用户在**那个窗口**里扫码登录 |
| `NO_RECOMMEND_FRAME` | 没停在推荐页 | 导航到 `/web/chat/recommend` |
| `cards_rendered.count = 0` | 页面在但列表没加载出来 | 等几秒重试；或前端改版了 |
| Chrome 启动即崩（SIGTRAP） | 沙箱内无法嵌套 Chrome 自己的沙箱 | 加 `--no-sandbox --disable-gpu`，或让用户在 Terminal 启动 |
| 详情 `0 次绘制调用` | 钩子打到了旧的堆叠 iframe | 代码已按「最后一个 iframe」处理；若仍失败说明改版了 |
| 详情解析出字符重叠 | 加粗文字 fill+stroke 各画一遍 | 已按精确坐标去重；若复现检查去重逻辑 |
| 详情返回的姓名与预期不符 | `idx` 与 DOM 序号错位 | 确认走的是 `geekid` 分支 |
| 选择器全部失效 | 前端改版 | 更新 `docs/anti-scraping.md` 第六节列的选择器 |

改完 `cdp.py` 后跑 `python3 <REPO>/tests/test_cdp_client.py` ——
它用伪造的 CDP 服务端验证握手和分帧，能在不碰真浏览器的情况下确认协议层没坏。
