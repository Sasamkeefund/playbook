#!/usr/bin/env python3
"""
Paper Trade 引擎 — 每日 scan 後自動行，持倉 + 戰績存 Google Sheet（零本地痕跡）
  S1 組   ：人手揀股（dashboard 加入），程式跟 T1/T2/止損
  TV 組   ：我的記錄（TradingView 真實 paper 單），只做 T1 提示，唔自動平倉
  S1A0/S1A1：S1 自動記錄（止蝕 0% vs 1% 緩衝）
  S7E20/S7S10：S7 突破自動記錄（20MA vs 10MA 止賺）
  S6A/S6C：S6 旗形突破自動記錄（手法A 放量 Limit @ 收市 vs 手法C Limit @ H1 等回測）
  S3A/S3B/S3C：S3 突破交易自動記錄（手法A / B / C）
  S5R/S5S：S5 支持阻力自動記錄（跟 checklist 4/4 vs 淨結構）
  （舊版 S7 A/B/C/D 已於 2026-10-04 移除：冇等突破就買，4 組全部負期望）
"""
import sys, json, datetime, urllib.request, urllib.parse
sys.path.insert(0, ".")
import scan

WL_API = "https://script.google.com/macros/s/AKfycbw-taBatpcuHXt5daIGq3Bo7lGw9OvdtsNeg292qdN0eW4RyxWLV4qf-oACvaVHHUI-Bg/exec"

def gv_get(action):
    url = WL_API + "?" + urllib.parse.urlencode({"action": action})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("GET fail", e); return {}

def gv_post(payload):
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(WL_API, data=data,
              headers={"Content-Type": "text/plain;charset=utf-8"}, method="POST")
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print("POST fail", e)

def today_str():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
           "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

def _norm_date(s):
    """將日期 string 轉成 (year, month, day)。處理兩種格式：
    '2026-06-24' 同 'Wed Jun 24 2026 00:00:00 GMT+0800'。"""
    s = str(s).strip()
    if not s:
        return None
    # ISO 格式 2026-06-24
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return (int(s[0:4]), int(s[5:7]), int(s[8:10]))
        except ValueError:
            pass
    # JS Date 格式 'Wed Jun 24 2026 ...'
    parts = s.split()
    if len(parts) >= 4 and parts[1] in _MONTHS:
        try:
            return (int(parts[3]), _MONTHS[parts[1]], int(parts[2]))
        except (ValueError, KeyError):
            pass
    return None

def _same_day(a, b):
    na, nb = _norm_date(a), _norm_date(b)
    return na is not None and na == nb

def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None

# ════════════════════════════════════════════════════════════════
# S1 自動記錄（跟 Sasa 平時 S1 做法；2026-09-30 定、2026-10-04 修訂）
#   訊號：S1 ready + dashboard 🟢/🔵（收市跌穿 EMA20，3 日內收市企返上；跌穿前要連續 ready ≥ 3 日）
#   掛單：訊號日收市後放 Limit = 訊號日收市價（入場價就係呢個價）
#   開市前 Check（跟 S1_開市前Check）：第二個交易日 —
#         開市價 < EMA20 → 取消；裂口低開 > 1% → 取消；裂口高開 > 1% → 取消（唔追）；
#         全日最低都未跌到 Limit → 冇成交。入場日 = 成交日。
#   止蝕：前底 = 回調期間 K 線實體最低（唔計影線）；S1A0 冇緩衝、S1A1 再低 1%
#         收市價低過止蝕先算（唔睇下影線），用嗰日收市價平倉
#   T1  ：前頂 = 跌穿前 20 個交易日 K 線實體最高；用盤中最高價判斷（Limit 賣單掂到就成交）
#   R:R→T1 ≥ 2 → 到 T1 全平；< 2 → T2 = T1 + (T1−止蝕)×1.618，T1 平一半、T2 平另一半；連 T2 都 < 2 → 唔入
#   到 T1 後餘下一半止蝕移去入場同 T1 中間（同樣收市確認）
#   每次 run 由入場日逐日重新計（唔怕 scan 漏咗日子）；同一隻股同一組 15 日內只入一次
# ════════════════════════════════════════════════════════════════
S1_AUTO_GROUPS = [("S1A0", 1.00, "0%緩衝"), ("S1A1", 0.99, "1%緩衝")]
S1_AUTO_COOLDOWN_DAYS = 15
S1_T1_LOOKBACK = 20

def s1_plan(entry, stop, t1):
    """返回 (t2 或 None, 計劃文字) ；唔入就返回 None。"""
    risk = entry - stop
    if risk <= 0 or t1 <= entry:
        return None
    rr1 = (t1 - entry) / risk
    if rr1 >= 2:
        return (None, "T1全平", rr1, None)
    t2 = t1 + (t1 - stop) * 1.618
    rr2 = (t2 - entry) / risk
    if rr2 < 2:
        return None
    return (t2, "T1半+T2半", rr1, rr2)

def session_date(charts):
    """charts.json 最後一條 bar 嘅日期（美股交易日），取最多隻股一致嗰個。"""
    from collections import Counter
    cnt = Counter()
    for i, ch in enumerate(charts.values()):
        t = ch.get("t") or []
        if t:
            cnt[datetime.datetime.utcfromtimestamp(t[-1]).strftime("%Y-%m-%d")] += 1
        if i >= 200:
            break
    return cnt.most_common(1)[0][0] if cnt else None

def _days_between(a, b):
    na, nb = _norm_date(a), _norm_date(b)
    if not na or not nb:
        return None
    return abs((datetime.date(*na) - datetime.date(*nb)).days)

