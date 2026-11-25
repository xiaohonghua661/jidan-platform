# -*- coding: utf-8 -*-
"""记单平台 · 每日提醒：今天还有哪些单要做。

  python remind.py              手动提醒：弹大窗（「立即提醒.bat」就是它）
  pythonw remind.py --auto      定时任务用：当天第一次弹大窗，之后有待办就在右下角弹小窗
  python remind.py --text       只在命令行打印今天的待办，不弹窗
  python remind.py --install    按 配置/提醒.json 注册 Windows 定时任务（--uninstall 删除，--status 查看）
只读订单库，不写任何订单数据。"""
import argparse, base64, ctypes, datetime, json, os, re, subprocess, sys, time, webbrowser
from pathlib import Path
from urllib.request import urlopen

import ledger

HERE = ledger.HERE
CFG_FILE = HERE / "配置" / "提醒.json"
STATE_FILE = HERE / ".提醒状态.json"
TASK_NAME = "记单平台每日提醒"
DEFAULT_CFG = {"时间": ["09:00", "14:00", "20:00"], "登录时提醒": True, "小窗停留秒": 20,
               "期限提前天数": 2, "稍后提醒分钟": 30}
WEEK = "一二三四五六日"

# 配色与看板一致
C = {"bg": "#f3f4f6", "card": "#ffffff", "line": "#e4e7ec", "text": "#101828", "text2": "#344054",
     "muted": "#667085", "faint": "#98a2b3", "accent": "#4f46e5", "accent2": "#e0e7ff",
     "red": "#c01048", "amber": "#b45309", "blue": "#1d4ed8", "green": "#15803d", "hover": "#f4f6ff"}
CAT_COLORS = [("#0f766e", "#ccfbf1"), ("#6d28d9", "#ede9fe"), ("#c2410c", "#ffedd5"),
              ("#334155", "#e2e8f0"), ("#be185d", "#fce7f3"), ("#0e7490", "#cffafe")]
FONT = "Microsoft YaHei UI"


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# ---------------- 算出今天要做什么（口径与看板一致） ----------------
def stage_state(v):
    if v is True:
        return "done"
    if v is False:
        return "skip"
    if v is None or v == "":
        return "todo"
    return "pend" if re.search(r"待|未|等|暂", str(v)) else "txt"


