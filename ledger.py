# -*- coding: utf-8 -*-
"""记单平台写入口：AI 只能通过本脚本增改订单，禁止手改 订单库/*.json。
用法见 AI必读.md。"""
import argparse, json, os, re, sys, time, datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOP = ["题目", "全称", "渠道", "日期", "期限", "价格", "定金", "尾款", "工程师", "老板", "来源", "急单", "备注", "月"]
ID_RE = re.compile(r"[A-Za-z]{0,2}-?\d{2}-\d{3}|[A-Za-z]\d{3}|[A-Za-z]{1,2}-\d{3}")
# 渠道.json 没写「台账列」时的列序；「环节」展开成该类别的全部环节
LEDGER_COLS = ["时间", "工程师", "单号", "价格", "环节", "结款工程师", "备注"]


def load_cfg():
    """读类别、渠道配置；看板每次请求都重读，改配置不用重启。"""
    global CATS, CHANNELS
    CATS = json.loads((HERE / "配置" / "类别.json").read_text(encoding="utf-8"))
    CHANNELS = json.loads((HERE / "配置" / "渠道.json").read_text(encoding="utf-8"))
    return CATS, CHANNELS


load_cfg()


def store_dir(cat):
    d = ROOT / CATS[cat]["目录"] / "订单库"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_month(cat, month):
    p = store_dir(cat) / f"{month}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


class Lock:
    """简单文件锁，多个 AI 同时写时排队。"""

    def __init__(self):
        self.p = HERE / ".ledger.lock"

    def __enter__(self):
        for _ in range(100):
            try:
                self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > 30:
                        self.p.unlink()
                except OSError:
                    pass
                time.sleep(0.1)
        sys.exit("加锁超时：另一个写入进程未结束")

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            self.p.unlink()
        except OSError:
            pass


SETTLE_LOG = HERE / "结款记录.jsonl"   # 结款打钩的时间，提醒里算各老板「上次结款多久了」用（用户 2026-11-25）


def log_settled(cat, month, rows):
    """和磁盘上的旧版比，结款从没打钩变成打钩的记一笔时间；记失败不影响写入。"""
    try:
        old = {r.get("id") or r["单号"]: (r.get("环节") or {}).get("结款") for r in read_month(cat, month)}
        add = [{"时间": now(), "类别": cat, "单号": r["单号"], "老板": r.get("老板"), "写入者": r.get("写入者")}
               for r in rows
               if (r.get("环节") or {}).get("结款") is True and old.get(r.get("id") or r["单号"]) is not True]
        if add:
            with open(SETTLE_LOG, "a", encoding="utf-8") as f:
                f.writelines(json.dumps(x, ensure_ascii=False) + "\n" for x in add)
    except Exception:
        pass


def write_month(cat, month, rows):
    log_settled(cat, month, rows)
    p = store_dir(cat) / f"{month}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def all_rows():
    out = []
    for cat in CATS:
        for f in sorted(store_dir(cat).glob("*.json")):
            out += json.loads(f.read_text(encoding="utf-8"))
    return out


