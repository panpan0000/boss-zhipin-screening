#!/usr/bin/env python3
"""从「推荐牛人」列表页提取候选人卡片。

要点：推荐列表不在主文档里，而在同源 iframe `/web/frame/recommend/` 内，
所以必须先穿透 iframe 才能读到卡片。列表是懒加载的，需要滚动才会出更多。

入口见 cli.py 的 list / run 子命令。
"""
import json
import sys
import time


# ------------------------------------------------------------------ JS 片段
# 每次都重新定位 iframe（导航后引用会失效）
FRAME_LOCATOR = """
(() => {
  const f = [...document.querySelectorAll('iframe')]
    .find(x => (x.src || '').includes('/frame/recommend'));
  if (!f) return null;
  try { return f.contentDocument ? 'OK' : 'NO_DOC'; } catch (e) { return 'CROSS_ORIGIN'; }
})()
"""

SCROLL = """
(() => {
  const f = [...document.querySelectorAll('iframe')].find(x => (x.src||'').includes('/frame/recommend'));
  const doc = f.contentDocument;
  // 滚动容器依次尝试：常见列表包裹层 -> 整个文档
  const cands = ['.list-wrap', '.card-list-wrap', '.candidate-body', 'body', 'documentElement'];
  let scrolled = null;
  for (const sel of cands) {
    const el = sel.startsWith('document') ? doc.documentElement : doc.querySelector(sel);
    if (!el) continue;
    const before = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    if (el.scrollTop > before || el.scrollHeight > el.clientHeight) { scrolled = sel; break; }
  }
  // 兜底：直接滚窗口
  try { doc.defaultView.scrollTo(0, doc.defaultView.document.body.scrollHeight); } catch (e) {}
  return JSON.stringify({ scrolled, cards: doc.querySelectorAll('li.card-item').length });
})()
"""

EXTRACT = r"""
(() => {
  const f = [...document.querySelectorAll('iframe')].find(x => (x.src||'').includes('/frame/recommend'));
  if (!f || !f.contentDocument) return JSON.stringify({error: 'iframe 不可达'});
  const doc = f.contentDocument;

  const t = (el) => el ? (el.innerText || '').trim() : '';
  const one = (root, sel) => t(root.querySelector(sel));

  const cards = [...doc.querySelectorAll('li.card-item')].map((card, i) => {
    const base = one(card, '.base-info');
    const mAge = base.match(/(\d+)\s*岁/);
    // 注意: "27年应届生"=2027届, "22年毕业"=2022年毕业 —— 都不是工作年限。
    // 必须先剥掉这两种模式，否则会被当成 27 年 / 22 年经验。
    const mGrad = base.match(/(\d{2})\s*年\s*应届生/);
    const mGradY = base.match(/(\d{2})\s*年\s*毕业/);
    const mExp = (mGrad || mGradY) ? null : base.match(/(\d+)\s*年(以上|以内)?/);
    const mDeg = base.match(/(博士|硕士|MBA|本科|大专|中专|高中|初中|其他)/);
    let status = base;
    [mAge && mAge[0], mGrad && mGrad[0], mGradY && mGradY[0],
     mExp && mExp[0], mDeg && mDeg[0]].forEach(x => {
      if (x) status = status.replace(x, '');
    });
    status = status.replace(/\s+/g, ' ').trim();
    if (!status && mGrad) status = '应届生';        // 去掉年龄/学历后就空了
    else if (!status && mGradY) status = '已毕业';

    // .expect-wrap 里可能混入 "期望" / "最近关注" 等标签行，取最后一个非标签行
    const LABELS = ['期望', '最近关注', '沟通过', '看过我', '新牛人', '查看全部'];
    const expLines = one(card, '.expect-wrap').split('\n')
      .map(s => s.trim()).filter(Boolean)
      .filter(s => LABELS.indexOf(s) === -1);
    const expect = expLines.length ? expLines[expLines.length - 1] : '';
    const em = expect.match(/^(\S+?)\s+(.+)$/);   // 城市 + 期望职位

    return {
      idx: i + 1,
      // 稳定标识：清洗/去重后 idx 会变，抓详情必须靠 geekid 定位，否则会抓错人
      geekid: (card.querySelector('.card-inner') || card).getAttribute('data-geekid'),
      salary: one(card, '.salary-wrap'),
      name: one(card, '.name'),
      active: one(card, '.active-text'),
      age: mAge ? +mAge[1] : null,
      exp_years: mGrad ? 0 : (mExp ? +mExp[1] : null),
      exp_text: mGrad ? '应届生'
        : (mGradY ? `${2000 + (+mGradY[1])}年毕业` : (mExp ? mExp[0] : null)),
      is_fresh_grad: !!mGrad,
      grad_year: mGrad ? 2000 + (+mGrad[1]) : (mGradY ? 2000 + (+mGradY[1]) : null),
      degree: mDeg ? mDeg[1] : null,
      job_status: status,
      expect_city: em ? em[1] : expect,
      expect_position: em ? em[2] : '',
      tags: [...card.querySelectorAll('.tags .tag-item')].map(e => t(e)),
      advantage: one(card, '.geek-desc').replace(/^优势\s*/, '').trim(),
      work_exps: [...card.querySelectorAll('.work-exps .timeline-item')]
        .map(e => t(e).replace(/\n/g, ' | ')),
      edu_exps: [...card.querySelectorAll('.edu-exps .timeline-item')]
        .map(e => t(e).replace(/\n/g, ' | ')),
      raw: t(card)
    };
  });

  return JSON.stringify({
    job_title: one(doc, '.job-title') || one(doc, '.job-name') || null,
    card_count: cards.length,
    cards
  });
})()
"""


def ensure_frame(cdp, tries=10):
    for _ in range(tries):
        st = cdp.eval(FRAME_LOCATOR)
        if st == "OK":
            return True
        time.sleep(1)
    return False


def collect(cdp, scroll=True, max_rounds=25, settle=2.5):
    """滚到底，直到卡片数不再增长。"""
    last = -1
    stable = 0
    rounds = 0
    if scroll:
        while rounds < max_rounds and stable < 3:
            rounds += 1
            info = cdp.eval(SCROLL)
            time.sleep(settle)
            data = json.loads(cdp.eval(EXTRACT))
            n = data.get("card_count", 0)
            print(f"    第 {rounds} 轮: {n} 张卡片", file=sys.stderr)
            if n <= last:
                stable += 1
            else:
                stable = 0
            last = n
    return json.loads(cdp.eval(EXTRACT))
