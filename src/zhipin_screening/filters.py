"""卡片清洗与候选人筛选。

清洗这一步是**正确性的前提**，不是"顺手整理"：推荐列表里混着非候选人卡片，
照 DOM 序号去点详情会点错人。详见 docs/anti-scraping.md。
"""
from __future__ import annotations


def clean_cards(cards):
    """剔除混在列表里的非候选人卡片，并按 (姓名, 年龄, 薪资) 去重。

    需要剔掉的：
      * VIP 推广横幅 —— 没有姓名、没有画像
      * "热搜牛人推荐" 卡 —— 匿名（王**），没有 data-geekid，属于另一种卡片类型。
        它无法用稳定标识打开详情；一旦混进来，清洗后的序号就和 DOM 序号错位，
        后续按序号点详情会返回**别人**的简历。

    返回 (干净列表, 被剔除数, 被去重数)。
    """
    has_geekid = any(c.get("geekid") for c in cards)
    real = [c for c in cards
            if c.get("name", "").strip()
            and (c.get("advantage") or c.get("age"))
            and (c.get("geekid") or not has_geekid)]
    dropped = len(cards) - len(real)

    seen, uniq = set(), []
    for c in real:
        key = (c["name"], c.get("age"), c.get("salary"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq, dropped, len(real) - len(uniq)


def shortlist(cards, min_exp=0, city=None, must_pos=None, degrees=None,
              active_first=True):
    """按硬条件做**机械预筛**，把明显不合适的先过滤掉，减少要抓详情的人数。

    这里只做能用字段直接判断的过滤；真正的匹配度判断交给 LLM。
    所有条件默认不启用（None / 0），避免对某个具体岗位做隐含假设。

    :param min_exp:     经验年限下限；应届生总会被排除
    :param city:        期望城市，精确匹配
    :param must_pos:    期望职位关键词，子串匹配
    :param degrees:     学历白名单，如 ("本科", "硕士")
    :param active_first: 同等条件下"刚刚活跃"的排前面
    """
    if isinstance(degrees, str):
        degrees = tuple(d.strip() for d in degrees.split(",") if d.strip())

    out = []
    for c in cards:
        if c.get("is_fresh_grad"):
            continue
        if min_exp and (c.get("exp_years") or 0) < min_exp:
            continue
        if city and c.get("expect_city") != city:
            continue
        if must_pos and must_pos not in (c.get("expect_position") or ""):
            continue
        if degrees and c.get("degree") not in degrees:
            continue
        out.append(c)

    if active_first:
        out.sort(key=lambda c: (c.get("active") != "刚刚活跃",
                                -(c.get("exp_years") or 0)))
    else:
        out.sort(key=lambda c: -(c.get("exp_years") or 0))
    return out
