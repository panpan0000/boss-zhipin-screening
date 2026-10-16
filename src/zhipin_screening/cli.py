#!/usr/bin/env python3
"""BOSS直聘「推荐牛人」抓取工具链 —— 统一入口，面向 agent 复用。

设计原则：
  * 每个子命令都能把**纯 JSON 打到 stdout**（--stdout），方便其他 agent 解析
  * 人类可读的进度信息一律走 stderr，不污染 stdout
  * 出错时返回非 0 退出码 + JSON 错误对象，而不是堆栈
  * 只依赖 Python 标准库；导出 Excel 才需要 openpyxl

子命令：
  doctor     环境自检（调试端口 / 登录态 / 推荐页 iframe）—— agent 应先跑这个
  list       抓推荐候选人清单
  shortlist  按条件从清单里筛"合适的人"
  detail     抓指定候选人的简历详情（破解 canvas 渲染）
  screen     汇总成一份喂给 AI 筛选的载荷
  xlsx       导出 Excel

典型 agent 用法：
  python3 zhipin.py doctor --stdout
  python3 zhipin.py list --scroll --max-rounds 10 --stdout > list.json
  python3 zhipin.py shortlist --in list.json --min-exp 3 --city 成都 --stdout > pick.json
  python3 zhipin.py detail --in pick.json --limit 5 --stdout > detail.json
  # 然后把 detail.json 交给 LLM 做筛选
"""
import argparse
import json
import os
import sys
import time


from .cdp import CDP, pick_target, http_json           # noqa: E402

DEFAULT_PORT = 9222
RECOMMEND_URL = "https://www.zhipin.com/web/chat/recommend"


# ------------------------------------------------------------------ 通用工具
class Fail(Exception):
    """可预期的失败：带 code，交给 CLI 转成 JSON 错误。"""
    def __init__(self, code, msg, hint=None):
        super().__init__(msg)
        self.code, self.msg, self.hint = code, msg, hint


def emit(obj, to_stdout, out_path=None):
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(s)
    if to_stdout:
        sys.stdout.write(s + "\n")
    return s


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def connect(port):
    try:
        http_json("/json/version", port, timeout=3)
    except Exception:
        raise Fail("NO_DEBUG_PORT",
                   f"连不上 127.0.0.1:{port} 的 Chrome 调试端口",
                   "需要用带 --remote-debugging-port 的独立实例启动 Chrome，见 README")
    target, _ = pick_target(port, "zhipin.com")
    if not target:
        raise Fail("NO_ZHIPIN_TAB", "没找到 zhipin.com 的标签页",
                   f"在受控 Chrome 里打开 {RECOMMEND_URL} 并登录")
    cdp = CDP(target["webSocketDebuggerUrl"], timeout=60)
    cdp.call("Page.enable")
    return cdp, target


def require_recommend_frame(cdp, tries=6, wait=1.0):
    """确认推荐列表 iframe 在，并已登录。"""
    from .list_page import FRAME_LOCATOR
    for _ in range(tries):
        st = cdp.eval(FRAME_LOCATOR)
        if st == "OK":
            return True
        time.sleep(wait)
    url = cdp.eval("location.href") or ""
    if "/web/user" in url:
        raise Fail("NOT_LOGGED_IN", f"页面停在登录页: {url}",
                   "在受控 Chrome 窗口里扫码登录后重试")
    raise Fail("NO_RECOMMEND_FRAME", f"找不到推荐列表 iframe，当前页面: {url}",
               f"导航到 {RECOMMEND_URL}")