def find(cat, no):
    for f in sorted(store_dir(cat).glob("*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            if r["单号"] == no:
                return f.stem, r
    return None, None


def norm(v):
    """√/X/待定/空 归一：true/false/null，其余原样（日期串、快递号）。"""
    if v is None:
        return None
    s = str(v).replace("\xa0", " ").strip()
    if s == "":
        return None
    if s in ("√", "✓", "true", "True", "是", "完成"):   # 「完成 / 不做 / 未做」和看板按钮同义
        return True
    if s in ("X", "x", "×", "false", "False", "否", "不做"):
        return False
    if s in ("null", "None", "-", "未做", "空"):
        return None
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def money(v):
    if v is None or str(v).strip() == "":
        return None
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def apply(rec, cat, kv):
    for k, v in kv.items():
        if k == "客户":
            sys.exit("字段「客户」已改名「老板」：中介单写中介名，自己接的单不填")
        if k in CATS[cat]["环节"]:
            rec["环节"][k] = norm(v)
        elif k in ("价格", "定金", "尾款"):
            rec[k] = money(v)
        elif k == "急单":
            rec[k] = norm(v) is True
        elif k in ("日期", "期限") and v and not re.fullmatch(r"\d{8}", v):
            sys.exit(f"{k}写成 YYYYMMDD：{v}")
        elif k == "来源":   # 私单从哪个推广店铺来的，用来算回本
            shops = load_cost_cfg()["店铺"]
            if v and v not in shops:
                sys.exit(f"来源写店铺名（{'、'.join(shops)}）：{v}；新店铺先在 配置/成本.json 的「店铺」里加")
            rec[k] = v or None
        elif k in TOP:
            rec[k] = v if v != "" else None
        else:
            sys.exit(f"未知字段 {k}；可用：{TOP + CATS[cat]['环节']}")


def new_rec(cat, no, month, writer):
    rows = read_month(cat, month)
    return {"id": f"{cat}-{month}-{len(rows) + 1:04d}", "单号": no, "类别": cat, "月": month,
            "题目": None, "渠道": "中介", "日期": None, "期限": None, "价格": None, "定金": None, "尾款": None,
            "工程师": None, "老板": None, "来源": None, "环节": {s: None for s in CATS[cat]["环节"]},
            "急单": False, "备注": None, "更新": now(), "写入者": writer}


def kvs(items):
    d = {}
    for it in items:
        if "=" not in it:
            sys.exit(f"参数应为 键=值：{it}")
        k, v = it.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def check_channel(kv):
    if "渠道" in kv and kv["渠道"] not in CHANNELS:
        sys.exit(f"未知渠道 {kv['渠道']}，先在 配置/渠道.json 加")


# 价格必填（用户 2026-10-15：「没有价格是万万不可的」）：一口价写 价格，定金尾款的写 定金 / 尾款，自己的项目可以写 0
PRICE_HINT = "价格必填：先问用户这单多少钱（一口价写 价格=，定金尾款的写 定金= 尾款=）"


def money_dups(rows=None):
    """同一单号在两个以上类别都记了钱（金额大于 0）的，返回 {单号: [(类别, 金额), ...]}。"""
    seen = {}
    for r in rows if rows is not None else all_rows():
        t = (r.get("价格") or 0) if r.get("价格") is not None else (r.get("定金") or 0) + (r.get("尾款") or 0)
        if t > 0:
            seen.setdefault(r["单号"], []).append((r["类别"], t))
    return {k: v for k, v in seen.items() if len(v) > 1}


def has_price(r):
    return any(r.get(k) is not None for k in ("价格", "定金", "尾款"))


def cmd_add(a):
    kv = kvs(a.kv)
    if a.类别 not in CATS:
        sys.exit(f"未知类别 {a.类别}，可用 {list(CATS)}")
    # 用户 2026-11-15 定「私单都是自营」：老板写 自己 的默认记 自营，其余照旧默认 中介
    kv.setdefault("渠道", "自营" if kv.get("老板") == "自己" else "中介")
    check_channel(kv)
    if kv.get("日期") and not re.fullmatch(r"\d{8}", kv["日期"]):
        sys.exit(f"日期写成 YYYYMMDD：{kv['日期']}")
    month = kv.get("月") or (kv.get("日期") or datetime.date.today().strftime("%Y%m%d"))[:6]
    if not re.fullmatch(r"\d{6}", month):
        sys.exit(f"月写成 YYYYMM：{month}")
    with Lock():
        m, r = find(a.类别, a.单号)
        if r:
            sys.exit(f"单号已存在（{m}）：{a.单号}；改请用 set")
        rec = new_rec(a.类别, a.单号, month, a.写入者)
        apply(rec, a.类别, kv)
        if not has_price(rec):
            sys.exit(PRICE_HINT)
        dup = [x for x in all_rows() if x["单号"] == a.单号 and x["类别"] != a.类别]
        if dup and (rec.get("价格") or 0) + (rec.get("定金") or 0) + (rec.get("尾款") or 0) > 0:
            print(f"注意：{a.单号} 在" + "、".join(CATS[x['类别']]['名称'] for x in dup)
                  + "里已经记过，价格是不是已经含在那边？不另收就改成 价格=0 结款=X", file=sys.stderr)
        rec["月"] = month
        rows = read_month(a.类别, month)
        rows.append(rec)
        write_month(a.类别, month, rows)
    print("已添加", rec["id"], a.单号)


def cmd_set(a):
    kv = kvs(a.kv)
    if "月" in kv:
        sys.exit("月 不能用 set 改：记录按月分文件存放，确需改月请告诉用户")
    check_channel(kv)
    with Lock():
        m, r = find(a.类别, a.单号)
        if not r:
            sys.exit(f"找不到 {a.类别} 单号 {a.单号}")
        rows = read_month(a.类别, m)
        for x in rows:
            if x["单号"] == a.单号:
                had = has_price(x)
                apply(x, a.类别, kv)
                if had and not has_price(x):
                    sys.exit("价格不能清空；" + PRICE_HINT)
                x["更新"] = now()
                x["写入者"] = a.写入者
                if not has_price(x):
                    print(f"注意：{a.单号} 还没有价格，先问用户多少钱，再 set 价格=…", file=sys.stderr)
        write_month(a.类别, m, rows)
    print("已更新", a.单号, kv)


def total(r):
    return r["价格"] if r["价格"] is not None else (r["定金"] or 0) + (r["尾款"] or 0)


def cmd_list(a):
    n = 0
    print("类别\t月\t单号\t渠道\t老板\t来源\t金额\t环节（√完成 X不做 ·未做）\t题目\t期限\t备注")
    for r in all_rows():
        if a.类别 and r["类别"] != a.类别:
            continue
        if a.月 and r["月"] != a.月:
            continue
        if a.未结款 and r["环节"].get("结款") in (True, False):   # X = 不单独收钱
            continue
        n += 1
        st = " ".join(f"{k}:{'√' if v is True else 'X' if v is False else '·' if v is None else v}"
                      for k, v in r["环节"].items())
        print(f"{r['类别']}\t{r['月']}\t{r['单号']}\t{r['渠道']}\t{r.get('老板') or '自己'}\t{r.get('来源') or ''}\t{total(r):g}\t{st}\t{r['题目'] or ''}"
              f"\t{r.get('期限') or ''}\t{r.get('备注') or ''}")
    print(f"-- 共 {n} 条")


def cell_name(r):
    """台账单号格子、结款单里每行的名字。
    填了「全称」（用户给的单号原文，如 A-26-030-基于STM32的xxx设计）就原样用；
    没填就单号紧接题目，不加空格（用户给的写法，如 A-26-066基于stm32的xxx系统设计）。"""
    if r.get("全称"):
        return r["全称"]
    return r["单号"] if not r.get("题目") or r["题目"] == r["单号"] else f"{r['单号']}{r['题目']}"


def cmd_export(a):
    """按该渠道台账的列序出一行文本，给用户自己填进 Excel。"""
    m, r = find(a.类别, a.单号)
    if not r:
        sys.exit("找不到该单")

    def c(v):
        return "√" if v is True else "X" if v is False else ("" if v is None else str(v))

    def yuan(v):
        return "" if v is None else f"{v:g}"

    no = cell_name(r)
    note = r.get("备注") or ""
    if r.get("急单") and "急单" not in note:
        note = "急单" + ("；" + note if note else "")
    val = {"时间": r.get("日期") or "", "工程师": r.get("工程师") or "", "单号": no,
           "价格": yuan(r.get("价格")), "定金": yuan(r.get("定金")), "尾款": yuan(r.get("尾款")),
           "结款工程师": r.get("老板") or "", "老板": r.get("老板") or "", "备注": note}
    head, out = [], []
    for col in CHANNELS.get(r["渠道"], {}).get("台账列", LEDGER_COLS):
        if col == "环节":
            for s in CATS[a.类别]["环节"]:
                head.append(s)
                out.append(c(r["环节"].get(s)))
        else:
            head.append(col)
            out.append(val.get(col, ""))
    if a.表头:
        print(" | ".join(head))
    print(" | ".join(out))


def cmd_check(a):
    seen, bad = set(), 0
    for r in all_rows():
        k = (r["类别"], r["单号"])
        if k in seen:
            print("重复", k)
            bad += 1
        if not has_price(r):
            print("缺价格（问用户多少钱）", k, r.get("老板") or "自己", r.get("题目") or "")
            bad += 1
        seen.add(k)
        if r.get("渠道") not in CHANNELS:
            print("未知渠道", k, r.get("渠道"))
            bad += 1
        if "客户" in r or "老板" not in r:
            print("缺 老板 字段（旧名 客户）", k)
            bad += 1
        if r.get("来源") and r["来源"] not in load_cost_cfg()["店铺"]:
            print("来源不在 配置/成本.json 的店铺里", k, r["来源"])
            bad += 1
        extra = set(r.get("环节", {})) - set(CATS[r["类别"]]["环节"])
        if extra:
            print("环节不在配置里", k, sorted(extra))
            bad += 1
        for f in ("日期", "期限"):
            if r.get(f) and not re.fullmatch(r"\d{8}", str(r[f])):
                print(f + "格式不对", k, r[f])
                bad += 1
    shops, ids = load_cost_cfg()["店铺"], set()
    for c in all_costs(voided=True):
        if c.get("id") in ids:
            print("私单成本编号重复", c.get("id"))
            bad += 1
        ids.add(c.get("id"))
        if c.get("作废"):
            continue
        if c.get("店铺") not in shops:
            print("私单成本的店铺不在 配置/成本.json 里", c.get("id"), c.get("店铺"))
            bad += 1
        if not isinstance(c.get("金额"), (int, float)) or c["金额"] <= 0:
            print("私单成本金额不对", c.get("id"), c.get("金额"))
            bad += 1
        if not isinstance(c.get("天数", 1), int) or c.get("天数", 1) < 0:
            print("私单成本天数不对（0 = 用完为止，其余写天数）", c.get("id"), c.get("天数"))
            bad += 1
        if not re.fullmatch(r"\d{8}", str(c.get("日期") or "")) or str(c.get("id"))[1:7] != str(c.get("日期"))[:6]:
            print("私单成本日期不对（要 YYYYMMDD，且和编号同月）", c.get("id"), c.get("日期"))
            bad += 1
    settle, seen = load_settle_cfg(), {}
    for owner, subs in settle.items():
        for b in subs or []:
            if b in settle:
                print("结款名单：", b, "既是结款工程师，又挂在", owner, "下面")
                bad += 1
            if b in seen:
                print("结款名单：", b, "同时挂在", seen[b], "和", owner, "下面")
                bad += 1
            seen[b] = owner
    print("检查完成，问题", bad)
    # 「自己」是自营私单（用户 2026-11-15 定），钱从平台直接到账，不归任何结款工程师，不报
    loose = sorted({r.get("老板") for r in all_rows() if r.get("老板") and r.get("老板") != "自己"} - set(settle) - set(seen))
    if loose:
        print("⚠ 这些老板还没归到结款工程师（配置/结款.json）：" + "、".join(loose))
    for no, items in money_dups().items():
        print(f"⚠ 可能重复记钱：{no}（" + "、".join(f"{CATS[c]['名称']} ¥{v:g}" for c, v in items)
              + "）。同一单的论文、仿真多半已含在实物价里，问用户是不是另收；不另收就把重复那条 价格=0 结款=X")
    n = len(pending_manual())
    if n:
        print(f"另有看板手动改动 {n} 处等用户确认（ledger.py manual 查看）")
    if bad:
        sys.exit(1)   # 查出问题时退出码非 0，脚本和 AI 能据此判断


# ---------------- 看板手动改动：用户在看板上改的东西，AI 要在对话里跟用户确认 ----------------
MANUAL_LOG = HERE / "手动改动.jsonl"
MANUAL_WHO = "看板手动"
VALUE_NAME = {"完成": True, "不做": False, "未做": None}
EDIT_FIELDS = ("备注", "期限", "价格", "定金", "尾款", "来源")   # 看板上能改的单子字段


def read_manual():
    if not MANUAL_LOG.exists():
        return []
    out = []
    for line in MANUAL_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def write_manual(items):
    tmp = MANUAL_LOG.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    os.replace(tmp, MANUAL_LOG)


def pending_manual():
    return [i for i in read_manual() if i.get("状态") == "待确认"]


def show_value(v, field=False):
    if field:
        return "空" if v in (None, "") else (f"{v:g}" if isinstance(v, float) else str(v))
    return "完成" if v is True else "不做" if v is False else "未做" if v in (None, "") else str(v)


def entry_name(i):
    return i.get("环节") or i.get("字段")


def entry_show(i, v):
    return show_value(v, field="字段" in i)


def parse_field(field, text):
    """看板传来的文字转成要存的值；空串 = 清空。"""
    t = (text or "").strip()
    if t == "":
        return None
    if field == "期限":
        t = t.replace("-", "")
        if not re.fullmatch(r"\d{8}", t):
            raise ValueError("期限写成日期，如 2026-10-20")
        return t
    if field in ("价格", "定金", "尾款"):
        v = money(t)
        if v is None:
            raise ValueError(f"{field}要写数字")
        return v
    if field == "来源":
        shops = load_cost_cfg()["店铺"]
        if t not in shops:
            raise ValueError(f"来源只能选店铺：{'、'.join(shops)}")
        return t
    if len(t) > 200:
        raise ValueError("备注太长（最多 200 字）")
    return t


def manual_set(cat, no, stage=None, value=None, field=None, text=None):
    """看板上用户手动改一处：环节（完成/不做/未做，或一段文字如日期、快递号）
    或单子字段（备注/期限/价格/定金/尾款）。改完记一笔「待确认」，没变化返回 None。"""
    load_cfg()
    if cat not in CATS:
        raise ValueError(f"未知类别 {cat}")
    if field is not None:
        if field not in EDIT_FIELDS:
            raise ValueError(f"看板上不能改「{field}」")
        new = parse_field(field, text)
    else:
        if stage not in CATS[cat]["环节"]:
            raise ValueError(f"{CATS[cat]['名称']}没有环节「{stage}」")
        if text is not None:
            if len(text.strip()) > 60:
                raise ValueError("环节里的字太长（最多 60 字）")
            new = norm(text)   # √ / X / 空 也认
        elif value in (True, False, None):
            new = value
        else:
            raise ValueError("只能改成 完成 / 不做 / 未做")
    try:
        with Lock():
            m, r = find(cat, no)
            if not r:
                raise ValueError(f"找不到 {cat} 单号 {no}")
            old = r.get(field) if field else r["环节"].get(stage)
            if old == new:
                return None
            if field in ("价格", "定金", "尾款") and new is None and has_price(r) and not has_price({**r, field: None}):
                raise ValueError("价格不能清空：这单多少钱就写多少")
            rows = read_month(cat, m)
            for x in rows:
                if x["单号"] == no:
                    if field:
                        x[field] = new
                    else:
                        x["环节"][stage] = new
                    x["更新"], x["写入者"] = now(), MANUAL_WHO
            write_month(cat, m, rows)
            return add_manual({"类别": cat, "单号": no, ("字段" if field else "环节"): field or stage,
                               "旧值": old, "新值": new})
    except SystemExit as ex:   # Lock 超时用的是 sys.exit，看板线程里换成普通错误
        raise RuntimeError(str(ex))


def add_manual(e):
    """记一笔「待确认」的看板手动改动（调用方已加锁）。"""
    items = read_manual()
    e = {"编号": max((i.get("编号", 0) for i in items), default=0) + 1, "时间": now(), "来源": MANUAL_WHO,
         **e, "状态": "待确认"}
    items.append(e)
    write_manual(items)
    return e


def manual_line(i):
    """一处手动改动的一句话说明。"""
    if i.get("对象") == "成本":
        return f"私单成本 {i['操作']}：{i['内容']}（{i['成本编号']}）"
    if i.get("对象") == "结款":
        return f"一键结款：{i['内容']}（{'、'.join(d['单号'] for d in i.get('明细', []))}）"
    return f"{i['类别']} {i['单号']}  {entry_name(i)}：{entry_show(i, i['旧值'])} → {entry_show(i, i['新值'])}"


def cmd_manual(a):
    items = pending_manual()
    if not items:
        print("没有待确认的手动改动")
        return
    print(f"看板上有 {len(items)} 处手动改动，要在对话里问用户是不是本人操作的：")
    for i in items:
        print(f"  #{i['编号']}  {i['时间'][5:16].replace('T', ' ')}  {manual_line(i)}")
    print("用户确认是本人：ledger.py confirm <编号…|all>；用户说不是：ledger.py reject <编号…> 改回原值")


def resolve_manual(ids, revert, writer):
    done = []
    with Lock():
        items = read_manual()
        # 改回从最新一条往回撤：同一处连改几次时，逐条撤回才能回到最初的值
        order = sorted(items, key=lambda i: i.get("编号", 0), reverse=True) if revert else items
        for i in order:
            if i.get("状态") != "待确认" or ("all" not in ids and str(i["编号"]) not in ids):
                continue
            note = ""
            if revert and i.get("对象") == "成本":
                # 看板上记的一笔撤回 = 作废它；看板上作废的撤回 = 恢复它
                add = i["操作"] == "新增"
                try:
                    changed = _mark_cost(i["成本编号"], add, writer)
                except ValueError:
                    changed = False
                note = ("，已作废这笔" if add else "，已恢复这笔") if changed else "，现在已经又变过，没有动它"
            elif revert and i.get("对象") == "结款":
                # 一键结款撤回：整批改回原值；之后又被改过的单不动
                back, kept = 0, []
                for d in i.get("明细", []):
                    m, r = find(d["类别"], d["单号"])
                    if r and r["环节"].get("结款") is True:
                        rows = read_month(d["类别"], m)
                        for x in rows:
                            if x["单号"] == d["单号"]:
                                x["环节"]["结款"] = d["旧值"]
                                x["更新"], x["写入者"] = now(), writer
                        write_month(d["类别"], m, rows)
                        back += 1
                    else:
                        kept.append(d["单号"])
                note = f"，已改回 {back} 单" + (f"；{'、'.join(kept)} 现在的值已经又变过，没有动" if kept else "")
            elif revert:
                m, r = find(i["类别"], i["单号"])
                key, stage = i.get("字段"), i.get("环节")
                cur = (r.get(key) if key else r["环节"].get(stage)) if r else None
                if r and cur == i["新值"]:
                    rows = read_month(i["类别"], m)
                    for x in rows:
                        if x["单号"] == i["单号"]:
                            if key:
                                x[key] = i["旧值"]
                            else:
                                x["环节"][stage] = i["旧值"]
                            x["更新"], x["写入者"] = now(), writer
                    write_month(i["类别"], m, rows)
                    note = f"，已改回「{entry_show(i, i['旧值'])}」"
                else:
                    note = "，现在的值已经又变过，没有动它"
            i["状态"], i["处理时间"], i["处理者"] = ("用户说不是本人" if revert else "用户确认"), now(), writer
            what = (f"私单成本{i['操作']} {i['内容']}" if i.get("对象") == "成本"
                    else f"一键结款 {i['内容']}" if i.get("对象") == "结款" else f"{i['单号']} {entry_name(i)}")
            done.append(f"#{i['编号']} {what}{note}")
        write_manual(items)
    print(("已改回：" if revert else "已确认：") + ("；".join(done) if done else "没有匹配的待确认编号"))


def cmd_confirm(a):
    resolve_manual(a.编号, False, a.写入者)


def cmd_reject(a):
    resolve_manual(a.编号, True, a.写入者)


# ---------------- 私单成本：各平台店铺花的推广费，按月汇总 ----------------
# 用户 2026-10-21：「加一个私单成本栏…有推广成本…按每个月的成本算」。钱哪天花的就算哪个月。
COST_DIR = HERE / "成本库"
COST_FIELDS = ("日期", "店铺", "方式", "金额", "天数", "备注")
COST_ID_RE = re.compile(r"C(\d{6})-\d{4}")


def load_cost_cfg():
    """配置/成本.json：店铺 → 平台、要不要每天提醒推广；方式只是下拉候选，可以写别的。"""
    p = HERE / "配置" / "成本.json"
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg.setdefault("店铺", {})
    cfg.setdefault("方式", {})
    return cfg


def read_costs(month):
    p = COST_DIR / f"{month}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def write_costs(month, rows):
    COST_DIR.mkdir(exist_ok=True)
    p = COST_DIR / f"{month}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def all_costs(voided=False):
    """全部私单成本；作废的默认不算。"""
    out = []
    for f in sorted(COST_DIR.glob("*.json")) if COST_DIR.exists() else []:
        out += [c for c in json.loads(f.read_text(encoding="utf-8")) if voided or not c.get("作废")]
    return out


def find_cost(cid):
    m = COST_ID_RE.fullmatch(str(cid or ""))
    if m:
        for c in read_costs(m.group(1)):
            if c["id"] == cid:
                return m.group(1), c
    return None, None


def days_text(c):
    """天数：0 = 用完为止（充进去的推广费，花完就停），1 = 只管当天，N = 投 N 天。"""
    n = c.get("天数", 1)
    return "用完为止" if n == 0 else f"{n} 天"


def cost_text(c):
    t = f"{c['日期'][4:6]}-{c['日期'][6:]} {c['店铺']}" + (f" {c['方式']}" if c.get("方式") else "") + f" ¥{c['金额']:g}"
    return t + (f"（{days_text(c)}）" if c.get("天数", 1) != 1 else "")


def parse_cost(kv, cfg, base=None):
    """键=值 → 一条私单成本（base 是要改的原记录）；写得不对抛 ValueError。"""
    # 天数默认「用完为止」：几个平台的推广都是先充钱、花完就停（用户 2026-10-21：「不知道管多少天，用完就没了」）
    c = dict(base) if base else {"日期": datetime.date.today().strftime("%Y%m%d"), "店铺": None, "平台": None,
                                  "方式": None, "金额": None, "天数": 0, "备注": None}
    for k, v in kv.items():
        v = str(v if v is not None else "").replace("\xa0", " ").strip()
        if k not in COST_FIELDS:
            raise ValueError(f"私单成本没有字段「{k}」；可用：{'、'.join(COST_FIELDS)}")
        if k == "日期":
            v = v.replace("-", "")
            try:
                datetime.datetime.strptime(v, "%Y%m%d")
            except ValueError:
                raise ValueError(f"日期写成 YYYYMMDD：{v}") from None
            c[k] = v
        elif k == "店铺":
            if v not in cfg["店铺"]:
                raise ValueError(f"没有店铺「{v}」，现有：{'、'.join(cfg['店铺'])}；新店铺先在 配置/成本.json 的「店铺」里加")
            c["店铺"], c["平台"] = v, cfg["店铺"][v].get("平台") or v
        elif k == "金额":
            m = money(v)
            if m is None or m <= 0:
                raise ValueError("金额写大于 0 的数字")
            c[k] = m
        elif k == "天数":
            if v in ("用完为止", "用完", "0"):
                c[k] = 0
            elif re.fullmatch(r"\d{1,3}", v):
                c[k] = int(v)
            else:
                raise ValueError("天数写「用完为止」（花完就停的），或者天数：只管当天写 1，投 7 天写 7")
        else:   # 方式 / 备注
            if len(v) > (30 if k == "方式" else 200):
                raise ValueError(f"{k}太长")
            c[k] = v or None
    if not c.get("店铺"):
        raise ValueError("要写 店铺=（" + "、".join(cfg["店铺"]) + "）")
    if c.get("金额") is None:
        raise ValueError("要写 金额=：这笔花了多少钱")
    return c


def _new_cost(c, writer):
    """存一条新的私单成本，返回带编号的记录（调用方已加锁）。作废的也占编号，编号不会重复。"""
    month = c["日期"][:6]
    rows = read_costs(month)
    n = max((int(x["id"][-4:]) for x in rows), default=0) + 1
    c = {"id": f"C{month}-{n:04d}", **c, "更新": now(), "写入者": writer}
    rows.append(c)
    write_costs(month, rows)
    return c


def _mark_cost(cid, void, writer):
    """作废（void=True）或恢复一条私单成本，状态没变返回 False（调用方已加锁）。只作废不删除，留底。"""
    m, c = find_cost(cid)
    if not c:
        raise ValueError(f"找不到私单成本 {cid}")
    if bool(c.get("作废")) == void:
        return False
    rows = read_costs(m)
    for x in rows:
        if x["id"] == cid:
            if void:
                x["作废"] = now()
            else:
                x.pop("作废", None)
            x["更新"], x["写入者"] = now(), writer
    write_costs(m, rows)
    return True


def manual_cost(op, data=None, cid=None):
    """看板上用户手动记一笔私单成本（新增）或作废一笔；记一笔「待确认」，等 AI 跟用户确认。"""
    try:
        with Lock():
            if op == "新增":
                c = _new_cost(parse_cost(data or {}, load_cost_cfg()), MANUAL_WHO)
            elif op == "作废":
                if not _mark_cost(cid, True, MANUAL_WHO):
                    return None
                c = find_cost(cid)[1]
            else:
                raise ValueError("操作只能是 新增 / 作废")
            return add_manual({"对象": "成本", "成本编号": c["id"], "操作": op, "内容": cost_text(c)})
    except SystemExit as ex:   # Lock 超时用的是 sys.exit，看板线程里换成普通错误
        raise RuntimeError(str(ex))


def shop_revenue(shop, cfg=None, month=None):
    """店铺配了「工作台」时，从只读快照 sqlite 读成交额（回本用）。没配或读不到返回 None。
    只认 status=='交易成功'，金额优先 transaction_amount_fen；只算回本起算日当天及以后下单的。
    month 给了就再按下单月份(YYYYMM)过滤。另返回不限起算日的「总收/总单」作参考。只读不写。"""
    cfg = cfg or load_cost_cfg()
    wb = (cfg["店铺"].get(shop) or {}).get("工作台")
    if not wb or not wb.get("快照") or not Path(wb["快照"]).exists():
        return None
    import sqlite3
    from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

    def to_fen(v):
        try:
            return int((Decimal(str(v).strip()) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, AttributeError):
            return None

    cut = (wb.get("回本起算日") or "").strip() or None
    try:
        conn = sqlite3.connect(f"file:{wb['快照']}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
            if not {"status", "ordered_at"} <= cols:
                return None
            try:
                meta = {str(k): v for k, v in conn.execute("SELECT key, value FROM metadata")}
            except sqlite3.Error:
                meta = {}
            rows = conn.execute("SELECT ordered_at, transaction_amount_fen, transaction_amount_cny "
                                "FROM orders WHERE status = '交易成功'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    收fen = 总fen = 单 = 总单 = 0
    for oa, fen, cny in rows:
        v = None
        if fen is not None and str(fen).strip():
            try:
                v = int(str(fen).strip())
            except ValueError:
                v = to_fen(cny)
        else:
            v = to_fen(cny)
        if v is None:
            continue
        总fen += v
        总单 += 1
        d = str(oa or "")[:10].replace("-", "")
        if len(d) == 8 and (not cut or d >= cut) and (not month or d[:6] == month):
            收fen += v
            单 += 1
    return {"收": 收fen / 100, "单": 单, "总收": 总fen / 100, "总单": 总单, "起算日": cut,
            "账号": meta.get("account_display_name"), "快照日": meta.get("collected_on"), "范围": meta.get("scope")}


def roi(month=None):
    """回本：推广来的私单收入 vs 推广花费。month=None 算全部月份。
    返回 {平台: {"花": 元, "收": 元, "单": 单数}}；花费按花钱那天算。
    收入来源：配了「工作台」的店铺用快照成交额（按起算日、按月过滤），其余用订单库里标了来源的单。"""
    cfg = load_cost_cfg()
    shops, out = cfg["店铺"], {}

    def plat(name):
        return (shops.get(name) or {}).get("平台") or name

    for c in all_costs():
        if month is None or c["日期"][:6] == month:
            out.setdefault(c.get("平台") or c["店铺"], {"花": 0, "收": 0, "单": 0})["花"] += c["金额"]
    wb_shops = set()
    for shop, sc in shops.items():
        if sc.get("工作台"):
            rev = shop_revenue(shop, cfg, month)
            if rev is not None:
                wb_shops.add(shop)
                p = out.setdefault(plat(shop), {"花": 0, "收": 0, "单": 0})
                p["收"] += rev["收"]
                p["单"] += rev["单"]
    for r in all_rows():
        src = r.get("来源")
        if src and src not in wb_shops and (month is None or r.get("月") == month):   # 工作台店铺不再数订单库，免重复
            p = out.setdefault(plat(src), {"花": 0, "收": 0, "单": 0})
            p["收"] += total(r)
            p["单"] += 1
    return out


def roi_text(rs):
    spend, got = sum(p["花"] for p in rs.values()), sum(p["收"] for p in rs.values())
    net = got - spend
    return (f"推广来的私单 {sum(p['单'] for p in rs.values())} 单 ¥{got:g}，花 ¥{spend:g}，"
            + (f"已回本，净赚 ¥{net:g}" if net >= 0 else f"还差 ¥{-net:g} 回本"))


def cost_list(month=None):
    month = month or datetime.date.today().strftime("%Y%m")
    rows = sorted((c for c in all_costs() if c["日期"][:6] == month), key=lambda c: (c["日期"], c["id"]))
    print("编号\t日期\t店铺\t平台\t方式\t金额\t天数\t备注\t写入者")
    plats = {}
    for c in rows:
        print(f"{c['id']}\t{c['日期']}\t{c['店铺']}\t{c['平台']}\t{c.get('方式') or ''}\t{c['金额']:g}\t{days_text(c)}"
              f"\t{c.get('备注') or ''}\t{c.get('写入者') or ''}")
        p = plats.setdefault(c["平台"], {"sum": 0, "shops": {}})
        p["sum"] += c["金额"]
        p["shops"][c["店铺"]] = p["shops"].get(c["店铺"], 0) + c["金额"]
    parts = []
    for name, p in sorted(plats.items(), key=lambda kv: -kv[1]["sum"]):
        sub = "（" + "、".join(f"{s} ¥{v:g}" for s, v in p["shops"].items()) + "）" \
            if len(p["shops"]) > 1 or name not in p["shops"] else ""
        parts.append(f"{name} ¥{p['sum']:g}{sub}")
    print(f"-- {month[:4]}年{int(month[4:])}月私单成本 ¥{sum(c['金额'] for c in rows):g}，{len(rows)} 笔"
          + ("：" + "，".join(parts) if parts else ""))
    by_m = {}
    for c in all_costs():
        by_m[c["日期"][:6]] = by_m.get(c["日期"][:6], 0) + c["金额"]
    if by_m:
        print("-- 按月：" + "，".join(f"{m[:4]}-{m[4:]} ¥{v:g}" for m, v in sorted(by_m.items())))
    rs = roi(month)
    print(f"-- 回本（{int(month[4:])}月）：{roi_text(rs)}")
    for name, p in sorted(rs.items(), key=lambda kv: -kv[1]["花"]):
        print(f"     {name}：花 ¥{p['花']:g}，来了 {p['单']} 单 ¥{p['收']:g}")
    print(f"-- 回本（全部月份）：{roi_text(roi())}")
    cfg = load_cost_cfg()
    for shop in cfg["店铺"]:
        rev = shop_revenue(shop, cfg)
        if rev is not None:
            cut = f"（{rev['起算日'][:4]}-{rev['起算日'][4:6]}-{rev['起算日'][6:]} 起算）" if rev.get("起算日") else ""
            print(f"-- {shop} 工作台快照（账号 {rev.get('账号') or '?'}，采于 {rev.get('快照日') or '?'}）："
                  f"起算后成交 {rev['单']} 单 ¥{rev['收']:g}{cut}；快照内总成交 {rev['总单']} 单 ¥{rev['总收']:g}")
            if rev.get("范围"):
                print(f"     注：{rev['范围']}")


def cmd_cost(a):
    try:
        if a.cc == "add":
            c = parse_cost(kvs(a.kv), load_cost_cfg())
            with Lock():
                c = _new_cost(c, a.写入者)
            print("已记私单成本", c["id"], cost_text(c))
        elif a.cc == "set":
            kv, cfg = kvs(a.kv), load_cost_cfg()
            with Lock():
                m, c = find_cost(a.编号)
                if not c:
                    raise ValueError(f"找不到私单成本 {a.编号}")
                new = parse_cost(kv, cfg, base=c)
                if new["日期"][:6] != m:
                    raise ValueError("跨月改日期：先 cost del 这条，再按新日期 cost add")
                rows = read_costs(m)
                for i, x in enumerate(rows):
                    if x["id"] == a.编号:
                        rows[i] = {**new, "更新": now(), "写入者": a.写入者}
                write_costs(m, rows)
            print("已改私单成本", a.编号, cost_text(new))
        elif a.cc == "del":
            with Lock():
                ok = _mark_cost(a.编号, True, a.写入者)
            print(("已作废 " if ok else "本来就是作废的：") + a.编号)
        else:
            cost_list(a.月)
    except ValueError as e:
        sys.exit(str(e))


# ---------------- 导出结款：对账单（单个工程师）、结款单（按结款工程师） ----------------
# 用户 2026-10-24：先找各个对账工程师对账，再统一找结款工程师结款。名单在 配置/结款.json。


def load_settle_cfg():
    """配置/结款.json：{结款工程师: [归他结款的对账工程师…]}；没有这个文件返回空。"""
    p = HERE / "配置" / "结款.json"
    return json.loads(p.read_text(encoding="utf-8")).get("结款工程师", {}) if p.exists() else {}


def order_state(r):
    """订单状态，口径同看板：0 未开始 1 进行中 2 待结款 3 已结款（结款 X 也算结清）；另返回还欠多少。"""
    env = r.get("环节") or {}
    stages = (CATS.get(r.get("类别")) or {}).get("环节") or list(env)

    def st(v):
        if v is True:
            return "done"
        if v is False:
            return "skip"
        if v in (None, ""):
            return "todo"
        return "pend" if re.search(r"待|未|等|暂", str(v)) else "txt"

    any_done = has_work = False
    work_done = True
    for s in stages:
        x = st(env.get(s))
        if x in ("done", "txt"):
            any_done = True
        if s != "结款":
            has_work = True
            if x in ("todo", "pend"):
                work_done = False
    paid = env.get("结款") in (True, False)
    status = 3 if paid else 2 if (has_work and work_done) else 1 if any_done else 0
    if r.get("价格") is not None:
        tot = r["价格"]
        got = tot if paid else 0
    else:
        tot = (r.get("定金") or 0) + (r.get("尾款") or 0)
        got = tot if paid else (r.get("定金") or 0)   # 定金按接单时已收算
    return status, max(tot - got, 0)


def week_label(day):
    """结款周（周日~周六，周六结款），写成 MMDD-MMDD；同看板。"""
    d = datetime.datetime.strptime(day, "%Y%m%d").date()
    e = d + datetime.timedelta(days=6 - (d.weekday() + 1) % 7)
    return f"{e - datetime.timedelta(days=6):%m%d}-{e:%m%d}"


def amt(v):
    """金额写成 350、350.5，不带千分位，方便直接发微信。"""
    return ("%.2f" % v).rstrip("0").rstrip(".")


def settle_block(items):
    """一个工程师的几行「单号题目 金额」，两单以上再加一行「a+b=总数」。items = [(订单, 欠款)]。"""
    lines = []
    for r, owe in items:
        lines.append(f"{cell_name(r)} {amt(owe)}")
    total_ = sum(o for _, o in items)
    if len(items) > 1:
        lines.append("+".join(amt(o) for _, o in items) + "=" + amt(total_))
    return "\n".join(lines), total_


def settle_text(owner, by_boss, cfg):
    """结款单：结款工程师自己的单在前，归他结款的对账工程师各一段，最后一行合计。"""
    parts, sums = [], []
    for b in [owner] + list(cfg.get(owner) or []):
        if by_boss.get(b):
            text, s = settle_block(by_boss[b])
            parts.append(f"【{b}】\n{text}")
            sums.append(s)
    if len(sums) > 1:
        parts.append("合计 " + "+".join(amt(s) for s in sums) + "=" + amt(sum(sums)))
    return "\n\n".join(parts)


def cmd_settle(a):
    cfg = load_settle_cfg()
    if a.周 and not re.fullmatch(r"\d{4}-\d{4}", a.周):
        sys.exit("结款周写成 MMDD-MMDD，如 1018-1024")
    by_boss = {}
    for r in all_rows():
        boss = r.get("老板")
        if not boss:
            continue   # 自己接的单不找工程师结款
        status, owe = order_state(r)
        if status == 3 or owe <= 0 or (not a.含未完成 and status != 2):
            continue
        if a.周 and (not r.get("日期") or week_label(r["日期"]) != a.周):
            continue
        by_boss.setdefault(boss, []).append((r, owe))
    for v in by_boss.values():   # 按下单日期排，没日期的排最后
        v.sort(key=lambda t: (t[0].get("日期") or "99999999", t[0]["单号"]))
    subs = {b for v in cfg.values() for b in (v or [])}
    name = a.名字
    if a.结清 and (not name or name == "全部"):
        sys.exit("一键结款一次只结一个人：写结款工程师（如 陈工）或单个工程师的名字")
    if name and name != "全部":
        if name in cfg:
            text = settle_text(name, by_boss, cfg)
        else:
            text = settle_block(by_boss[name])[0] if by_boss.get(name) else ""
            if name not in subs and text:
                print(f"注意：{name} 还没归到哪个结款工程师（配置/结款.json）", file=sys.stderr)
        print(text or f"{name} 没有{'没结清' if a.含未完成 else '待结款'}的单")
        if a.结清 and text:   # 用户说钱到账了：上面列出的单全部标成已结款
            bosses = [name] + list(cfg.get(name) or []) if name in cfg else [name]
            items = [(r["类别"], r["单号"]) for b in bosses for r, _ in by_boss.get(b, [])]
            try:
                with Lock():
                    done = _settle_mark(items, a.写入者)
            except ValueError as e:
                sys.exit(str(e))
            print(f"-- 已结款 {len(done)} 单 ¥{amt(sum(d['金额'] for d in done))}："
                  + "、".join(d["单号"] for d in done))
        return
    out = [f"===== {o} 结款 =====\n{settle_text(o, by_boss, cfg)}" for o in cfg
           if any(by_boss.get(b) for b in [o] + list(cfg.get(o) or []))]
    loose = sorted(b for b in by_boss if b not in cfg and b not in subs)
    # 「自己」是自营私单，平台直接到账，标成自营，不算「没归到结款工程师」
    out += [f"===== {b}（{'自营，平台到账' if b == '自己' else '还没归到结款工程师'}）=====\n{settle_block(by_boss[b])[0]}"
            for b in loose]
    print("\n\n".join(out) if out else "没有" + ("没结清" if a.含未完成 else "待结款") + "的单")


def _settle_mark(items, writer):
    """一键结款：把这些单的「结款」标成完成（调用方已加锁）。items = [(类别, 单号)]。
    先全部核对再动手，有一单不对就一单都不改；已经结过（√ 或 X）的跳过。返回改了的 [{类别, 单号, 旧值, 金额}]。"""
    todo = []
    for cat, no in items:
        if cat not in CATS or "结款" not in CATS[cat]["环节"]:
            raise ValueError(f"{cat} 没有「结款」环节")
        m, r = find(cat, no)
        if not r:
            raise ValueError(f"找不到 {cat} 单号 {no}")
        if r["环节"].get("结款") in (True, False):
            continue
        todo.append((cat, no, m, r["环节"].get("结款"), order_state(r)[1]))
    for cat, no, m, old, owe in todo:
        rows = read_month(cat, m)
        for x in rows:
            if x["单号"] == no:
                x["环节"]["结款"] = True
                x["更新"], x["写入者"] = now(), writer
        write_month(cat, m, rows)
    return [{"类别": c, "单号": n, "旧值": o, "金额": w} for c, n, _, o, w in todo]


def manual_settle(items, label):
    """看板上一键结款：一批单一起标成已结款，只记一笔「待确认」；用户说不是本人时整批改回。"""
    load_cfg()
    try:
        with Lock():
            done = _settle_mark(items, MANUAL_WHO)
            if not done:
                return None
            return add_manual({"对象": "结款", "操作": "一键结款",
                               "内容": f"{label} {len(done)} 单 ¥{amt(sum(d['金额'] for d in done))}", "明细": done})
    except SystemExit as ex:   # Lock 超时用的是 sys.exit，看板线程里换成普通错误
        raise RuntimeError(str(ex))


def cmd_import(a):
    """一次性只读导入旧台账 xlsx。"""
    import openpyxl
    ws = openpyxl.load_workbook(a.xlsx, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h).strip() if h is not None else "" for h in rows[0]]
    stages = CATS[a.类别]["环节"]
    added = skipped = 0
    with Lock():
        for line in rows[1:]:
            d = {h: v for h, v in zip(head, line) if h}
            raw = str(d.get("单号") or "").replace("\xa0", " ").strip()
            if not raw:
                continue
            mt = ID_RE.search(raw)
            no = mt.group(0) if mt else raw[:24]
            title = (raw[:mt.start()] + raw[mt.end():]).strip(" ＋+/-　") if mt else raw
            m, ex = find(a.类别, no)
            if ex:
                skipped += 1
                continue
            rec = new_rec(a.类别, no, a.月, "import")
            rec["题目"], rec["渠道"] = title or None, a.渠道
            rec["日期"] = norm(d.get("时间"))
            rec["工程师"] = norm(d.get("工程师"))
            rec["老板"] = norm(d.get("结款工程师"))
            rec["备注"] = norm(d.get("备注"))
            for k in ("价格", "定金", "尾款"):
                rec[k] = money(d.get(k))
            for s in stages:
                rec["环节"][s] = norm(d.get(s))
            if rec["备注"] == "急单":
                rec["急单"] = True
            rs = read_month(a.类别, a.月)
            rs.append(rec)
            write_month(a.类别, a.月, rs)
            added += 1
    print(f"导入 {a.xlsx}：新增 {added}，跳过 {skipped}")


def main():
    p = argparse.ArgumentParser(description="记单平台写入口")
    sp = p.add_subparsers(dest="c", required=True)
    for name, fn in (("add", cmd_add), ("set", cmd_set)):
        q = sp.add_parser(name)
        q.add_argument("类别")
        q.add_argument("单号")
        q.add_argument("kv", nargs="*")
        q.add_argument("--写入者", default="ai")
        q.set_defaults(f=fn)
    q = sp.add_parser("list")
    q.add_argument("--类别")
    q.add_argument("--月")
    q.add_argument("--未结款", action="store_true")
    q.set_defaults(f=cmd_list)
    q = sp.add_parser("export")
    q.add_argument("类别")
    q.add_argument("单号")
    q.add_argument("--表头", action="store_true", help="先打一行列名，方便对照")
    q.set_defaults(f=cmd_export)
    q = sp.add_parser("check")
    q.set_defaults(f=cmd_check)
    q = sp.add_parser("manual", help="列出看板上等用户确认的手动改动")
    q.set_defaults(f=cmd_manual)
    for name, fn in (("confirm", cmd_confirm), ("reject", cmd_reject)):
        q = sp.add_parser(name, help="用户确认是本人改的" if name == "confirm" else "用户说不是本人改的：改回原值")
        q.add_argument("编号", nargs="+", help="manual 列出的编号，或 all")
        q.add_argument("--写入者", default="ai")
        q.set_defaults(f=fn)
    q = sp.add_parser("settle", help="导出结款：名字是结款工程师出结款单，是对账工程师出对账单，不写出全部")
    q.add_argument("名字", nargs="?", help="如 陈工（结款单）、郑工（对账单）；不写或写 全部 = 每个结款工程师一份")
    q.add_argument("--含未完成", action="store_true", help="连还没干完的单也列上（默认只列待结款）")
    q.add_argument("--周", help="只看某个结款周，写成 MMDD-MMDD")
    q.add_argument("--结清", action="store_true", help="用户说钱到账了：把列出的单全部标成已结款（一次只结一个人）")
    q.add_argument("--写入者", default="ai")
    q.set_defaults(f=cmd_settle)
    q = sp.add_parser("cost", help="私单成本（推广费）：add / list / set / del")
    cs = q.add_subparsers(dest="cc", required=True)
    x = cs.add_parser("add", help="记一笔：店铺= 金额= [方式=] [天数=] [日期=] [备注=]")
    x.add_argument("kv", nargs="*")
    x.add_argument("--写入者", default="ai")
    x = cs.add_parser("list", help="某月明细和按平台、按月汇总（默认本月）")
    x.add_argument("--月")
    x = cs.add_parser("set", help="改一笔（不能跨月改日期）")
    x.add_argument("编号")
    x.add_argument("kv", nargs="*")
    x.add_argument("--写入者", default="ai")
    x = cs.add_parser("del", help="作废一笔（留底，不再算钱）")
    x.add_argument("编号")
    x.add_argument("--写入者", default="ai")
    q.set_defaults(f=cmd_cost)
    q = sp.add_parser("import")
    q.add_argument("xlsx")
    q.add_argument("--类别", required=True)
    q.add_argument("--渠道", default="中介")
    q.add_argument("--月", required=True)
    q.set_defaults(f=cmd_import)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