def _bar_dates(ch):
    return [datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d") for t in (ch.get("t") or [])]

def _stop_hit(ch, k, k0, eff):
    """Stop 止損單（2026-10-07 起，跟 Sasa 實際做法）：盤中最低掂到止蝕價就成交；
    開市已經低過止蝕價（裂口）就用開市價成交。入場日用止蝕價（成交之後先跌落嚟）。冇掂到返回 None。"""
    l, o = ch["l"], ch["o"]
    if k >= len(l) or l[k] is None or l[k] > eff:
        return None
    if k > k0 and o[k] is not None and o[k] < eff:
        return o[k]
    return eff

def simulate_s1(ch, k0, entry, stop, t1, t2):
    """由入場日 k0 開始逐日行。止蝕用 Stop 單：盤中掂到止蝕價就止蝕（裂口低開用開市價）；
    同一日又掂止蝕又掂目標 → 當止蝕先（保守）。入場日唔計目標（唔知成交前定後先掂到）。
    到 T1 之後，餘下止蝕移去入場同 T1 中間，第二日先生效（你第二朝先改得張止蝕單）。
    返回 {"exit": (k, px, reason, r) 或 None, "t1hit": bool}"""
    c, h, o = ch["c"], ch["h"], ch["o"]
    risk = entry - stop
    R = lambda px: (px - entry) / risk
    t1hit, eff, move = False, stop, None
    for k in range(k0, len(c)):
        if c[k] is None or h[k] is None:
            continue
        if move and k >= move[0]:
            eff, move = move[1], None
        px = _stop_hit(ch, k, k0, eff)
        if px is not None:
            if t1hit:
                return {"exit": (k, px, "止蝕(T1平一半後，餘下盤中跌穿中間位)", 0.5 * R(t1) + 0.5 * R(px)), "t1hit": True}
            gap = k > k0 and o[k] is not None and o[k] < eff
            why = ("止蝕(入場當日盤中跌穿前底)" if k == k0 else
                   "止蝕(裂口低開穿前底，開市價平倉)" if gap else "止蝕(盤中跌穿前底)")
            return {"exit": (k, px, why, R(px)), "t1hit": False}
        if k > k0:
            if t2 is None:
                if h[k] >= t1:
                    return {"exit": (k, t1, "止賺(到T1全平)", R(t1)), "t1hit": True}
            else:
                if not t1hit and h[k] >= t1:
                    t1hit, move = True, (k + 1, entry + (t1 - entry) * 0.5)
                if t1hit and h[k] >= t2:
                    return {"exit": (k, t2, "止賺(T1平一半+T2平一半)", 0.5 * R(t1) + 0.5 * R(t2)), "t1hit": True}
    return {"exit": None, "t1hit": t1hit}

def _post_s1_result(tk, grp, ch, entry, stop, res, already_t1hit=False):
    dates = _bar_dates(ch)
    if res["exit"]:
        k, px, reason, r = res["exit"]
        pct = r * (entry - stop) / entry * 100
        gv_post({"action": "paper_close", "ticker": tk, "group": grp, "exitDate": dates[k],
                 "exitPx": round(px, 2), "reason": reason, "r": round(r, 2), "pct": round(pct, 1)})
        print(f"平倉 [{grp}] {tk} {dates[k]}: {reason} R={r:.2f}")
    elif res["t1hit"] and not already_t1hit:
        gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
        print(f"📍 [{grp}] {tk}: 到T1（平一半，餘下止蝕移去入場/T1中間）")

def manage_s1_auto(p, charts):
    """由入場日重新逐日計一次，揾出有冇止賺 / 止蝕 / 到 T1（唔怕中間漏咗 scan）。"""
    tk, grp = p["ticker"], p["group"]
    entry, stop = _num(p.get("entry")), _num(p.get("stop"))
    t1, t2 = _num(p.get("t1")), _num(p.get("t2"))
    ch = charts.get(tk) or {}
    if entry is None or stop is None or t1 is None or entry <= stop or not ch.get("c"):
        return
    nd = _norm_date(str(p.get("entryDate", "")))
    if not nd:
        return
    ed = "%04d-%02d-%02d" % nd
    dates = _bar_dates(ch)
    if ed not in dates:
        return
    res = simulate_s1(ch, dates.index(ed), entry, stop, t1, t2)
    _post_s1_result(tk, grp, ch, entry, stop, res,
                    already_t1hit=str(p.get("t1hit", "")).upper() == "Y")

S1_ENTRY_MARK = "開市Check"

def cleanup_old_s1_auto(open_pos, closed):
    """一次性：刪走舊規則嘅 S1 自動記錄（冇「開市Check」標記），由新規則重新開始。"""
    groups = [a for a, _, _ in S1_AUTO_GROUPS]
    n = 0
    for lst, status in ((open_pos, "open"), (closed, "closed")):
        for x in list(lst):
            if x.get("group") in groups and S1_ENTRY_MARK not in str(x.get("state", "")):
                gv_post({"action": "paper_remove", "ticker": x["ticker"], "group": x["group"],
                         "entry": x.get("entry"), "status": status})
                lst.remove(x); n += 1
    if n:
        print(f"S1 自動：刪走 {n} 張舊規則記錄，改用新規則（開市前 Check / 實體 / 收市止蝕）")

def open_s1_auto(data, stocks, charts, open_pos, closed, session):
    """上個交易日嘅 🟢🔵 訊號（scan.py 寫嘅 s1Pending）→ 下一個交易日做開市前 Check，跌到 Limit 先成交。"""
    groups = [a for a, _, _ in S1_AUTO_GROUPS]
    recent, held = {}, set()
    for x in list(open_pos) + list(closed):
        if x.get("group") in groups:
            recent.setdefault((x["ticker"], x["group"]), []).append(str(x.get("entryDate", "")))
    for x in open_pos:
        if x.get("group") in groups:
            held.add((x["ticker"], x["group"]))
    pend = data.get("s1Pending") or []
    n = cancel = 0
    for pd in pend:
        tk, sig = pd["ticker"], pd.get("signalDate")
        ch = charts.get(tk) or {}
        if not sig or not ch.get("c") or pd.get("ema20") is None:
            continue
        dates = _bar_dates(ch)
        nxt = [k for k, d in enumerate(dates) if d > sig]
        if not nxt:
            continue                      # 下一個交易日嘅 bar 未有
        k = nxt[0]
        opn, low = ch["o"][k], ch["l"][k]
        limit, ema = pd["limit"], pd["ema20"]
        gap = (opn - limit) / limit * 100
        why = None
        if opn < ema:
            why = f"開市 {opn:.2f} 低過 EMA20 {ema:.2f}"
        elif gap < -1:
            why = f"裂口低開 {gap:.1f}%"
        elif gap > 1:
            why = f"裂口高開 +{gap:.1f}%（唔追）"
        elif low > limit:
            why = f"全日最低 {low:.2f} 未跌到 Limit {limit:.2f}"
        if why:
            cancel += 1
            print(f"  S1 掛單取消 {tk}（訊號 {sig}，{dates[k]}）：{why}")
            continue
        fill_date, entry = dates[k], limit
        for grp, buf, buf_txt in S1_AUTO_GROUPS:
            gaps = [_days_between(d, fill_date) for d in recent.get((tk, grp), [])]
            if (tk, grp) in held or any(g is not None and g <= S1_AUTO_COOLDOWN_DAYS for g in gaps):
                continue
            stop = pd["low"] * buf; t1 = pd["t1"]
            plan = s1_plan(entry, stop, t1)
            if not plan:
                print(f"  S1自動 skip {tk} [{grp}]：R:R 唔夠 或 前頂唔高過入場價")
                continue
            t2, plan_txt, rr1, rr2 = plan
            dot = "🟢" if pd.get("color") == "green" else "🔵"
            gv_post({"action": "paper_open", "ticker": tk, "group": grp,
                     "state": f"S1自動 {dot} | {buf_txt} | {plan_txt} | {pd.get('universe', '')} | "
                              f"Limit成交(訊號{sig[5:]}) {S1_ENTRY_MARK}✓ | 跌穿{pd.get('depth', '')}%",
                     "entryDate": fill_date, "entry": round(entry, 2), "stop": round(stop, 2),
                     "t1": round(t1, 2), "t2": round(t2, 2) if t2 else "",
                     "bonus": pd.get("bonus"), "rsi": pd.get("rsi"), "trend": pd.get("trend"),
                     "pullback": pd.get("pullback"), "spy1m": "", "m1": ""})
            recent.setdefault((tk, grp), []).append(fill_date)
            held.add((tk, grp))
            n += 1
            print(f"成交 [{grp}] {tk} {dot}: 訊號{sig} → {fill_date} Limit {entry:.2f} "
                  f"止蝕 {stop:.2f} T1 {t1:.2f}" + (f" T2 {t2:.2f}" if t2 else "") + f" → {plan_txt}")
            # 成交之後（包括成交當日收市）已經有嘅 bar 即刻逐日計
            _post_s1_result(tk, grp, ch, entry, stop, simulate_s1(ch, k, entry, stop, t1, t2))
    print(f"S1 自動記錄：掛單 {len(pend)} 張，成交開 {n} 單，取消/冇成交 {cancel} 張")

# ════════════════════════════════════════════════════════════════
# S7 突破（2026-10-04 重做；舊版 A/B/C/D 冇等突破就買，已停）
#   訊號：前一日 S7 ready（VCP 整固）→ 今日第一次收市升穿整固區最高收市（pivot）+ RelVol ≥ 1.5；
#         大市 SPY > EMA200；ATR% ≥ 1%（排除優先股 / 等收購嘅股）
#   入場：下一個交易日開市價買；開市 < pivot（突破失敗）或 > pivot × 1.05（追高）→ 取消
#   止蝕：突破前最後 10 日最低收市，收市低過先算；風險 > 8% 唔入
#   止賺：兩組對比 —— S7E20 收市跌穿 EMA20、S7S10 收市跌穿 10 日均線；
#         要曾經升過 入場 + 0.5×ATR 先開始用均線止賺（未賺過錢就只睇止蝕）
#   同一隻股同一組 30 日內只入一次；每次 run 由入場日逐日重算
# ════════════════════════════════════════════════════════════════
S7N_GROUPS = [("S7E20", "E20", "20MA止賺"), ("S7S10", "S10", "10MA止賺")]
S7N_COOLDOWN_DAYS = 30
S7N_MAX_RISK_PCT = 8.0
S7N_MAX_CHASE_PCT = 5.0

def _atr14_before(ch, k):
    h, l, c = ch["h"], ch["l"], ch["c"]
    trs = []
    for j in range(max(1, k - 14), k):
        if None in (h[j], l[j], c[j - 1]):
            continue
        trs.append(max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1])))
    return sum(trs) / len(trs) if trs else None

