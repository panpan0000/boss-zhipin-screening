#!/usr/bin/env python3
"""提取候选人**详情页**内容。

背景：详情页反爬有三层 ——
  1. 接口 /wapi/zpjob/view/geek/info/v2 的 zpData.encryptGeekDetailInfo 是**加密**的
  2. 前端用 WASM (wasm-resume-container) 解密
  3. 解密结果**不写 DOM**，直接画到 <canvas>，所以 innerText 恒为空
DOM 读取和抓接口都拿不到可读文本。

破法：WASM 总得调用 CanvasRenderingContext2D.fillText 才能画字。
钩住 fillText，边滚边收集 (text, x, y)，再按 y 聚成行、按 x 排序还原版面。
canvas 的 CSS transform: translateY(T) 给出滚动偏移 → 绝对 y = T + 局部 y。

坑：每次点开候选人都会**新建一个 c-resume iframe 并堆叠**（不是复用），
所以必须把补丁打到所有 iframe，而读写只针对**最后一个**（即当前候选人）。

入口见 cli.py 的 detail / run 子命令。
"""
import json
import re
import socket
import sys
import time


# 所有 JS 片段共用的定位逻辑：取最后一个 c-resume iframe（=当前候选人）
ACTIVE = r"""
  const d = document.querySelector('iframe').contentDocument;
  const ifs = [...d.querySelectorAll('iframe')].filter(x => (x.src || '').includes('c-resume'));
  const f = ifs.length ? ifs[ifs.length - 1] : null;
  const w = f ? f.contentWindow : null;
  const wrap = f ? (f.closest('.resume-detail-wrap') || [...d.querySelectorAll('.resume-detail-wrap')].pop()) : null;
"""

# 补丁打到**所有** c-resume iframe（新开的那个才会重绘）
PATCH_ALL = """
(() => {
""" + ACTIVE + """
  if (!ifs.length) return 'NO_RESUME_IFRAME';
  let patched = 0, already = 0;
  ifs.forEach(f => {
    let w; try { w = f.contentWindow; } catch (e) { return; }
    if (!w) return;
    if (w.__ftHooked) { already++; return; }
    w.__ftHooked = true;
    w.__cap = [];
    w.__read = 0;
    const P = w.CanvasRenderingContext2D.prototype;
    ['fillText', 'strokeText'].forEach(n => {
      const o = P[n];
      if (typeof o !== 'function') return;
      P[n] = function (t, x, y) {
        try { w.__cap.push({ t: String(t), x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }); } catch (e) {}
        return o.apply(this, arguments);
      };
    });
    patched++;
  });
  return JSON.stringify({ iframeCount: ifs.length, patched: patched, already: already });
})()
"""

# 读取当前候选人 iframe 的新增绘制 + 滚动偏移
READ = """
(() => {
""" + ACTIVE + """
  if (!w) return JSON.stringify({ error: 'NO_IFRAME' });
  const cv = w.document.querySelector('canvas');
  const m = cv ? /translateY\\((-?[\\d.]+)px\\)/.exec(cv.style.transform || '') : null;
  const T = m ? parseFloat(m[1]) : 0;
  const cap = w.__cap || [];
  return JSON.stringify({ T: T, total: cap.length, items: cap.slice(w.__read || 0),
                          canvasH: cv ? cv.height : null });
})()
"""

MARK_READ = """
(() => {
""" + ACTIVE + """
  if (!w) return -1;
  w.__read = (w.__cap || []).length;
  return w.__read;
})()
"""

SCROLL = """
(() => {
""" + ACTIVE + """
  if (!wrap) return JSON.stringify({ error: 'NO_WRAP' });
  wrap.scrollTop += %d;
  return JSON.stringify({ top: wrap.scrollTop, max: wrap.scrollHeight, client: wrap.clientHeight });
})()
"""

RESET_SCROLL = """
(() => {
""" + ACTIVE + """
  if (wrap) wrap.scrollTop = 0;
  return wrap ? wrap.scrollHeight : -1;
})()
"""


def drain(cdp, seconds):
    end = time.time() + seconds
    cdp.ws.sock.settimeout(0.4)
    while time.time() < end:
        try:
            cdp.ws.recv_message()
        except (socket.timeout, TimeoutError):
            continue
        except Exception:
            break


def close_modal(cdp):
    cdp.eval(r"""
    (() => { const d = document.querySelector('iframe').contentDocument;
      const b = d.querySelector('.iboss-close'); if (b) b.click(); return 1; })()
    """)
    drain(cdp, 1.2)