# ------------------------------------------------------------------- doctor
def cmd_doctor(args):
    r = {"ok": False, "port": args.port, "checks": {}}
    # 1. 调试端口
    try:
        v = http_json("/json/version", args.port, timeout=3)
        r["checks"]["debug_port"] = {"ok": True, "browser": v.get("Browser")}
    except Exception as e:
        r["checks"]["debug_port"] = {"ok": False, "error": str(e)}
        r["error"] = {"code": "NO_DEBUG_PORT",
                      "message": f"127.0.0.1:{args.port} 无响应",
                      "hint": "启动带 --remote-debugging-port 的 Chrome，见 README"}
        emit(r, args.stdout, args.out)
        return 1

    # 2. 标签页
    try:
        pages = [t for t in http_json("/json/list", args.port) if t.get("type") == "page"]
    except Exception as e:
        r["error"] = {"code": "LIST_FAILED", "message": str(e)}
        emit(r, args.stdout, args.out)
        return 1
    zp = [t for t in pages if "zhipin.com" in (t.get("url") or "")]
    r["checks"]["zhipin_tab"] = {"ok": bool(zp), "url": zp[0]["url"] if zp else None,
                                 "all_tabs": [t.get("url") for t in pages][:8]}
    if not zp:
        r["error"] = {"code": "NO_ZHIPIN_TAB", "message": "没有 zhipin.com 标签页",
                      "hint": f"在受控 Chrome 里打开 {RECOMMEND_URL}"}
        emit(r, args.stdout, args.out)
        return 1

    # 3. 登录态 + 推荐 iframe
    cdp = CDP(zp[0]["webSocketDebuggerUrl"], timeout=40)
    try:
        url = cdp.eval("location.href") or ""
        logged_in = "/web/user" not in url and "login" not in url.lower()
        r["checks"]["logged_in"] = {"ok": logged_in, "url": url}
        from .list_page import FRAME_LOCATOR
        st = cdp.eval(FRAME_LOCATOR)
        r["checks"]["recommend_frame"] = {"ok": st == "OK", "state": st}
        cards = 0
        if st == "OK":
            cards = cdp.eval("""(()=>{const d=document.querySelector('iframe').contentDocument;
                                 return d.querySelectorAll('li.card-item').length;})()""")
        r["checks"]["cards_rendered"] = {"ok": cards > 0, "count": cards}
    except Exception as e:
        r["checks"]["dom"] = {"ok": False, "error": str(e)}
    finally:
        cdp.close()

    r["ok"] = all(c.get("ok") for c in r["checks"].values())
    if not r["ok"]:
        bad = [k for k, v in r["checks"].items() if not v.get("ok")]
        r["error"] = {"code": "NOT_READY", "message": "未就绪: " + ", ".join(bad),
                      "hint": "按 README 的步骤让受控 Chrome 登录并停在推荐页"}
    emit(r, args.stdout, args.out)
    return 0 if r["ok"] else 2


# --------------------------------------------------------------------- list
def cmd_list(args):
    from .list_page import collect
    cdp, target = connect(args.port)
    try:
        require_recommend_frame(cdp)
        cdp.call("Page.bringToFront")
        data = collect(cdp, scroll=args.scroll, max_rounds=args.max_rounds)
    finally:
        cdp.close()
    out = {"ok": True, "source": target.get("url"),
           "count": data.get("card_count", 0), "cards": data.get("cards", [])}
    emit(out, args.stdout, args.out)
    log(f"[+] 清单: {out['count']} 条 -> {args.out or 'stdout'}")
    return 0


# ---------------------------------------------------------------- shortlist
def cmd_shortlist(args):
    from .filters import shortlist
    if args.infile:
        data = json.load(open(args.infile, encoding="utf-8"))
        cards = data.get("cards", data.get("details", []))
    else:
        cards = json.load(sys.stdin).get("cards", [])
    cards = [c for c in cards if c.get("name", "").strip()]
    picked = shortlist(cards, min_exp=args.min_exp, city=args.city,
                       must_pos=args.must_pos,
                       degrees=args.degree)
    if args.limit:
        picked = picked[:args.limit]
    out = {"ok": True, "criteria": {"min_exp": args.min_exp, "city": args.city,
                                    "must_pos": args.must_pos, "degrees": args.degree},
           "count": len(picked), "cards": picked}
    emit(out, args.stdout, args.out)
    log(f"[+] 命中 {len(picked)} 人")
    return 0