def parse_day(s):
    try:
        return datetime.datetime.strptime(str(s), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def analyze(r, cats, order, today):
    env = r.get("环节") or {}
    stages = (cats.get(r.get("类别")) or {}).get("环节") or list(env)
    done_any, work_done, has_work, nxt = False, True, False, None
    for s in stages:
        st = stage_state(env.get(s))
        if st in ("done", "txt"):
            done_any = True
        if s != "结款":
            has_work = True
            if st in ("todo", "pend"):
                work_done = False
                if nxt is None:
                    nxt = s + ("（待定）" if st == "pend" else "")
    paid = env.get("结款") in (True, False)   # 结款标 X = 这单不单独收钱，按已结清算
    status = 3 if paid else 2 if (has_work and work_done) else 1 if done_any else 0
    if r.get("价格") is not None:
        total = r["价格"]
        got = total if paid else 0
    else:
        total = (r.get("定金") or 0) + (r.get("尾款") or 0)
        got = total if paid else (r.get("定金") or 0)   # 定金按接单时已收算
    due = parse_day(r.get("期限"))
    cat = r.get("类别")
    return {"r": r, "status": status, "next": nxt, "owe": max(total - got, 0),
            "due": due, "due_days": (due - today).days if due else None,
            "boss": r.get("老板") or "", "cat": (cats.get(cat) or {}).get("名称", cat),
            "cat_idx": order.index(cat) if cat in order else 0}


def settle_times():
    """各单最近一次结款打钩的时间：ledger.py 写入时记的结款记录.jsonl，加看板手动打钩的手动改动.jsonl。"""
    t = {}

    def put(e):
        k, v = (e.get("类别"), e.get("单号")), e.get("时间") or ""
        if v > t.get(k, ""):
            t[k] = v
    try:
        for line in ledger.SETTLE_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                put(json.loads(line))
    except (OSError, ValueError):
        pass
    for e in ledger.read_manual():
        if e.get("环节") == "结款" and e.get("新值") is True and e.get("状态") != "用户说不是本人":
            put(e)
    return t


def last_settled(xs):
    """老板 → (上次结款那天, 是不是估的)。没有打钩时间的单拿最后更新时间估，只会比实际晚。"""
    st, last = settle_times(), {}
    for x in xs:
        r = x["r"]
        if (r.get("环节") or {}).get("结款") is not True:
            continue
        ts = st.get((r.get("类别"), r["单号"]))
        approx = ts is None
        try:
            day = datetime.date.fromisoformat((ts or r.get("更新") or "")[:10])
        except ValueError:
            continue
        if x["boss"] not in last or day > last[x["boss"]][0]:
            last[x["boss"]] = (day, approx)
    return last


def since_text(v, today):
    if not v:
        return "没结过款"
    n = (today - v[0]).days
    if v[1] and n >= 1:   # 估的那天只会比实际晚，所以写「以上」
        return f"上次结款 {n} 天以上"
    return "上次结款 " + ("今天" if n <= 0 else "昨天" if n == 1 else f"{n} 天前")


def build(cfg=None, today=None):
    cfg = cfg or load_cfg()
    today = today or datetime.date.today()
    cats, _ = ledger.load_cfg()
    order = list(cats)
    xs = [analyze(r, cats, order, today) for r in ledger.all_rows()]
    lead = int(cfg.get("期限提前天数", 2))

    def kaiti(x):
        # 用户 2026-10-15：在开题报告的就是还没开始，开题过了才能做实物，不算待办
        return x["status"] == 0 and "开题" in (x["r"].get("备注") or "")

    def hot(x):
        return x["status"] < 2 and not kaiti(x) and (
            x["r"].get("急单") or (x["due_days"] is not None and x["due_days"] <= lead))

    def key(x):
        return (x["cat_idx"], -(int(x["r"].get("日期") or 0)), x["r"]["单号"])

    urgent = sorted((x for x in xs if hot(x)),
                    key=lambda x: (x["due_days"] if x["due_days"] is not None else 99, key(x)))
    doing = sorted((x for x in xs if x["status"] == 1 and not hot(x)), key=key)
    todo = sorted((x for x in xs if x["status"] == 0 and not hot(x) and not kaiti(x)), key=key)
    waiting = sorted((x for x in xs if kaiti(x)), key=key)
    noprice = sorted((x for x in xs if not ledger.has_price(x["r"])), key=key)   # 缺价格要先问用户
    bosses = {}
    for x in xs:
        # 自己接的私单不进催款（用户 2026-11-25：「自己的不用管啊，自己的已经结款了都」）；
        # 老板空着的（如直客直客单）照旧要催，不算自己
        if x["status"] == 2 and x["boss"] != "自己":
            b = bosses.setdefault(x["boss"], {"name": x["boss"], "n": 0, "owe": 0, "nos": []})
            b["n"] += 1
            b["owe"] += x["owe"]
            b["nos"].append(x["r"]["单号"])
    last = last_settled(xs)
    for b in bosses.values():
        b["since"] = since_text(last.get(b["name"]), today)
    collect = sorted(bosses.values(), key=lambda b: (-b["owe"], b["name"] == ""))
    return {"today": today, "urgent": urgent, "doing": doing, "todo": todo, "waiting": waiting, "noprice": noprice, "collect": collect,
            "collect_n": sum(b["n"] for b in collect), "collect_owe": sum(b["owe"] for b in collect),
            "work_n": len(urgent) + len(doing) + len(todo), "promo": build_promo(today)}


# ---------------- 推广：每天开始推广（用户 2026-10-21） ----------------
def span(c):
    """一笔推广费管的日子：从日期起算 N 天；天数 0（用完为止）和 1 都只算当天。"""
    b = parse_day(c.get("日期"))
    return b, b + datetime.timedelta(days=max(int(c.get("天数", 1)), 1) - 1)


def build_promo(today):
    """「每天推广」的店铺今天记没记推广费、多天的投放到哪天、本月花了多少、回本没有（只读 成本库和订单库）。"""
    cfg = ledger.load_cost_cfg()
    costs = [c for c in ledger.all_costs() if parse_day(c.get("日期"))]
    shops = []
    for name, sc in cfg["店铺"].items():
        mine = [(c, *span(c)) for c in costs if c.get("店铺") == name]
        ends = [e for c, b, e in mine if c.get("天数", 1) > 1]
        last = max(mine, key=lambda t: (t[0]["日期"], t[0]["id"]))[0] if mine else None
        on = [c for c, b, e in mine if b <= today <= e]
        shops.append({"name": name, "daily": bool(sc.get("每天推广")), "on": on,
                      "end": max(ends) if ends else None, "last": last,
                      # 上一笔是「用完为止」：不知道花完没有，提醒看一眼，花完了再开
                      "open": bool(last and last.get("天数", 1) == 0 and not on),
                      # 上一笔就是投好几天的那笔（之后没再续），到期才提醒续不续
                      "run": bool(last and last.get("天数", 1) > 1)})
    month = today.strftime("%Y%m")
    plats = {}
    for c in costs:
        if c["日期"][:6] == month:
            p = c.get("平台") or c["店铺"]
            plats[p] = plats.get(p, 0) + c["金额"]
    rs = ledger.roi(month)
    # 不用每天推广的店铺：投好几天的列投放中、昨天刚到期的（到期提醒续不续）；用完为止的列出来看一眼
    yest = today - datetime.timedelta(days=1)
    runs = [s for s in shops if not s["daily"] and (s["open"] or (s["run"] and s["end"] >= yest))]
    daily = [s for s in shops if s["daily"]]
    return {"shops": shops, "daily": daily, "runs": runs,
            "todo": [s for s in daily if not s["on"] and not s["open"]],
            "check": [s for s in daily if s["open"]],
            "due": [s for s in runs if s["run"] and s["end"] <= today],
            "total": sum(plats.values()), "plats": sorted(plats.items(), key=lambda kv: -kv[1]),
            "got": sum(p["收"] for p in rs.values()), "got_n": sum(p["单"] for p in rs.values())}


def cost_short(c):
    return f"{c.get('方式') or ''} ¥{c['金额']:g}".strip()


def promo_state(s, today):
    """一个店铺今天的推广情况 → (文字, 颜色)。"""
    md = lambda d: f"{d.month}-{d.day:02d}"
    last = s["last"]
    if s["open"]:
        return f"上次 {md(parse_day(last['日期']))} {cost_short(last)}，用完为止：看看花完没有，花完了再开", C["amber"]
    if s["daily"]:
        if s["on"]:
            return "已推广：" + "，".join(cost_short(c) for c in s["on"]), C["green"]
        return "还没推广" + (f"（上次 {md(parse_day(last['日期']))} {cost_short(last)}）" if last else ""), C["red"]
    d = (s["end"] - today).days
    if d < 0:
        return f"投放 {md(s['end'])} 到期了，续不续？", C["red"]
    if d == 0:
        return f"投放今天到期（{md(s['end'])}），续不续？", C["red"]
    if d == 1:
        return f"投放明天到期（{md(s['end'])}）", C["amber"]
    return f"投放中，到 {md(s['end'])}（还剩 {d} 天）", C["green"]


def promo_total(p):
    return f"本月推广 {yuan(p['total'])}" + ("：" + " · ".join(f"{k} {yuan(v)}" for k, v in p["plats"]) if p["plats"] else "")


def promo_head(p):
    if p["todo"]:
        return f"还有 {len(p['todo'])} 个店没推广"
    if p["due"]:
        return "有投放到期，续不续？"
    if p["check"]:
        return f"{len(p['check'])} 个店看看推广费花完没有"
    return "都推广了"


def promo_roi(p):
    """本月回本：推广来的私单（订单标了来源）收入减推广花费。"""
    net = p["got"] - p["total"]
    return (f"推广来的私单 {p['got_n']} 单 {yuan(p['got'])}，"
            + (f"已回本，净赚 {yuan(net)}" if net >= 0 else f"还差 {yuan(-net)} 回本"))


def yuan(v):
    return "¥" + f"{v:,.0f}"


def day_label(d):
    return f"{d.month}月{d.day}日 周{WEEK[d.weekday()]}"


def due_text(x):
    """期限提示：逾期 / 今天 / 明天 / 还剩几天。"""
    d = x["due_days"]
    if d is None:
        return None
    md = f"{x['due'].month}-{x['due'].day:02d}"
    if d < 0:
        return f"已逾期 {-d} 天（{md}）"
    if d == 0:
        return f"今天到期（{md}）"
    if d == 1:
        return f"明天到期（{md}）"
    return f"{md} 到期，还剩 {d} 天"


def right_text(x):
    t = due_text(x)
    if t:
        return t, C["red"] if x["due_days"] <= 1 else C["amber"]
    if x["status"] == 0:
        return ("开题中", C["faint"]) if "开题" in (x["r"].get("备注") or "") else ("还没开始", C["faint"])
    return ("下一步 " + x["next"], C["blue"]) if x["next"] else ("", C["muted"])


def title_of(x):
    r = x["r"]
    return r.get("题目") or r["单号"]


def no_of(x):
    r = x["r"]
    return "无单号" if r.get("题目") and r["单号"] == r["题目"] else r["单号"]


def summary(d):
    parts = [f"还没推广 {'、'.join(s['name'] for s in d['promo']['todo'])}"] if d["promo"]["todo"] else []
    if d["noprice"]:
        parts.append(f"缺价格 {len(d['noprice'])} 单")
    parts.append(f"要做 {d['work_n']} 单")
    if d["urgent"]:
        parts.append(f"急 {len(d['urgent'])}")
    if d["collect_n"]:
        parts.append(f"待结款 {d['collect_n']} 单 {yuan(d['collect_owe'])}")
    if d["waiting"]:
        parts.append(f"开题中 {len(d['waiting'])} 单")
    return " · ".join(parts)


def as_text(d):
    out = [f"记单提醒 · {day_label(d['today'])}", summary(d), ""]
    p = d["promo"]
    if p["daily"] or p["runs"] or p["total"]:
        out.append("【今天开始推广】" + promo_head(p))
        for s in p["daily"] + p["runs"]:
            out.append(f"  {s['name']:<6} {promo_state(s, d['today'])[0]}")
        out.append("  " + promo_total(p))
        out.append("  " + promo_roi(p))
        out.append("")
    for name, xs in (("没填价格（先问清楚多少钱）", d["noprice"]), ("急单 / 快到期", d["urgent"]), ("进行中", d["doing"]), ("未开始", d["todo"]),
                     ("等开题报告（开题过了才能做）", d["waiting"])):
        if xs:
            out.append(f"【{name}】{len(xs)} 单")
            for x in xs:
                rt, _ = right_text(x)
                note = f"　{x['r']['备注']}" if x["r"].get("备注") not in (None, "", "开题中") else ""
                if x["r"].get("急单") and "急单" not in note:
                    note = "　急单" + note
                out.append(f"  {no_of(x):<10} {x['cat']}  {title_of(x)[:24]}  {rt}{note}")
            out.append("")
    if d["collect"]:
        out.append(f"【待结款，可以催了】{d['collect_n']} 单 {yuan(d['collect_owe'])}")
        for b in d["collect"]:
            out.append(f"  {b['name'] or '没填老板'}  {b['n']} 单  {yuan(b['owe'])}  {b['since']}  {'、'.join(b['nos'])}")
    return "\n".join(out).rstrip()


# ---------------- 窗口 ----------------
def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def work_area():
    """屏幕可用区（去掉任务栏）。"""
    try:
        from ctypes import wintypes
        rc = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
        return rc.left, rc.top, rc.right, rc.bottom
    except Exception:
        return None


def open_dashboard():
    """看板在跑就直接开网页；没在跑就用 bat 起服务（会开一个黑窗口，关掉即停止）。"""
    for port in range(8765, 8775):
        try:
            urlopen(f"http://127.0.0.1:{port}/api/orders", timeout=0.5).read()
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        except Exception:
            continue
    os.startfile(str(HERE / "启动记单平台.bat"))


def ellipsis(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def make_button(tk, parent, text, cmd, S, primary=False):
    bg, fg, hov = (C["accent"], "white", "#4338ca") if primary else (C["card"], C["text2"], C["hover"])
    b = tk.Label(parent, text=text, font=(FONT, 10, "bold" if primary else "normal"), bg=bg, fg=fg,
                 padx=int(14 * S), pady=int(6 * S), cursor="hand2",
                 highlightthickness=0 if primary else 1, highlightbackground=C["line"])
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>", lambda e: b.configure(bg=hov))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    return b


def snapshot(win, path):
    """自测用：把窗口内容画进图片（窗口放在屏幕外，不打扰用户）。"""
    from ctypes import wintypes
    from PIL import Image
    win.update_idletasks()
    win.update()
    hwnd = int(win.wm_frame(), 16) or win.winfo_id()
    rc = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp)
    u.PrintWindow(hwnd, mdc, 2)

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]
    bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(path)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, hdc)