def simulate_s7(ch, k0, entry, stop, trail, buffer_met=False):
    """由入場日 k0（開市買入）逐日行：盤中掂到止蝕價（Stop 單）→ 止蝕；
    升過 entry+0.5ATR 之後收市跌穿均線 → 平倉（均線止賺仍然係睇收市）。"""
    c, e20, o = ch["c"], ch.get("e20") or [], ch["o"]
    risk = entry - stop
    atr = _atr14_before(ch, k0) if k0 > 0 else None
    buf_px = entry + 0.5 * atr if atr else entry
    mx = None
    for k in range(k0, len(c)):
        ck = c[k]
        if ck is None:
            continue
        px = _stop_hit(ch, k, k0, stop)
        if px is not None:
            gap = k > k0 and o[k] is not None and o[k] < stop
            why = ("止蝕(入場當日盤中跌穿整固低位)" if k == k0 else
                   "止蝕(裂口低開穿整固低位，開市價平倉)" if gap else "止蝕(盤中跌穿整固低位)")
            return {"exit": (k, px, why, (px - entry) / risk)}
        mx = ck if mx is None else max(mx, ck)
        if k > k0 and (buffer_met or mx >= buf_px):
            if trail == "E20":
                ma, name = (e20[k] if k < len(e20) else None), "20MA"
            else:
                win = [x for x in c[k - 9:k + 1] if x is not None] if k >= 9 else []
                ma, name = (sum(win) / 10 if len(win) == 10 else None), "10MA"
            if ma is not None and ck < ma:
                why = f"止賺(收市跌穿{name})" if ck > entry else f"止蝕(升過之後打返轉，收市跌穿{name})"
                return {"exit": (k, ck, why, (ck - entry) / risk)}
    return {"exit": None}

def _post_s7_close(tk, grp, ch, entry, stop, res):
    if not res["exit"]:
        return
    k, px, reason, r = res["exit"]
    d = _bar_dates(ch)[k]
    gv_post({"action": "paper_close", "ticker": tk, "group": grp, "exitDate": d, "exitPx": round(px, 2),
             "reason": reason, "r": round(r, 2), "pct": round((px - entry) / entry * 100, 1)})
    print(f"平倉 [{grp}] {tk} {d}: {reason} R={r:.2f}")

