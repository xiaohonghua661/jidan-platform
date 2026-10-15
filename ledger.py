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
TOP = ["题目", "渠道", "日期", "期限", "价格", "定金", "尾款", "工程师", "老板", "急单", "备注", "月"]
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


def write_month(cat, month, rows):
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
        elif k in TOP:
            rec[k] = v if v != "" else None
        else:
            sys.exit(f"未知字段 {k}；可用：{TOP + CATS[cat]['环节']}")


def new_rec(cat, no, month, writer):
    rows = read_month(cat, month)
    return {"id": f"{cat}-{month}-{len(rows) + 1:04d}", "单号": no, "类别": cat, "月": month,
            "题目": None, "渠道": "中介", "日期": None, "期限": None, "价格": None, "定金": None, "尾款": None,
            "工程师": None, "老板": None, "环节": {s: None for s in CATS[cat]["环节"]},
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


def cmd_add(a):
    kv = kvs(a.kv)
    if a.类别 not in CATS:
        sys.exit(f"未知类别 {a.类别}，可用 {list(CATS)}")
    kv.setdefault("渠道", "中介")
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
                apply(x, a.类别, kv)
                x["更新"] = now()
                x["写入者"] = a.写入者
        write_month(a.类别, m, rows)
    print("已更新", a.单号, kv)


def total(r):
    return r["价格"] if r["价格"] is not None else (r["定金"] or 0) + (r["尾款"] or 0)


def cmd_list(a):
    n = 0
    print("类别\t月\t单号\t渠道\t老板\t金额\t环节（√完成 X不做 ·未做）\t题目\t期限\t备注")
    for r in all_rows():
        if a.类别 and r["类别"] != a.类别:
            continue
        if a.月 and r["月"] != a.月:
            continue
        if a.未结款 and r["环节"].get("结款") is True:
            continue
        n += 1
        st = " ".join(f"{k}:{'√' if v is True else 'X' if v is False else '·' if v is None else v}"
                      for k, v in r["环节"].items())
        print(f"{r['类别']}\t{r['月']}\t{r['单号']}\t{r['渠道']}\t{r.get('老板') or '自己'}\t{total(r):g}\t{st}\t{r['题目'] or ''}"
              f"\t{r.get('期限') or ''}\t{r.get('备注') or ''}")
    print(f"-- 共 {n} 条")


def cmd_export(a):
    """按该渠道台账的列序出一行文本，给用户自己填进 Excel。"""
    m, r = find(a.类别, a.单号)
    if not r:
        sys.exit("找不到该单")

    def c(v):
        return "√" if v is True else "X" if v is False else ("" if v is None else str(v))

    def yuan(v):
        return "" if v is None else f"{v:g}"

    # 单号格子 = 单号紧接题目，不加空格（用户给的写法，如 A-26-066基于stm32的智能家居系统设计）
    no = r["单号"] if not r.get("题目") or r["题目"] == r["单号"] else f"{r['单号']}{r['题目']}"
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
        seen.add(k)
        if r.get("渠道") not in CHANNELS:
            print("未知渠道", k, r.get("渠道"))
            bad += 1
        if "客户" in r or "老板" not in r:
            print("缺 老板 字段（旧名 客户）", k)
            bad += 1
        extra = set(r.get("环节", {})) - set(CATS[r["类别"]]["环节"])
        if extra:
            print("环节不在配置里", k, sorted(extra))
            bad += 1
        for f in ("日期", "期限"):
            if r.get(f) and not re.fullmatch(r"\d{8}", str(r[f])):
                print(f + "格式不对", k, r[f])
                bad += 1
    print("检查完成，问题", bad)
    n = len(pending_manual())
    if n:
        print(f"另有看板手动改动 {n} 处等用户确认（ledger.py manual 查看）")
    if bad:
        sys.exit(1)   # 查出问题时退出码非 0，脚本和 AI 能据此判断


# ---------------- 看板手动改动：用户在看板上改的东西，AI 要在对话里跟用户确认 ----------------
MANUAL_LOG = HERE / "手动改动.jsonl"
MANUAL_WHO = "看板手动"
VALUE_NAME = {"完成": True, "不做": False, "未做": None}
EDIT_FIELDS = ("备注", "期限", "价格", "定金", "尾款")   # 看板上能改的单子字段


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
            rows = read_month(cat, m)
            for x in rows:
                if x["单号"] == no:
                    if field:
                        x[field] = new
                    else:
                        x["环节"][stage] = new
                    x["更新"], x["写入者"] = now(), MANUAL_WHO
            write_month(cat, m, rows)
            items = read_manual()
            e = {"编号": max((i.get("编号", 0) for i in items), default=0) + 1, "时间": now(),
                 "来源": MANUAL_WHO, "类别": cat, "单号": no, ("字段" if field else "环节"): field or stage,
                 "旧值": old, "新值": new, "状态": "待确认"}
            items.append(e)
            write_manual(items)
            return e
    except SystemExit as ex:   # Lock 超时用的是 sys.exit，看板线程里换成普通错误
        raise RuntimeError(str(ex))


def cmd_manual(a):
    items = pending_manual()
    if not items:
        print("没有待确认的手动改动")
        return
    print(f"看板上有 {len(items)} 处手动改动，要在对话里问用户是不是本人操作的：")
    for i in items:
        print(f"  #{i['编号']}  {i['时间'][5:16].replace('T', ' ')}  {i['类别']} {i['单号']}  "
              f"{entry_name(i)}：{entry_show(i, i['旧值'])} → {entry_show(i, i['新值'])}")
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
            if revert:
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
            done.append(f"#{i['编号']} {i['单号']} {entry_name(i)}{note}")
        write_manual(items)
    print(("已改回：" if revert else "已确认：") + ("；".join(done) if done else "没有匹配的待确认编号"))


def cmd_confirm(a):
    resolve_manual(a.编号, False, a.写入者)


def cmd_reject(a):
    resolve_manual(a.编号, True, a.写入者)


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