def popup(d, cfg, snap=None):
    """大窗：今天的完整待办。返回 'snooze' 表示用户点了稍后提醒。"""
    import tkinter as tk
    result = {"act": None}
    root = tk.Tk()
    S = root.winfo_fpixels("1i") / 96
    root.title(f"记单提醒 · {day_label(d['today'])}")
    root.configure(bg=C["bg"])
    W, H = int(660 * S), int(640 * S)
    if snap:   # 自测截图：全透明、不进任务栏，用户看不到
        root.attributes("-alpha", 0.0)
        root.attributes("-toolwindow", True)
        root.geometry(f"{W}x{H}+{root.winfo_screenwidth() - W}+0")
    else:
        root.geometry(f"{W}x{H}+{(root.winfo_screenwidth() - W) // 2}+{(root.winfo_screenheight() - H) // 3}")
        root.attributes("-topmost", True)
    root.minsize(int(520 * S), int(420 * S))

    # 顶部：日期 + 一句话总结
    hd = tk.Frame(root, bg=C["accent"])
    hd.pack(fill="x")
    tk.Label(hd, text="今天要做的单", font=(FONT, 16, "bold"), fg="white", bg=C["accent"]).pack(
        anchor="w", padx=int(22 * S), pady=(int(16 * S), 0))
    tk.Label(hd, text=f"{day_label(d['today'])}　·　{summary(d)}", font=(FONT, 10), fg=C["accent2"],
             bg=C["accent"]).pack(anchor="w", padx=int(22 * S), pady=(int(2 * S), int(14 * S)))

    # 底部按钮
    ft = tk.Frame(root, bg=C["card"])
    ft.pack(fill="x", side="bottom")
    tk.Frame(ft, bg=C["line"], height=1).pack(fill="x", side="top")
    times = [t for t in cfg.get("时间", []) if t > datetime.datetime.now().strftime("%H:%M")]
    tip = f"下次提醒 {times[0]}" if times else "今天的定时提醒都过了"
    tk.Label(ft, text=tip + "　·　双击「立即提醒」随时再看", font=(FONT, 9), fg=C["faint"], bg=C["card"]).pack(
        side="left", padx=int(18 * S), pady=int(12 * S))

    def act(a):
        result["act"] = a
        root.destroy()
    make_button(tk, ft, "知道了", lambda: act("ok"), S, primary=True).pack(side="right", padx=(int(6 * S), int(18 * S)))
    make_button(tk, ft, "打开看板", lambda: (open_dashboard(), act("ok")), S).pack(side="right", padx=int(6 * S))
    make_button(tk, ft, f"{cfg.get('稍后提醒分钟', 30)} 分钟后再提醒", lambda: act("snooze"), S).pack(side="right", padx=int(6 * S))

    # 中间：可滚动的分组列表
    outer = tk.Frame(root, bg=C["bg"])
    outer.pack(fill="both", expand=True)
    cv = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
    sb = tk.Scrollbar(outer, orient="vertical", command=cv.yview)
    body = tk.Frame(cv, bg=C["bg"])
    win = cv.create_window((0, 0), window=body, anchor="nw")
    body.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
    cv.configure(yscrollcommand=sb.set)
    cv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    root.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))

    def section(title, color, count, rows, limit=12):
        card = tk.Frame(body, bg=C["card"], highlightbackground=C["line"], highlightthickness=1)
        card.pack(fill="x", padx=int(16 * S), pady=(int(12 * S), 0))
        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", padx=int(14 * S), pady=(int(10 * S), int(4 * S)))
        dot = tk.Canvas(head, width=int(10 * S), height=int(10 * S), bg=C["card"], highlightthickness=0)
        dot.create_oval(1, 1, int(9 * S), int(9 * S), fill=color, outline=color)
        dot.pack(side="left")
        tk.Label(head, text=title, font=(FONT, 11, "bold"), fg=C["text"], bg=C["card"]).pack(side="left", padx=(int(8 * S), 0))
        tk.Label(head, text=count, font=(FONT, 10), fg=C["muted"], bg=C["card"]).pack(side="left", padx=(int(8 * S), 0))
        for row in rows[:limit]:
            row(card)
        if len(rows) > limit:
            tk.Label(card, text=f"还有 {len(rows) - limit} 单，打开看板查看", font=(FONT, 9), fg=C["faint"],
                     bg=C["card"]).pack(anchor="w", padx=int(14 * S), pady=(0, int(8 * S)))
        tk.Frame(card, bg=C["card"], height=int(6 * S)).pack()

    def order_row(x):
        def draw(card):
            f = tk.Frame(card, bg=C["card"])
            f.pack(fill="x", padx=int(14 * S), pady=int(3 * S))
            f.columnconfigure(2, weight=1)
            tk.Label(f, text=no_of(x), font=("Consolas", 10, "bold"), fg=C["text"], bg=C["card"], width=10,
                     anchor="w").grid(row=0, column=0, sticky="w")
            fg, bg = CAT_COLORS[x["cat_idx"] % len(CAT_COLORS)]
            tk.Label(f, text=x["cat"], font=(FONT, 8, "bold"), fg=fg, bg=bg, padx=int(6 * S)).grid(
                row=0, column=1, padx=(int(4 * S), int(8 * S)))
            tk.Label(f, text=ellipsis(title_of(x), 22), font=(FONT, 10), fg=C["text2"], bg=C["card"], anchor="w").grid(
                row=0, column=2, sticky="we")
            rt, rc = right_text(x)
            tk.Label(f, text=rt, font=(FONT, 9, "bold"), fg=rc, bg=C["card"]).grid(row=0, column=3, sticky="e", padx=(int(8 * S), 0))
            sub = [x["boss"] or "自己"]
            if x["r"].get("急单"):
                sub.insert(0, "急单")
            if x["r"].get("备注") and x["r"]["备注"] not in ("急单", "开题中"):
                sub.append(ellipsis(x["r"]["备注"], 26))
            tk.Label(f, text="　·　".join(sub), font=(FONT, 9), fg=C["faint"], bg=C["card"], anchor="w").grid(
                row=1, column=2, columnspan=2, sticky="w")
        return draw

    def boss_row(b):
        def draw(card):
            f = tk.Frame(card, bg=C["card"])
            f.pack(fill="x", padx=int(14 * S), pady=int(3 * S))
            f.columnconfigure(1, weight=1)
            tk.Label(f, text=b["name"] or "没填老板", font=(FONT, 10, "bold"), fg=C["text"], bg=C["card"], width=8,
                     anchor="w").grid(row=0, column=0, sticky="w")
            tk.Label(f, text=f"{b['n']} 单　" + ellipsis("、".join(b["nos"]), 28), font=(FONT, 9), fg=C["muted"],
                     bg=C["card"], anchor="w").grid(row=0, column=1, sticky="we")
            tk.Label(f, text=b["since"], font=(FONT, 9), fg=C["text2"], bg=C["card"]).grid(
                row=0, column=2, sticky="e", padx=(int(8 * S), 0))
            tk.Label(f, text=yuan(b["owe"]), font=(FONT, 10, "bold"), fg=C["amber"], bg=C["card"]).grid(
                row=0, column=3, sticky="e", padx=(int(8 * S), 0))
        return draw

    p = d["promo"]

    def promo_row(s):
        def draw(card):
            f = tk.Frame(card, bg=C["card"])
            f.pack(fill="x", padx=int(14 * S), pady=int(3 * S))
            f.columnconfigure(1, weight=1)
            tk.Label(f, text=s["name"], font=(FONT, 10, "bold"), fg=C["text"], bg=C["card"], width=8,
                     anchor="w").grid(row=0, column=0, sticky="w")
            txt, col = promo_state(s, d["today"])
            tk.Label(f, text=txt, font=(FONT, 10), fg=col, bg=C["card"], anchor="w").grid(row=0, column=1, sticky="we")
        return draw

    def promo_foot(card):
        net = p["got"] - p["total"]
        tk.Label(card, text=promo_roi(p), font=(FONT, 9, "bold"), fg=C["green"] if net >= 0 else C["amber"],
                 bg=C["card"], anchor="w").pack(anchor="w", padx=int(14 * S), pady=(int(6 * S), 0))
        tk.Label(card, text=promo_total(p) + "　·　推广完在看板「私单成本」里记一笔", font=(FONT, 9), fg=C["faint"],
                 bg=C["card"], anchor="w", justify="left", wraplength=int(580 * S)).pack(
            anchor="w", padx=int(14 * S), pady=(int(2 * S), 0))

    def promo_section():
        if p["daily"] or p["runs"] or p["total"]:
            color = C["red"] if p["todo"] or p["due"] else C["amber"] if p["check"] else C["green"]
            section("今天开始推广", color, promo_head(p),
                    [promo_row(s) for s in p["daily"] + p["runs"]] + [promo_foot], limit=20)

    promo_first = bool(p["todo"] or p["due"] or p["check"])   # 有推广的事就排最前，都推广了放最后
    if promo_first:
        promo_section()
    if d["noprice"]:
        section("没填价格（先问清楚多少钱）", C["red"], f"{len(d['noprice'])} 单", [order_row(x) for x in d["noprice"]])
    if d["urgent"]:
        section("急单 / 快到期", C["red"], f"{len(d['urgent'])} 单", [order_row(x) for x in d["urgent"]])
    if d["doing"]:
        section("进行中", C["blue"], f"{len(d['doing'])} 单", [order_row(x) for x in d["doing"]])
    if d["todo"]:
        section("还没开始", C["faint"], f"{len(d['todo'])} 单", [order_row(x) for x in d["todo"]])
    if d["waiting"]:
        section("等开题报告（开题过了才能做）", C["line"], f"{len(d['waiting'])} 单", [order_row(x) for x in d["waiting"]])
    if d["collect"]:
        section("待结款，可以催了", C["amber"], f"{d['collect_n']} 单 {yuan(d['collect_owe'])}",
                [boss_row(b) for b in d["collect"]])
    if not (d["work_n"] or d["collect"]):
        tk.Label(body, text="今天没有要做的单", font=(FONT, 12), fg=C["muted"], bg=C["bg"]).pack(pady=int(40 * S))
    if not promo_first:
        promo_section()
    tk.Frame(body, bg=C["bg"], height=int(14 * S)).pack()

    if snap:
        root.after(400, lambda: (snapshot(root, snap), root.destroy()))
    root.mainloop()
    return result["act"]