def manage_s7_auto(p, charts):
    tk, grp = p["ticker"], p["group"]
    entry, stop = _num(p.get("entry")), _num(p.get("stop"))
    ch = charts.get(tk) or {}
    if entry is None or stop is None or entry <= stop or not ch.get("c"):
        return
    nd = _norm_date(str(p.get("entryDate", "")))
    if not nd:
        return
    ed = "%04d-%02d-%02d" % nd
    dates = _bar_dates(ch)
    trail = dict((g, t) for g, t, _ in S7N_GROUPS).get(grp, "E20")
    if ed in dates:
        res = simulate_s7(ch, dates.index(ed), entry, stop, trail)
    elif dates and ed < dates[0]:
        res = simulate_s7(ch, 0, entry, stop, trail, buffer_met=True)   # 揸咗好耐，入場日已經唔喺圖表入面
    else:
        return
    _post_s7_close(tk, grp, ch, entry, stop, res)

def open_s7_auto(data, charts, open_pos, closed):
    groups = [g for g, _, _ in S7N_GROUPS]
    recent, held = {}, set()
    for x in list(open_pos) + list(closed):
        if x.get("group") in groups:
            recent.setdefault((x["ticker"], x["group"]), []).append(str(x.get("entryDate", "")))
    for x in open_pos:
        if x.get("group") in groups:
            held.add((x["ticker"], x["group"]))
    pend = data.get("s7Pending") or []
    n = cancel = 0
    for pd in pend:
        tk, sig, pivot, stop = pd["ticker"], pd.get("signalDate"), pd.get("pivot"), pd.get("stopRef")
        ch = charts.get(tk) or {}
        if not sig or not pivot or not stop or not ch.get("c"):
            continue
        dates = _bar_dates(ch)
        nxt = [k for k, d in enumerate(dates) if d > sig]
        if not nxt:
            continue
        k = nxt[0]
        opn = ch["o"][k]
        why = None
        if opn < pivot:
            why = f"開市 {opn:.2f} 跌返落突破位 {pivot:.2f} 以下（突破失敗）"
        elif opn > pivot * (1 + S7N_MAX_CHASE_PCT / 100):
            why = f"開市 {opn:.2f} 已經高過突破位 {(opn / pivot - 1) * 100:.1f}%（唔追）"
        elif (opn - stop) / opn * 100 > S7N_MAX_RISK_PCT:
            why = f"止蝕太闊（{(opn - stop) / opn * 100:.1f}% > {S7N_MAX_RISK_PCT:.0f}%）"
        if why:
            cancel += 1
            print(f"  S7 取消 {tk}（突破 {sig}，{dates[k]}）：{why}")
            continue
        entry = opn
        for grp, trail, label in S7N_GROUPS:
            gaps = [_days_between(d, dates[k]) for d in recent.get((tk, grp), [])]
            if (tk, grp) in held or any(g is not None and g <= S7N_COOLDOWN_DAYS for g in gaps):
                continue
            gv_post({"action": "paper_open", "ticker": tk, "group": grp,
                     "state": f"S7突破 | {label} | 突破位 {pivot:.2f} | RVOL {pd.get('brkRvol')} | "
                              f"{pd.get('universe', '')} | 大市{pd.get('regime', '')} | 突破日 {sig[5:]}",
                     "entryDate": dates[k], "entry": round(entry, 2), "stop": round(stop, 2),
                     "bonus": pd.get("bonus"), "spy1m": "", "m1": ""})
            recent.setdefault((tk, grp), []).append(dates[k]); held.add((tk, grp)); n += 1
            print(f"開倉 [{grp}] {tk}: 突破 {sig} → {dates[k]} 開市 {entry:.2f} 止蝕 {stop:.2f}（{label}）")
            _post_s7_close(tk, grp, ch, entry, stop, simulate_s7(ch, k, entry, stop, trail))
    print(f"S7 突破記錄：掛單 {len(pend)} 張，開 {n} 單，取消 {cancel} 張")

# ════════════════════════════════════════════════════════════════
# S6 旗形自動記錄（2026-10-05，跟 S6 筆記 v4 + checklist；突破 / 掛單規則喺 scan.py s6_breakouts）
#   S6A = 手法A（突破日 RV > 1.5）：Limit @ 突破日收市；第二日高開 > 1% → 取消；跌到 Limit 先成交
#   S6C = 手法C（RV ≤ 1.5）     ：Limit @ H1 等回測；最多等 20 個交易日
#   止蝕：旗形最低收市 × 0.98，收市低過先算，用收市價平倉
#   T1 = 旗形最低 + 旗杆 × 0.618（盤中掂到）→ 平一半，餘下止蝕移去入場價；T2 = 旗形最低 + 旗杆 → 平餘下
#   每次 run 由入場日逐日重算；同一個突破日只記一次
# ════════════════════════════════════════════════════════════════
S6_GROUPS = {"A": ("S6A", "手法A"), "C": ("S6C", "手法C")}
S6_SIG_MARK = "突破日"

def simulate_t12(ch, k0, entry, stop, t1, t2, stop_txt):
    """由入場日 k0 開始逐日行。止蝕用 Stop 單：盤中掂到止蝕價就止蝕（裂口低開用開市價）；
    同一日又掂止蝕又掂目標 → 當止蝕先（保守）。入場日唔計目標。
    T1（盤中掂到）平一半，餘下止蝕移去入場價（第二日先生效）；T2 平餘下。t1 = None → 全倉等 T2。"""
    c, h, o = ch["c"], ch["h"], ch["o"]
    risk = entry - stop
    R = lambda px: (px - entry) / risk
    t1hit, eff, move = False, stop, None
    for k in range(k0, len(c)):
        if c[k] is None or h[k] is None:
            continue
        if move and k >= move[0]:
            eff, move = move[1], None
        px = _stop_hit(ch, k, k0, eff)
        if px is not None:
            if t1hit:
                return {"exit": (k, px, "保本(T1平一半後，餘下盤中跌穿入場價)", 0.5 * R(t1) + 0.5 * R(px)),
                        "t1hit": True}
            gap = k > k0 and o[k] is not None and o[k] < eff
            why = (f"止蝕(入場當日盤中跌穿{stop_txt})" if k == k0 else
                   f"止蝕(裂口低開穿{stop_txt}，開市價平倉)" if gap else f"止蝕(盤中跌穿{stop_txt})")
            return {"exit": (k, px, why, R(px)), "t1hit": False}
        if k > k0:
            if t1 is not None and not t1hit and h[k] >= t1:
                t1hit, move = True, (k + 1, entry)
            if h[k] >= t2:
                if t1 is None:
                    return {"exit": (k, t2, "止賺(全倉到T2)", R(t2)), "t1hit": False}
                if t1hit:
                    return {"exit": (k, t2, "止賺(T1平一半+T2平一半)", 0.5 * R(t1) + 0.5 * R(t2)), "t1hit": True}
    return {"exit": None, "t1hit": t1hit}