# ------------------------------------------------------------------- detail
def cmd_detail(args):
    from .detail_page import grab_detail
    if args.infile:
        src = json.load(open(args.infile, encoding="utf-8"))
        cards = src.get("cards", src.get("details", []))
    elif args.idx:
        cards = [{"idx": int(i)} for i in args.idx.split(",")]
    else:
        raise Fail("BAD_ARGS", "需要 --in <shortlist.json> 或 --idx 1,2,3")

    if args.limit:
        cards = cards[:args.limit]

    cdp, _ = connect(args.port)
    results = []
    try:
        require_recommend_frame(cdp)
        cdp.call("Page.bringToFront")
        for i, c in enumerate(cards, 1):
            log(f"[{i}/{len(cards)}] #{c.get('idx')} {c.get('name','?')} ...")
            # 有 geekid 就靠它定位（清洗后 idx 与 DOM 序号可能不一致）
            r = grab_detail(cdp,
                            idx=None if c.get("geekid") else int(c["idx"]) - 1,
                            geekid=c.get("geekid"),
                            debug=args.debug)
            r["card"] = {k: c.get(k) for k in
                         ("idx", "geekid", "name", "age", "exp_text", "degree", "salary",
                          "expect_city", "expect_position", "active", "tags")}
            results.append(r)
            log(f"    -> {'ok ' + str(len(r.get('lines', []))) + ' 行' if r.get('ok') else '失败: ' + str(r.get('error'))}")
            if args.delay:
                time.sleep(args.delay)
    finally:
        cdp.close()
    ok = sum(1 for r in results if r.get("ok"))
    emit({"ok": ok > 0, "count": len(results), "ok_count": ok, "details": results},
         args.stdout, args.out)
    log(f"[+] {ok}/{len(results)} -> {args.out or 'stdout'}")
    return 0 if ok else 3


# --------------------------------------------------------------------- run
def cmd_run(args):
    """一条命令跑完：清单 -> 预筛 -> 取简历 -> 生成 AI 筛选载荷。

    这是本工具的主用法。产物是一份**给 LLM 的判断载荷**，不是数据存档。
    """
    from .list_page import collect
    from .filters import shortlist as do_shortlist
    from .detail_page import grab_detail
    from .filters import clean_cards as clean

    jd = args.jd
    if args.jd_file:
        jd = open(args.jd_file, encoding="utf-8").read().strip()
    if not jd:
        raise Fail("NO_JD", "需要 --jd 或 --jd-file",
                   "把岗位要求给进来，AI 才能按岗位判断匹配度")

    cdp, _ = connect(args.port)
    try:
        require_recommend_frame(cdp)
        cdp.call("Page.bringToFront")

        log(f"[1/4] 抓清单 (scroll={args.scroll}, max_rounds={args.max_rounds})")
        data = collect(cdp, scroll=args.scroll, max_rounds=args.max_rounds)
        cards, dropped, dedup = clean(data.get("cards", []))
        log(f"      {len(data.get('cards', []))} -> 剔除非候选人 {dropped} -> 去重 {dedup} -> {len(cards)}")

        log(f"[2/4] 预筛: 经验>={args.min_exp}年 城市={args.city or '不限'} "
            f"期望含「{args.must_pos or '不限'}」 学历={args.degree or '不限'}")
        picked = do_shortlist(cards, min_exp=args.min_exp, city=args.city,
                              must_pos=args.must_pos,
                              degrees=args.degree)
        if args.limit:
            picked = picked[:args.limit]
        log(f"      命中 {len(picked)} 人")
        if not picked:
            raise Fail("NO_MATCH", "预筛没有命中的候选人",
                       "放宽 --min-exp / --city / --must-pos，或确认岗位选对了")

        log(f"[3/4] 逐个进详情页取简历（这一步最慢）")
        details = []
        for i, c in enumerate(picked, 1):
            log(f"      [{i}/{len(picked)}] {c.get('name')} ...")
            r = grab_detail(cdp, idx=None if c.get("geekid") else c["idx"] - 1,
                            geekid=c.get("geekid"), debug=args.debug)
            r["card"] = {k: c.get(k) for k in
                         ("idx", "geekid", "name", "age", "exp_text", "degree", "salary",
                          "expect_city", "expect_position", "active", "tags")}
            details.append(r)
            log(f"         -> {'ok ' + str(len(r.get('lines', []))) + ' 行' if r.get('ok') else '失败 ' + str(r.get('error'))}")
            if args.delay:
                time.sleep(args.delay)
    finally:
        cdp.close()

    log("[4/4] 生成 AI 筛选载荷")
    items = []
    for d in details:
        c = d.get("card") or {}
        items.append({
            "no": c.get("idx"), "name": c.get("name"), "age": c.get("age"),
            "experience": c.get("exp_text"), "degree": c.get("degree"),
            "expected_salary": c.get("salary"), "expected_city": c.get("expect_city"),
            "expected_role": c.get("expect_position"), "activity": c.get("active"),
            "tags": c.get("tags"),
            "resume_text": d.get("text") or "",
            "resume_available": bool(d.get("text")),
        })

    payload = {
        "ok": True,
        "task": "screen_resumes",
        "job_description": jd,
        "instruction": (
            "你是招聘助手。请依据 job_description，对 candidates 中每位候选人做筛选，"
            "逐人输出：1) 匹配度评分 0-100 2) 3 条关键优势 3) 风险或疑点 4) 是否建议发起沟通 "
            "5) 若建议，给一句得体的打招呼话术。"
            "只依据给出的简历正文判断，不要编造简历里没有的信息；"
            "信息不足时明确写“简历未提及”。"
            "必须返回 JSON 数组，字段：no, name, score, strengths[], concerns[], "
            "recommend_contact(bool), greeting。按 score 降序排列。"
        ),
        "prefilter": {"min_exp": args.min_exp, "city": args.city,
                      "must_pos": args.must_pos, "degrees": args.degree},
        "count": len(items),
        "candidates": items,
    }
    emit(payload, args.stdout, args.out)
    got = sum(1 for i in items if i["resume_available"])
    log(f"[+] {len(items)} 人（{got} 人有简历正文）-> {args.out or 'stdout'}")
    if got < len(items):
        log(f"    ⚠ {len(items) - got} 人没取到简历正文，AI 只能凭清单信息判断")
    return 0