def click_candidate(cdp, idx=None, geekid=None):
    """打开候选人详情弹层。

    优先用 geekid 定位（稳定），退化到 DOM 序号 idx（0 基）。
    绝不点击 '打招呼' 按钮。
    """
    find = ("const card = cards[%d];" % idx) if geekid is None else (
        "const card = cards.find(c => (c.querySelector('.card-inner') || c)"
        ".getAttribute('data-geekid') === %s);" % json.dumps(geekid))
    return cdp.eval(r"""
    (() => {
      const d = document.querySelector('iframe').contentDocument;
      const cards = [...d.querySelectorAll('li.card-item')];
      %s
      if (!card) return 'NOT_FOUND';
      const nm = card.querySelector('.name');
      if (!nm) return 'NO_NAME';
      if (nm.closest('.operate-side')) return 'ABORT_IN_GREET_AREA';
      card.scrollIntoView({ block: 'center' });
      nm.click();
      return 'clicked:' + nm.innerText;
    })()
    """ % find)


def read_resume(cdp, step=450, settle=1.0, max_steps=40, debug=False):
    """滚动整个简历，收集绘制调用，返回 (绝对坐标条目, 总绘制次数)。"""
    st = json.loads(cdp.eval(PATCH_ALL))
    if st == "NO_RESUME_IFRAME":
        return None, 0
    if debug:
        print(f"    patch: {st}", file=sys.stderr)

    h = cdp.eval(RESET_SCROLL)
    drain(cdp, 0.9)
    cdp.eval(MARK_READ)

    abs_items = []
    seen_draw = set()          # 加粗文字会被画两遍(all fill + stroke 同坐标)，按精确坐标去重
    last_top = -1
    for i in range(max_steps):
        r = json.loads(cdp.eval(SCROLL % step))
        if r.get("error"):
            break
        drain(cdp, settle)
        got = json.loads(cdp.eval(READ))
        T = got.get("T", 0.0) or 0.0
        items = got.get("items", [])
        dup = 0
        for it in items:
            k = (round(it["x"], 1), round(it["y"], 1), it["t"])
            if k in seen_draw:
                dup += 1
                continue
            seen_draw.add(k)
            abs_items.append((T + it["y"], it["x"], it["t"]))
        cdp.eval(MARK_READ)
        top = r["top"]
        if debug:
            print(f"    step {i}: top={top} T={T} +{len(items)}(去重{dup}) 累计={len(abs_items)}",
                  file=sys.stderr)
        if top >= r["max"] - r["client"] - 2:
            break
        if top == last_top:
            break
        last_top = top
    return abs_items, len(abs_items)


def reconstruct(items, y_tol=6.0, x_gap=14.0, char_w=7.0):
    """(abs_y, x, text) -> 行文本。同 y 聚成行，行内按 x 排序，大间距插列分隔。"""
    if not items:
        return []
    items = sorted(items, key=lambda t: (t[0], t[1]))
    lines, cur, cur_y = [], [], None
    for y, x, t in items:
        if cur_y is None or abs(y - cur_y) <= y_tol:
            cur.append((x, t))
            cur_y = y if cur_y is None else (cur_y + y) / 2
        else:
            lines.append(cur)
            cur, cur_y = [(x, t)], y
    if cur:
        lines.append(cur)

    out = []
    for ln in lines:
        ln.sort(key=lambda p: p[0])
        parts, prev_end = [], None
        for x, t in ln:
            if prev_end is not None and x - prev_end > x_gap:
                parts.append("  ")
            parts.append(t)
            prev_end = x + len(t) * char_w
        s = "".join(parts).strip()
        if s:
            out.append(s)
    return out


def dedupe_lines(lines):
    seen, out = set(), []
    for l in lines:
        k = re.sub(r"\s+", "", l)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out


def grab_detail(cdp, idx=None, geekid=None, debug=False):
    close_modal(cdp)
    res = click_candidate(cdp, idx=idx, geekid=geekid)
    if not res.startswith("clicked:"):
        return {"idx": idx, "geekid": geekid, "ok": False, "error": res}
    name = res.split(":", 1)[1]
    drain(cdp, 3.5)

    items, n = read_resume(cdp, debug=debug)
    if items is None:
        return {"idx": idx, "geekid": geekid, "name": name, "ok": False,
                "error": "简历 iframe 未出现"}
    if n == 0:
        return {"idx": idx, "geekid": geekid, "name": name, "ok": False,
                "error": "0 次绘制调用 —— hook 未覆盖到渲染 iframe"}
    lines = dedupe_lines(reconstruct(items))
    close_modal(cdp)
    return {"idx": idx, "geekid": geekid, "name": name, "ok": True,
            "draw_calls": n, "lines": lines, "text": "\n".join(lines)}