def simulate_s6(ch, k0, entry, stop, t1, t2):
    return simulate_t12(ch, k0, entry, stop, t1, t2, "旗形低×0.98")

def _post_s6_result(tk, grp, ch, entry, stop, res, already_t1hit=False):
    dates = _bar_dates(ch)
    if res["exit"]:
        k, px, reason, r = res["exit"]
        gv_post({"action": "paper_close", "ticker": tk, "group": grp, "exitDate": dates[k],
                 "exitPx": round(px, 2), "reason": reason, "r": round(r, 2),
                 "pct": round(r * (entry - stop) / entry * 100, 1)})
        print(f"平倉 [{grp}] {tk} {dates[k]}: {reason} R={r:.2f}")
    elif res["t1hit"] and not already_t1hit:
        gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
        print(f"📍 [{grp}] {tk}: 到T1（平一半，餘下止蝕移去入場價）")

def manage_s6_auto(p, charts):
    tk, grp = p["ticker"], p["group"]
    entry, stop = _num(p.get("entry")), _num(p.get("stop"))
    t1, t2 = _num(p.get("t1")), _num(p.get("t2"))
    ch = charts.get(tk) or {}
    if None in (entry, stop, t1, t2) or entry <= stop or not ch.get("c"):
        return
    nd = _norm_date(str(p.get("entryDate", "")))
    if not nd:
        return
    ed = "%04d-%02d-%02d" % nd
    dates = _bar_dates(ch)
    if ed not in dates:
        return
    res = simulate_s6(ch, dates.index(ed), entry, stop, t1, t2)
    _post_s6_result(tk, grp, ch, entry, stop, res,
                    already_t1hit=str(p.get("t1hit", "")).upper() == "Y")

def _s6_sig_date(state):
    """由 state 文字攞返突破日（防止同一個突破記兩次）。"""
    s = str(state or "")
    i = s.find(S6_SIG_MARK)
    return s[i + len(S6_SIG_MARK):].strip()[:10] if i >= 0 else None

def _iso(d):
    nd = _norm_date(str(d or ""))
    return "%04d-%02d-%02d" % nd if nd else None

def _hold_intervals(open_pos, closed, groups):
    """每隻股每組嘅持倉期間 [入場日, 平倉日]（未平倉 = None）。用嚟判斷某日係咪已經揸緊。"""
    iv = {}
    for x in open_pos:
        if x.get("group") in groups:
            iv.setdefault((x["ticker"], x["group"]), []).append((_iso(x.get("entryDate")), None))
    for x in closed:
        if x.get("group") in groups:
            iv.setdefault((x["ticker"], x["group"]), []).append((_iso(x.get("entryDate")), _iso(x.get("exitDate"))))
    return iv

def _busy(iv, key, day):
    """day 嗰日同一組係咪已經揸緊呢隻股（補跑舊日子都會得出同每日行一樣嘅結果）。"""
    return any(a and a <= day and (b is None or day <= b) for a, b in iv.get(key, []))

def _exit_day(ch, res):
    return _bar_dates(ch)[res["exit"][0]] if res.get("exit") else None

def open_s6_auto(data, charts, open_pos, closed):
    groups = [g for g, _ in S6_GROUPS.values()]
    done = set()
    for x in list(open_pos) + list(closed):
        if x.get("group") in groups:
            done.add((x["ticker"], x["group"], _s6_sig_date(x.get("state"))))
    iv = _hold_intervals(open_pos, closed, groups)
    sigs = sorted(data.get("s6Signals") or [], key=lambda x: x["signalDate"])
    n = wait = cancel = skip = 0
    for sg in sigs:
        tk, sig, m = sg["ticker"], sg["signalDate"], sg["method"]
        grp, label = S6_GROUPS[m]
        if sig < AUTO_START:
            continue
        if sg.get("skip"):
            skip += 1
            continue
        if (tk, grp, sig) in done:
            continue
        ch = charts.get(tk) or {}
        dates = _bar_dates(ch)
        if not ch.get("c") or sig not in dates:
            continue
        st = scan.s6_order_status(m, sg["limit"], sg["stop"], dates.index(sig), ch["o"], ch["l"], ch["c"])
        if st["status"] == "wait":
            wait += 1
            continue
        if st["status"] == "cancel":
            cancel += 1
            continue
        k, entry = st["k"], st["px"]
        if _busy(iv, (tk, grp), dates[k]):
            print(f"  S6 {tk} [{grp}]：突破 {sig} 喺 {dates[k]} 成交，但嗰日已經揸緊同一組，唔再入")
            continue
        stop, t1, t2 = sg["stop"], sg["t1"], sg["t2"]
        waited = f" 等{st.get('waited')}日" if m == "C" else ""
        gv_post({"action": "paper_open", "ticker": tk, "group": grp,
                 "state": f"S6自動 | {label} | {sg.get('quality', '')} | RV {sg.get('rv')} | H1 {sg.get('h1')} | "
                          f"{sg.get('universe', '')} | Limit {sg['limit']}{waited} | {S6_SIG_MARK} {sig}",
                 "entryDate": dates[k], "entry": round(entry, 2), "stop": round(stop, 2),
                 "t1": round(t1, 2), "t2": round(t2, 2),
                 "bonus": sg.get("bonus"), "trend": sg.get("streak"), "pullback": sg.get("retrace"),
                 "spy1m": "", "m1": ""})
        n += 1
        print(f"成交 [{grp}] {tk}: 突破 {sig} → {dates[k]} @ {entry:.2f}（Limit {sg['limit']}）"
              f" 止蝕 {stop:.2f} T1 {t1:.2f} T2 {t2:.2f}｜{sg.get('quality')}")
        res = simulate_s6(ch, k, entry, stop, t1, t2)
        done.add((tk, grp, sig)); iv.setdefault((tk, grp), []).append((dates[k], _exit_day(ch, res)))
        _post_s6_result(tk, grp, ch, entry, stop, res)
    print(f"S6 自動記錄：最近突破 {len(sigs)} 次 → 新成交 {n}，等緊 {wait}，取消 {cancel}，唔入 {skip}")