# ------------------------------------------------------------------- screen
def cmd_screen(args):
    """把清单+详情整理成给 LLM 的筛选载荷（含明确指令，可直接塞进 prompt）。"""
    src = json.load(open(args.infile, encoding="utf-8"))
    details = src.get("details", src.get("cards", []))
    brief = args.brief or "挖掘候选人与岗位的匹配度，重点看技术栈相关性、项目复杂度和稳定性"
    items = []
    for d in details:
        c = d.get("card") or d
        items.append({
            "no": c.get("idx"),
            "name": c.get("name"),
            "age": c.get("age"),
            "experience": c.get("exp_text"),
            "degree": c.get("degree"),
            "expected_salary": c.get("salary"),
            "expected_city": c.get("expect_city"),
            "expected_role": c.get("expect_position"),
            "activity": c.get("active"),
            "tags": c.get("tags"),
            "resume_text": d.get("text") or "",
            "resume_available": bool(d.get("text")),
        })
    payload = {
        "ok": True,
        "task": "screen_resumes",
        "instruction": (
            "你是招聘助手。下面是 BOSS直聘 候选人的结构化信息与简历正文。"
            "请对每位候选人输出：1) 匹配度评分(0-100) 2) 3 条关键优势 3) 风险/疑点 "
            "4) 是否建议发起沟通(是/否) 5) 若建议，给一句打招呼话术。"
            "必须以 JSON 数组返回，字段: no, name, score, strengths[], concerns[], "
            "recommend_contact(bool), greeting。不要编造简历里没有的信息。"
        ),
        "focus": brief,
        "count": len(items),
        "candidates": items,
    }
    emit(payload, args.stdout, args.out)
    log(f"[+] 已汇总 {len(items)} 人（其中 {sum(1 for i in items if i['resume_available'])} 人有简历正文）")
    return 0


# --------------------------------------------------------------------- xlsx
def cmd_xlsx(args):
    from .filters import clean_cards as clean
    from .export import build_xlsx, build_md
    data = json.load(open(args.infile, encoding="utf-8"))
    cards, dropped, dedup = clean(data.get("cards", []))
    for i, c in enumerate(cards, 1):
        c["idx"] = i
    out_x = args.out or "zhipin_candidates.xlsx"
    build_xlsx(cards, out_x)
    if args.md:
        open(args.md, "w", encoding="utf-8").write(build_md(data, cards))
    log(f"[+] 原始 {len(data.get('cards', []))} -> 去推广 {dropped} -> 去重 {dedup} -> {len(cards)}")
    emit({"ok": True, "count": len(cards), "xlsx": out_x, "md": args.md}, args.stdout)
    return 0


