# CLAUDE.md

Claude Code 会自动读这个文件。**内容与 [AGENTS.md](AGENTS.md) 完全一致**，
只是入口名不同 —— 这样同一套约定能同时被 Codex / Claude Code / 本仓库的
skill 体系读到，不用维护三份。

请直接看 **[AGENTS.md](AGENTS.md)**。

关键的三件事先放这里：

1. **改动解析逻辑前必读 [docs/anti-scraping.md](docs/anti-scraping.md)** ——
   里面 7 个坑都不会报错，只会安静地给出错误数据。
2. **不要实现自动打招呼 / 群发** —— 这是项目边界，不是待办。
3. **改解析逻辑要做统计量交叉验证** —— 平均经验、应届生比例、城市分布、学历分布。

用法见 [README.md](README.md)。
