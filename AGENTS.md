# AGENTS.md

给在本仓库工作的 AI agent 的说明。**人类请看 [README.md](README.md)。**

> 这份文件是为 agent-agnostic 的工具准备的：Codex 读 `AGENTS.md`，
> Claude Code 读 `CLAUDE.md`，本仓库的 skill 体系读 `skill/SKILL.md`。
> 内容一致，只是入口不同。

---

## 这是什么

把招聘平台「推荐牛人」列表上的候选人变成一份带简历正文的 JSON，
交给 LLM 做简历筛选。**核心零依赖**（纯标准库手写 CDP 客户端）。

**它不是爬虫，也不是数据采集器。** 产物是一份给 LLM 的判断载荷，
不是候选人数据库。

---

## 先读什么（按需）

| 你要做的事 | 读这个 |
|---|---|
| 只是用它筛简历 | [README.md](README.md) 的「安装与使用」 |
| 把能力接进别的 agent | [skill/SKILL.md](skill/SKILL.md) —— 触发条件、输出契约、排障表 |
| **改动解析逻辑** | [docs/anti-scraping.md](docs/anti-scraping.md) —— **必读** |

`docs/anti-scraping.md` 记录了 **7 个不会报错、只会安静地给出错误数据的坑**。
不读就改解析，几乎一定会引入错误数据，而且没有任何报错提示。

---

## 常用命令

```bash
# 环境自检 —— 永远先跑这个，它能定位是哪一层坏了
python3 zhipin.py doctor --stdout

# 主流程：一条命令跑完，产出给 LLM 的筛选载荷
python3 zhipin.py run --jd "岗位要求……" --limit 8 --city 成都 --out payload.json

# 自测（不需要真浏览器，用伪造的 CDP 服务端验证握手与分帧）
python3 tests/test_cdp_client.py

# 语法检查（不落地字节码，避免往 ~/Library/Caches 写）
python3 -c "import pathlib;[compile(f.read_text(),str(f),'exec') for f in pathlib.Path('.').rglob('*.py') if '.git' not in str(f)]"
```

**运行前提**：需要一个开着调试端口的「受控 Chrome」，并在其中登录目标平台。
具体命令和两个必须知道的坑（Chrome 136+ 的默认 profile 限制、
沙箱内启动需要 `--no-sandbox`）见 README 的「起一个受控 Chrome」。

---

## 硬性约定

改这个仓库时请遵守：

1. **不要提交任何真实候选人数据。** `.gitignore` 已排除常见产物名，
   但请自己再确认一次。示例一律用虚构数据（见 `examples/`）。
2. **不要实现自动打招呼 / 群发 / 批量消息类功能。**
   `click_candidate()` 里有 `closest('.operate-side')` 守卫，
   命中打招呼区域会返回 `ABORT_IN_GREET_AREA`。**不要绕过它。**
   这是项目的明确边界，不是待办事项。
3. **保持核心零依赖。** 新依赖放进 `pyproject.toml` 的
   `[project.optional-dependencies]`，不要加到 `dependencies`。
4. **改解析逻辑必须做统计量交叉验证**，并在描述里贴出结果。
   检查项：平均工作年限（不应超过年龄 − 16）、应届生比例、城市分布、学历分布。
   第一版实现时「平均经验」算出来是 15.2 年，修正后是 3.7 年 —— 全程没有任何报错。
5. **页面改版导致选择器更新时**，同步更新 `docs/anti-scraping.md` 第六节的清单。

---

## 架构地图

```
zhipin.py                      不安装也能跑的启动器
src/zhipin_screening/
├── cdp.py                     CDP 客户端（纯标准库）。不感知任何站点，可独立复用
├── list_page.py               列表页：同源 iframe 穿透 + 滚动加载 + 字段解析
├── detail_page.py             详情页：canvas fillText 钩子 + 滚动累积 + 版面还原
├── filters.py                 清洗非候选人卡片 + 机械预筛
├── export.py                  可选：Markdown / Excel 渲染
└── cli.py                     统一入口（doctor/run/list/shortlist/detail/screen/xlsx）
```

**站点相关的字面量集中在 `cli.py` 的 `SITE_HOST` 常量**，
换平台只需改它加上两个页面模块的选择器。

**分层原则**：`cdp.py` 不感知站点；`list_page` / `detail_page` 是站点相关的；
`filters.py` 和 `export.py` 只处理数据结构。

---

## 输出契约（改 CLI 时别破坏）

- **JSON 走 stdout，进度走 stderr** —— 用户会 `--stdout > x.json`
- 失败时退出码非 0，且 stdout 上**仍是合法 JSON**（含 `error.code` + `error.hint`）
- 退出码：`0` 成功 / `1` 端口不通 / `2` 环境未就绪 / `3` 详情全失败 / `4` 参数错 / `5` 意外

这几个约定让本工具能被别的脚本和 agent 组合使用，改动前请想清楚影响。