# ---------------------------------------------------------------------- CLI
def build_parser():
    # 公共参数放进 parent，这样子命令前后都能写（agent 拼命令更省心）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Chrome 调试端口（默认 {DEFAULT_PORT}）")
    common.add_argument("--stdout", action="store_true", help="把 JSON 打到 stdout（agent 用）")
    common.add_argument("--out", default=None, help="同时写入文件")

    # 注意：common 只挂在子命令上。argparse 的子命令默认值会覆盖主 parser 解析出的值，
    # 若两边都挂，写在子命令前的 --port 会被悄悄丢弃。所以统一要求写在子命令之后。
    p = argparse.ArgumentParser(prog="zhipin.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检", parents=[common])
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("run", help="★ 一条命令跑完全流程，产出 AI 筛选载荷", parents=[common])
    r.add_argument("--jd", default=None, help="岗位要求正文")
    r.add_argument("--jd-file", default=None, help="从文件读岗位要求")
    r.add_argument("--limit", type=int, default=10, help="最多抓几个人的简历")
    r.add_argument("--scroll", action="store_true", default=True)
    r.add_argument("--no-scroll", dest="scroll", action="store_false")
    r.add_argument("--max-rounds", type=int, default=10)
    r.add_argument("--min-exp", type=int, default=0)
    r.add_argument("--city", default=None)
    r.add_argument("--must-pos", default=None)
    r.add_argument("--degree", default=None)
    r.add_argument("--delay", type=float, default=0.8)
    r.add_argument("--debug", action="store_true")
    r.set_defaults(func=cmd_run)

    l = sub.add_parser("list", help="抓推荐清单", parents=[common])
    l.add_argument("--scroll", action="store_true", help="滚动加载更多")
    l.add_argument("--max-rounds", type=int, default=10)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("shortlist", help="筛选候选人", parents=[common])
    s.add_argument("--in", dest="infile", default=None)
    s.add_argument("--min-exp", type=int, default=0, help="经验年限下限，0 表示不筛（应届生始终排除）")
    s.add_argument("--city", default=None, help="期望城市，不填不筛")
    s.add_argument("--must-pos", default=None, help="期望职位关键词，不填不筛")
    s.add_argument("--degree", default=None, help="学历白名单，逗号分隔")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_shortlist)

    t = sub.add_parser("detail", help="抓简历详情", parents=[common])
    t.add_argument("--in", dest="infile", default=None)
    t.add_argument("--idx", default=None)
    t.add_argument("--limit", type=int, default=None)
    t.add_argument("--delay", type=float, default=0.8)
    t.add_argument("--debug", action="store_true")
    t.set_defaults(func=cmd_detail)

    c = sub.add_parser("screen", help="生成 AI 筛选载荷", parents=[common])
    c.add_argument("--in", dest="infile", required=True)
    c.add_argument("--brief", default=None)
    c.set_defaults(func=cmd_screen)

    x = sub.add_parser("xlsx", help="导出 Excel", parents=[common])
    x.add_argument("--in", dest="infile", required=True)
    x.add_argument("--md", default=None)
    x.set_defaults(func=cmd_xlsx)
    return p


def main():
    args = build_parser().parse_args()
    args.stdout = getattr(args, "stdout", False)
    args.out = getattr(args, "out", None)
    try:
        return args.func(args)
    except Fail as e:
        err = {"ok": False, "error": {"code": e.code, "message": e.msg}}
        if e.hint:
            err["error"]["hint"] = e.hint
        emit(err, args.stdout, args.out)
        log(f"[x] {e.code}: {e.msg}")
        if e.hint:
            log(f"    hint: {e.hint}")
        return 4
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        err = {"ok": False, "error": {"code": "UNEXPECTED", "message": f"{type(e).__name__}: {e}"}}
        emit(err, args.stdout, args.out)
        log(f"[x] 意外错误: {e}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