# ════════════════════════════════════════════════════════════════
# S3 突破 / S5 支持阻力 自動記錄（2026-10-05；訊號同掛單規則喺 scan.py s3_breakouts / s5_setups）
#   S3A / S3B / S3C = 手法A / B / C：Limit @ 訊號日收市，下一個交易日高開 > 1% 取消
#   S5R = 跟 checklist（4/4 + 3日🔥 + 結構）；S5S = 淨結構：Limit @ 0.786，最多等 20 日
#   出場：收市穿止蝕先平；T1 平一半、止蝕移去入場價；T2 平餘下（S3 入場價高過 T1 就全倉等 T2）
# ════════════════════════════════════════════════════════════════
AUTO_SIG_MARK = "訊號日"
# 上線日：之前嘅訊號唔記錄（Sasa 要 forward test，唔要補記歷史）。scanner 嘅 30 日清單仍然會顯示，方便對圖。
AUTO_START = "2026-10-02"

def cleanup_backfill(open_pos, closed):
    """刪走上線日之前嘅 S3 / S5 / S6 自動記錄（第一次上線時補記咗嘅舊訊號）。"""
    groups = set(AUTO_GROUP_STOP) | {g for g, _ in S6_GROUPS.values()}
    n = 0
    for lst, status in ((open_pos, "open"), (closed, "closed")):
        for x in list(lst):
            if x.get("group") not in groups:
                continue
            sd = _auto_sig_date(x.get("state")) or _s6_sig_date(x.get("state"))
            if sd and sd < AUTO_START:
                gv_post({"action": "paper_remove", "ticker": x["ticker"], "group": x["group"],
                         "entry": x.get("entry"), "status": status})
                lst.remove(x)
                n += 1
    if n:
        print(f"刪走 {n} 張上線日（{AUTO_START}）之前嘅自動記錄（只做 forward test）")
AUTO_CFG = {
    "S3": {"key": "s3Signals", "gfield": "method",
           "groups": {"A": ("S3A", "手法A", "Buildup底×0.98"), "B": ("S3B", "手法B", "假突破低×0.98"),
                      "C": ("S3C", "手法C", "回測低×0.98")}},
    "S5": {"key": "s5Signals", "gfield": "grp",
           "groups": {"R": ("S5R", "跟checklist", "A點"), "S": ("S5S", "淨結構", "A點")}},
}
AUTO_GROUP_STOP = {g: txt for cfg in AUTO_CFG.values() for g, _, txt in cfg["groups"].values()}

def _auto_sig_date(state):
    s = str(state or "")
    i = s.find(AUTO_SIG_MARK)
    return s[i + len(AUTO_SIG_MARK):].strip()[:10] if i >= 0 else None

def manage_t12_auto(p, charts):
    tk, grp = p["ticker"], p["group"]
    entry, stop = _num(p.get("entry")), _num(p.get("stop"))
    t1, t2 = _num(p.get("t1")), _num(p.get("t2"))
    ch = charts.get(tk) or {}
    if None in (entry, stop, t2) or entry <= stop or not ch.get("c"):
        return
    nd = _norm_date(str(p.get("entryDate", "")))
    if not nd:
        return
    ed = "%04d-%02d-%02d" % nd
    dates = _bar_dates(ch)
    if ed not in dates:
        return
    res = simulate_t12(ch, dates.index(ed), entry, stop, t1, t2, AUTO_GROUP_STOP.get(grp, "止蝕位"))
    _post_s6_result(tk, grp, ch, entry, stop, res, already_t1hit=str(p.get("t1hit", "")).upper() == "Y")

def open_auto(strat, data, charts, open_pos, closed):
    cfg = AUTO_CFG[strat]
    gmap = cfg["groups"]
    groups = [g for g, _, _ in gmap.values()]
    done = set()
    for x in list(open_pos) + list(closed):
        if x.get("group") in groups:
            done.add((x["ticker"], x["group"], _auto_sig_date(x.get("state"))))
    iv = _hold_intervals(open_pos, closed, groups)
    sigs = sorted(data.get(cfg["key"]) or [], key=lambda x: x["signalDate"])
    n = wait = cancel = skip = 0
    for sg in sigs:
        gk = sg.get(cfg["gfield"])
        if gk not in gmap or sg.get("skip") or sg.get("status") == "skip":
            skip += 1
            continue
        grp, label, stop_txt = gmap[gk]
        tk, sig = sg["ticker"], sg["signalDate"]
        if sig < AUTO_START:
            continue
        if (tk, grp, sig) in done:
            continue
        ch = charts.get(tk) or {}
        dates = _bar_dates(ch)
        if not ch.get("c") or sig not in dates:
            continue
        ks = dates.index(sig)
        if strat == "S5":
            st = scan.s5_order_status(sg["limit"], sg["stop"], sg["poleB"], ks, ch["o"], ch["l"], ch["c"])
        else:
            st = scan.limit_next_day_status(sg["limit"], sg["stop"], ks, ch["o"], ch["l"], ch["c"])
        if st["status"] == "wait":
            wait += 1
            continue
        if st["status"] == "cancel":
            cancel += 1
            continue
        k, entry = st["k"], st["px"]
        if _busy(iv, (tk, grp), dates[k]):
            print(f"  {strat} {tk} [{grp}]：訊號 {sig} 喺 {dates[k]} 成交，但嗰日已經揸緊同一組，唔再入")
            continue
        stop, t1, t2 = sg["stop"], sg.get("t1"), sg["t2"]
        if t1 is not None and t1 <= entry:
            t1 = None
        if t2 <= entry or entry <= stop:
            print(f"  {strat} {tk} [{grp}]：成交價 {entry:.2f} 唔啱（T2 {t2} / 止蝕 {stop}），唔入")
            continue
        if strat == "S3":
            desc = f"突破線 {sg.get('line')} | RV {sg.get('rv')}"
        else:
            desc = f"A {sg.get('poleA')} → B {sg.get('poleB')} | 整理區 {sg.get('congesBottom')}-{sg.get('congesTop')}"
        plan = "T1半+T2半" if t1 is not None else "全倉等T2"
        waited = f" 等{st.get('waited')}日" if strat == "S5" else ""
        gv_post({"action": "paper_open", "ticker": tk, "group": grp,
                 "state": f"{strat}自動 | {label} | {plan} | {desc} | {sg.get('universe', '')} | "
                          f"Limit {sg['limit']}{waited} | {AUTO_SIG_MARK} {sig}",
                 "entryDate": dates[k], "entry": round(entry, 2), "stop": round(stop, 2),
                 "t1": round(t1, 2) if t1 is not None else "", "t2": round(t2, 2),
                 "bonus": sg.get("bonus"), "trend": sg.get("streak"), "pullback": "", "spy1m": "", "m1": ""})
        n += 1
        print(f"成交 [{grp}] {tk}: 訊號 {sig} → {dates[k]} @ {entry:.2f}（Limit {sg['limit']}）止蝕 {stop:.2f}"
              + (f" T1 {t1:.2f}" if t1 is not None else "") + f" T2 {t2:.2f}")
        res = simulate_t12(ch, k, entry, stop, t1, t2, stop_txt)
        done.add((tk, grp, sig)); iv.setdefault((tk, grp), []).append((dates[k], _exit_day(ch, res)))
        _post_s6_result(tk, grp, ch, entry, stop, res)
    print(f"{strat} 自動記錄：最近訊號 {len(sigs)} 個 → 新成交 {n}，等緊 {wait}，取消 {cancel}，唔入 {skip}")

