"""zhipin-screening —— 把招聘平台的「推荐牛人」列表变成给 LLM 的简历筛选载荷。

分层：
    cdp.py          Chrome DevTools Protocol 客户端（纯标准库）
    list_page.py    清单页：同源 iframe 穿透 + 滚动加载 + 字段解析
    detail_page.py  详情页：canvas fillText 钩子 + 版面还原
    filters.py      清洗非候选人卡片 / 机械预筛
    export.py       可选：导出 Excel / Markdown
    cli.py          统一入口

只依赖 Python 标准库；只有 Excel 导出需要 openpyxl。
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