def small(d, cfg, snap=None):
    """小窗：右下角一张卡片，几秒后自己收起。返回 'full' 表示用户要看完整清单。"""
    import tkinter as tk
    result = {"act": None}
    root = tk.Tk()
    S = root.winfo_fpixels("1i") / 96
    root.overrideredirect(True)
    root.configure(bg=C["line"])
    root.attributes("-alpha", 0.0)   # 先透明，摆好位置再淡入（自测截图时一直透明）
    if not snap:
        root.attributes("-topmost", True)
    box = tk.Frame(root, bg=C["card"])
    box.pack(fill="both", expand=True, padx=1, pady=1)
    p = d["promo"]
    promo_alert = bool(p["todo"] or p["due"])
    tk.Frame(box, bg=C["red"] if d["urgent"] or promo_alert else C["accent"], width=int(5 * S)).pack(side="left", fill="y")
    main = tk.Frame(box, bg=C["card"])
    main.pack(side="left", fill="both", expand=True, padx=(int(14 * S), int(12 * S)), pady=(int(10 * S), int(12 * S)))

    def act(a):
        result["act"] = a
        root.destroy()
    top = tk.Frame(main, bg=C["card"])
    top.pack(fill="x")
    tk.Label(top, text="记单提醒", font=(FONT, 9, "bold"), fg=C["accent"], bg=C["card"]).pack(side="left")
    tk.Label(top, text=datetime.datetime.now().strftime("%H:%M"), font=(FONT, 9), fg=C["faint"], bg=C["card"]).pack(side="left", padx=int(6 * S))
    x_btn = tk.Label(top, text="✕", font=(FONT, 10), fg=C["faint"], bg=C["card"], cursor="hand2")
    x_btn.pack(side="right")
    x_btn.bind("<Button-1>", lambda e: act("close"))
    if d["work_n"] or not promo_alert:
        head = f"还有 {d['work_n']} 单要做" + (f"，其中急 {len(d['urgent'])} 单" if d["urgent"] else "")
    else:
        head = "今天还没推广" if p["todo"] else "有推广投放到期"
    tk.Label(main, text=head, font=(FONT, 12, "bold"),
             fg=C["red"] if d["urgent"] or (promo_alert and not d["work_n"]) else C["text"],
             bg=C["card"]).pack(anchor="w", pady=(int(4 * S), 0))
    if p["todo"]:
        tk.Label(main, text="今天还没推广：" + "、".join(s["name"] for s in p["todo"]), font=(FONT, 9, "bold"),
                 fg=C["red"], bg=C["card"]).pack(anchor="w")
    for s in p["due"]:
        tk.Label(main, text=f"{s['name']} {promo_state(s, d['today'])[0]}", font=(FONT, 9, "bold"),
                 fg=C["red"], bg=C["card"]).pack(anchor="w")
    if p["check"]:
        tk.Label(main, text="看看推广费花完没有：" + "、".join(s["name"] for s in p["check"]), font=(FONT, 9),
                 fg=C["amber"], bg=C["card"]).pack(anchor="w")
    if d["noprice"]:
        tk.Label(main, text=f"{len(d['noprice'])} 单没填价格，先问清楚多少钱", font=(FONT, 9, "bold"),
                 fg=C["red"], bg=C["card"]).pack(anchor="w")
    if d["collect_n"]:
        tk.Label(main, text=f"待结款 {d['collect_n']} 单 {yuan(d['collect_owe'])}，可以催了", font=(FONT, 9),
                 fg=C["amber"], bg=C["card"]).pack(anchor="w")
    for x in (d["urgent"] + d["doing"])[:3]:
        rt, rc = right_text(x)
        line = tk.Frame(main, bg=C["card"])
        line.pack(fill="x", pady=(int(4 * S), 0))
        tk.Label(line, text=no_of(x), font=("Consolas", 9, "bold"), fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Label(line, text=ellipsis(title_of(x), 12), font=(FONT, 9), fg=C["text2"], bg=C["card"]).pack(side="left", padx=int(6 * S))
        tk.Label(line, text=rt, font=(FONT, 9), fg=rc, bg=C["card"]).pack(side="right")
    btns = tk.Frame(main, bg=C["card"])
    btns.pack(fill="x", pady=(int(10 * S), 0))
    make_button(tk, btns, "查看全部", lambda: act("full"), S, primary=True).pack(side="left")
    make_button(tk, btns, "打开看板", lambda: (open_dashboard(), act("close")), S).pack(side="left", padx=int(8 * S))

    W = int(380 * S)
    root.update_idletasks()
    H = root.winfo_reqheight()
    if snap:
        root.geometry(f"{W}x{H}+{root.winfo_screenwidth() - W}+0")
        root.after(400, lambda: (snapshot(root, snap), root.destroy()))
        root.mainloop()
        return None
    wa = work_area() or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    root.geometry(f"{W}x{H}+{wa[2] - W - int(16 * S)}+{wa[3] - H - int(16 * S)}")

    # 淡入；鼠标停在上面就不自动收起
    def fade(a=0.0):
        a = min(a + 0.12, 1.0)
        root.attributes("-alpha", a)
        if a < 1.0:
            root.after(16, fade, a)
    fade()
    timer = {"id": None}
    stay = int(cfg.get("小窗停留秒", 20)) * 1000

    def arm():
        timer["id"] = root.after(stay, lambda: act("timeout"))

    def disarm(_=None):
        if timer["id"]:
            root.after_cancel(timer["id"])
            timer["id"] = None
    root.bind("<Enter>", disarm)
    root.bind("<Leave>", lambda e: (disarm(), arm()))
    arm()
    root.mainloop()
    return result["act"]


# ---------------- 定时任务 ----------------
def pythonw():
    p = Path(sys.executable).with_name("pythonw.exe")
    return str(p if p.exists() else sys.executable)


def powershell(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


PS_UTF8 = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$ProgressPreference = 'SilentlyContinue'\n"


def show(r):
    """只打正常输出；出错才把错误打出来（PowerShell 的进度流很吵）。"""
    print(r.stdout.strip())
    if r.returncode:
        print(r.stderr.strip())


def install(cfg):
    times = [t for t in cfg.get("时间", []) if re.fullmatch(r"\d{1,2}:\d{2}", t)]
    if not times:
        sys.exit("配置/提醒.json 里的「时间」没有有效的 HH:MM")
    trig = "\n".join(f"$t += New-ScheduledTaskTrigger -Daily -At '{t}'" for t in times)
    if cfg.get("登录时提醒", True):
        trig += "\n$l = New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\"; $l.Delay = 'PT1M'; $t += $l"
    script = PS_UTF8 + f"""$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{pythonw()}' -Argument '"{HERE / 'remind.py'}" --auto' -WorkingDirectory '{HERE}'
$t = @()
{trig}
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Description '记单平台：每天提醒还有哪些单要做（当天第一次弹大窗，之后弹小窗）' -Force | Out-Null
"已安装，下次提醒 $((Get-ScheduledTaskInfo -TaskName '{TASK_NAME}').NextRunTime.ToString('MM-dd HH:mm'))"
"""
    r = powershell(script)
    show(r)
    if r.returncode == 0:
        print("提醒时间：" + "、".join(times) + ("，另外开机登录后 1 分钟" if cfg.get("登录时提醒", True) else ""))
    return r.returncode


def uninstall():
    r = powershell(PS_UTF8 + f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false; '已删除定时提醒'")
    show(r)
    return r.returncode


def status():
    r = powershell(PS_UTF8 + f"""$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($i) {{
  $last = if ($i.LastRunTime.Year -lt 2000) {{ '还没跑过' }} else {{ $i.LastRunTime.ToString('MM-dd HH:mm') }}
  "已安装。上次运行 $last，下次 $($i.NextRunTime.ToString('MM-dd HH:mm'))"
}} else {{ '没安装（双击「安装每日提醒.bat」）' }}""")
    show(r)
    st = load_state()
    if st:
        print(f"今天弹过大窗：{'是' if st.get('弹窗日') == datetime.date.today().isoformat() else '否'}；最近一次：{st.get('上次', '—')}（{st.get('上次方式', '—')}）")
    return r.returncode


# ---------------- 入口 ----------------
def main():
    p = argparse.ArgumentParser(description="记单平台每日提醒")
    p.add_argument("--auto", action="store_true", help="定时任务用：当天第一次弹大窗，之后弹小窗")
    p.add_argument("--small", action="store_true", help="直接弹小窗")
    p.add_argument("--text", action="store_true", help="只打印，不弹窗")
    p.add_argument("--dry", action="store_true", help="只说这次会弹哪种窗，不弹")
    p.add_argument("--spawn", action="store_true", help="用 pythonw 在后台另起一个提醒（bat 用，黑窗口马上关）")
    p.add_argument("--install", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--snapshot", metavar="PNG", help="自测：把窗口画进图片，不显示")
    a = p.parse_args()
    cfg = load_cfg()

    if a.install:
        sys.exit(install(cfg))
    if a.uninstall:
        sys.exit(uninstall())
    if a.status:
        sys.exit(status())
    if a.spawn:
        subprocess.Popen([pythonw(), str(HERE / "remind.py")], cwd=str(HERE), close_fds=True,
                         creationflags=0x00000008 | 0x00000200)   # 脱离控制台，bat 窗口随即关闭
        return

    d = build(cfg)
    if a.text:
        print(as_text(d))
        return
    st = load_state()
    today = d["today"].isoformat()
    if a.small:
        mode = "small"
    elif a.auto:
        p = d["promo"]
        busy = d["work_n"] or d["collect_n"] or d["noprice"] or p["todo"] or p["due"]
        if st.get("弹窗日") != today:
            # 当天第一次：有事就弹大窗；「看看推广费花完没有」也算，每天开始推广要提醒到
            mode = "popup" if busy or p["check"] else None
        else:
            mode = "small" if busy else None   # 之后只在有事时弹小窗，没事就不打扰
    else:
        mode = "popup"                       # 手动提醒总是大窗
    if a.dry:
        print({"popup": "弹大窗", "small": "弹小窗", None: "不弹（没有待办）"}[mode], "·", summary(d))
        return
    dpi_aware()
    if a.snapshot:
        (small if mode == "small" else popup)(d, cfg, snap=a.snapshot)
        return
    if mode is None:
        return
    st.update({"上次": datetime.datetime.now().isoformat(timespec="seconds"),
               "上次方式": "弹窗" if mode == "popup" else "小窗"})
    if mode == "popup":
        st["弹窗日"] = today
    save_state(st)
    act = small(d, cfg) if mode == "small" else popup(d, cfg)
    while act in ("full", "snooze"):
        if act == "snooze":
            time.sleep(int(cfg.get("稍后提醒分钟", 30)) * 60)
        act = popup(build(cfg), cfg)


if __name__ == "__main__":
    main()