# ════════════════════════════════════════════════════════════════
# 2026-10-07：止蝕改用 Stop 單（盤中掂到就止蝕）。之前用「收市先算」已經平咗倉嘅自動記錄，
# 用新規則由入場日重新計一次；結果唔同就刪咗舊記錄再記過（state 加「盤中止蝕」標記，唔會再計）。
# ════════════════════════════════════════════════════════════════
STOP_RULE_DATE = "2026-10-07"
STOP_MARK = "盤中止蝕"

def _resim(x, charts):
    tk, grp = x["ticker"], x["group"]
    ch = charts.get(tk) or {}
    entry, stop = _num(x.get("entry")), _num(x.get("stop"))
    t1, t2 = _num(x.get("t1")), _num(x.get("t2"))
    ed = _iso(x.get("entryDate"))
    dates = _bar_dates(ch)
    if None in (entry, stop) or entry <= stop or not ed or ed not in dates:
        return None
    k0 = dates.index(ed)
    if grp in [a for a, _, _ in S1_AUTO_GROUPS]:
        return simulate_s1(ch, k0, entry, stop, t1, t2) if t1 is not None else None
    if grp in [g for g, _, _ in S7N_GROUPS]:
        return simulate_s7(ch, k0, entry, stop, dict((g, t) for g, t, _ in S7N_GROUPS)[grp])
    if grp in [g for g, _ in S6_GROUPS.values()]:
        return simulate_s6(ch, k0, entry, stop, t1, t2) if None not in (t1, t2) else None
    if grp in AUTO_GROUP_STOP:
        return simulate_t12(ch, k0, entry, stop, t1, t2, AUTO_GROUP_STOP[grp]) if t2 is not None else None
    return None

def migrate_intraday_stops(open_pos, closed, charts):
    groups = (set(a for a, _, _ in S1_AUTO_GROUPS) | set(g for g, _, _ in S7N_GROUPS)
              | set(g for g, _ in S6_GROUPS.values()) | set(AUTO_GROUP_STOP))
    n = 0
    for x in list(closed):
        if x.get("group") not in groups or STOP_MARK in str(x.get("state", "")):
            continue
        ex = _iso(x.get("exitDate"))
        if not ex or ex >= STOP_RULE_DATE:
            continue
        res = _resim(x, charts)
        if res is None:
            continue
        dates = _bar_dates(charts[x["ticker"]])
        new = res.get("exit")
        old_r = _num(x.get("r"))
        if new and dates[new[0]] == ex and old_r is not None and abs(round(new[3], 2) - old_r) < 0.01:
            continue                                   # 用新規則計都一樣，唔使改
        tk, grp = x["ticker"], x["group"]
        gv_post({"action": "paper_remove", "ticker": tk, "group": grp, "entry": x.get("entry"), "status": "closed"})
        state = str(x.get("state", "")) + f" | {STOP_MARK}"
        payload = {"action": "paper_open", "ticker": tk, "group": grp, "state": state,
                   "entryDate": _iso(x.get("entryDate")), "entry": x.get("entry"), "stop": x.get("stop"),
                   "t1": x.get("t1", ""), "t2": x.get("t2", "")}
        for k in ("bonus", "rsi", "trend", "pullback", "spy1m", "m1"):
            if k in x:
                payload[k] = x[k]
        gv_post(payload)
        x["state"] = state
        if new:
            entry, stop = float(x["entry"]), float(x["stop"])
            if res.get("t1hit"):
                gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
            k, px, reason, r = new
            gv_post({"action": "paper_close", "ticker": tk, "group": grp, "exitDate": dates[k],
                     "exitPx": round(px, 2), "reason": reason, "r": round(r, 2),
                     "pct": round((px - entry) / entry * 100, 1)})
            print(f"重新計 [{grp}] {tk}：{ex} R={old_r} → {dates[k]} {reason} R={r:.2f}")
            x.update({"exitDate": dates[k], "exitPx": round(px, 2), "reason": reason, "r": round(r, 2)})
        else:
            closed.remove(x)
            x.pop("exitDate", None)
            open_pos.append(x)
            print(f"重新計 [{grp}] {tk}：用盤中止蝕計仲未平倉，改返做持倉")
        n += 1
    if n:
        print(f"止蝕改用盤中 Stop 單：重新計咗 {n} 張已平倉自動記錄")

def main():
    # 1. 攞最新 scan data
    data = json.load(open("data.json"))
    stocks = {s["ticker"]: s for s in data["stocks"]}
    try:
        charts = json.load(open("charts.json"))
    except (FileNotFoundError, json.JSONDecodeError):
        charts = {}

    # 0. 周末 check：睇數據嘅交易日，唔睇時鐘。
    #    （舊做法用 utcnow：nightly scan 延遲到 UTC 星期六先行，就會成日 skip 咗星期五嘅交易日）
    session = session_date(charts)
    if session is None:
        wd = datetime.datetime.utcnow().weekday()
        if wd >= 5:
            print(f"周末（weekday={wd}）而且冇圖表數據 — skip paper trade")
            return
    else:
        print(f"數據交易日：{session}")

    # 2. 攞現有 paper 持倉（Google Sheet "Paper" tab）
    pf = gv_get("paper_list")
    open_pos = pf.get("open", [])      # [{ticker, entry, stop, entryDate, group}]
    closed = pf.get("closed", [])      # 已平倉

    cleanup_old_s1_auto(open_pos, closed)
    cleanup_backfill(open_pos, closed)
    migrate_intraday_stops(open_pos, closed, charts)

    # 持倉 key = ticker|group（同一隻股可同時喺 A、B 組）
    held = {(p["ticker"], p.get("group", "A")) for p in open_pos}

    # 3. 管理現有持倉：跌穿止賺線 → 平倉；跌穿止損 → 止蝕
    #    A/B 組止賺用 EMA20；C/D 組止賺用 10MA
    #    BUG FIX：入場當日唔 check 平倉（要至少隔一日），否則一買即平
    today = today_str()
    for p in list(open_pos):
        tk = p["ticker"]; grp = p.get("group", "A")
        ed = str(p.get("entryDate", ""))
        if grp in [a for a, _, _ in S1_AUTO_GROUPS]:
            manage_s1_auto(p, charts)
            continue
        if grp in [g for g, _, _ in S7N_GROUPS]:
            manage_s7_auto(p, charts)
            continue
        if grp in [g for g, _ in S6_GROUPS.values()]:
            manage_s6_auto(p, charts)
            continue
        if grp in AUTO_GROUP_STOP:
            manage_t12_auto(p, charts)
            continue
        if _same_day(ed, today):
            continue
        st = stocks.get(tk)
        if not st:
            continue
        close = st["close"]
        # TV 記錄剛 tick 落嚟可能仲未填 entry/stop（等緊你手動補），冇得計就 skip
        if p.get("entry") in (None, "") or p.get("stop") in (None, ""):
            continue
        entry = float(p["entry"])
        stop = float(p["stop"])

        # ── S1 / TV 組：人手揀股(或TradingView真實落單)，用當日 High/Low check T1/T2/止損 ──
        if grp == "S1":
            high = st.get("high", close)
            low = st.get("low", close)
            # 數據新鮮度：high/low 同 close 都等於入場（冇變）= 數據未更新
            if abs(close - entry) < 0.001 and abs(high - entry) < 0.001:
                continue
            t1 = _num(p.get("t1"))
            t2 = _num(p.get("t2"))
            t1hit = str(p.get("t1hit", "")).upper() == "Y"
            exit_reason = None
            exit_px = None
            # 掂咗 T1 之後：止損搬去 entry 同 T1 中間點（鎖定部分利潤，留返少少回調空間）
            eff_stop = stop
            if t1hit and t1 is not None:
                eff_stop = entry + (t1 - entry) * 0.5
            # 止損優先（保守）：當日 Low ≤ (新)止損
            if low <= eff_stop:
                exit_reason = "止蝕(跌穿保本止損@T1中間點)" if t1hit else "止蝕(跌穿止損)"
                exit_px = eff_stop
            # T2 止賺：當日 High ≥ T2
            elif t2 and high >= t2:
                exit_reason = "止賺(到T2 1.618)"
                exit_px = t2
            if exit_reason:
                r_mult = (exit_px - entry) / (entry - stop) if entry > stop else 0
                pct = (exit_px - entry) / entry * 100
                gv_post({"action": "paper_close", "ticker": tk, "group": grp,
                         "exitDate": today, "exitPx": round(exit_px, 2),
                         "reason": exit_reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
                print(f"平倉 [{grp}] {tk}: {exit_reason} R={r_mult:.2f} {pct:+.1f}%")
                held.discard((tk, grp))
            elif t1 and high >= t1 and not t1hit:
                # 掂咗 T1 → 標記（通知，止損同步搬去 entry/T1 中間點，唔即刻平）
                gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
                new_stop = entry + (t1 - entry) * 0.5
                print(f"📍 [{grp}] {tk}: 掂咗 T1 ${t1}（止損搬去 ${new_stop:.2f}，繼續持倉等 T2）")
            continue

        if grp == "TV":
            # 「我的記錄」係真實 TradingView 單，唔幫你自動平倉（怕同實際成交價/時間對唔上）。
            # 淨係做「通知」：掂咗 T1 就標記（畀 Market page/Paper Trade 頁顯示提示），
            # 止蝕/T2 完全唔喺呢度處理，交返俾前端用即時股價自己計「建議平倉」badge，
            # 真正平倉一定要你自己撳「平倉」揀，填返實際成交價。
            high = st.get("high", close)
            t1 = _num(p.get("t1"))
            t1hit = str(p.get("t1hit", "")).upper() == "Y"
            if t1 and high >= t1 and not t1hit:
                gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
                new_stop = entry + (t1 - entry) * 0.5
                print(f"📍 [TV] {tk}: 掂咗 T1 ${t1}（提示止損可以上調去 ${new_stop:.2f}，唔會自動平倉）")
            continue

        # 其他組（例如舊版 S7 A/B/C/D）：已移除，唔再處理

    # 4. 新入場：S1 自動記錄 + S7 突破 + S6 旗形
    if session:
        open_s1_auto(data, stocks, charts, open_pos, closed, session)
        open_s7_auto(data, charts, open_pos, closed)
        open_s6_auto(data, charts, open_pos, closed)
        open_auto("S3", data, charts, open_pos, closed)
        open_auto("S5", data, charts, open_pos, closed)
    print("Paper trade 完成")

if __name__ == "__main__":
    main()
