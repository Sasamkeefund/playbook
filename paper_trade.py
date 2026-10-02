#!/usr/bin/env python3
"""
S7 機械 Paper Trade 引擎（跟 J Law）— 每日 scan 後自動行
入場：S7 ready + state 突破放量/回測 + 最嚴(旗桿強 + 跑贏×2 + Bonus≥4) → 隔日開市買
止損：突破位(resist)下方
止賺：收市跌穿 20MA → 平倉（移動止損，let winners run）
持倉 + 戰績存 Google Sheet（同 watchlist 一樣，零本地痕跡）
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

# S7 止賺保護期：入場後要曾經升穿呢個 buffer 先當「真正止賺」，
# 未升到就淨係用硬止損睇住，唔會一有正常回調篤穿 MA 就篤走（未賺過錢）
S7_BUFFER_MULT = 0.5  # buffer = entry + 0.5 × ATR(估算)

# 2026-09-30 停止 S7 開新單：4,402 單已平倉，四組全部負期望（-0.28R 至 -0.48R）。
# 原因：入場只 check S7 ready（VCP 整固中），冇等突破就買，唔係 J Law 原本做法。
# 現有持倉照舊按規則平倉。修好入場邏輯（等突破）之後先改返 True。
S7_OPEN_NEW = False

def max_close_since(charts, ticker, entry_date_str):
    """揾返 ticker 喺 charts.json 入面，entryDate 至今嘅最高 close。
    冇歷史數據就 return None（外面會 fallback 用當日 close）。"""
    hist = charts.get(ticker)
    if not hist or not hist.get("t") or not hist.get("c"):
        return None
    ed = _norm_date(entry_date_str)
    if not ed:
        return None
    ed_date = datetime.date(ed[0], ed[1], ed[2])
    mx = None
    for ts, c in zip(hist["t"], hist["c"]):
        try:
            d = datetime.datetime.utcfromtimestamp(ts).date()
        except (ValueError, OSError, OverflowError):
            continue
        if d >= ed_date and c is not None:
            mx = c if mx is None else max(mx, c)
    return mx

# ════════════════════════════════════════════════════════════════
# S1 自動記錄（跟 Sasa 平時 S1 做法，2026-09-30 確認）
#   入場：S1 ready + dashboard 🟢/🔵（跌穿 EMA20 後 3 日內企返上）→ 即晚放 Limit（訊號日收市價），
#         第二個交易日 Low 跌到 Limit 先成交（裂口低開用開市價），入場日 = 成交日；冇成交就取消
#   止蝕：跌穿期間最低位；兩組對比 —— S1A0 冇緩衝、S1A1 再低 1%
#   T1  ：跌穿日之前 20 個交易日最高位（前頂）
#   R:R→T1 ≥ 2 → 到 T1 全平；< 2 → T2 = T1 + (T1−止蝕)×1.618，到 T1 平一半、T2 平另一半
#   連 T2 都 R:R < 2 → 唔入
#   到 T1 後餘下一半止蝕移去入場同 T1 中間；止蝕用當日 Low 觸發（同 S1 人手組一樣）
#   同一隻股同一組 15 日內只入一次（同一個 setup 唔重複入）
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

def manage_s1_auto(p, st, charts, today):
    """管理一單 S1 自動記錄。返回 True = 已處理（平倉或冇嘢做）。"""
    tk, grp = p["ticker"], p["group"]
    entry, stop = _num(p.get("entry")), _num(p.get("stop"))
    t1, t2 = _num(p.get("t1")), _num(p.get("t2"))
    if entry is None or stop is None or t1 is None or entry <= stop:
        return
    close = st["close"]; high = st.get("high", close); low = st.get("low", close)
    if abs(close - entry) < 0.001 and abs(high - entry) < 0.001:
        return  # 數據未更新
    ch = charts.get(tk) or {}
    opn = (ch.get("o") or [None])[-1]
    risk = entry - stop
    R = lambda px: (px - entry) / risk
    t1hit = str(p.get("t1hit", "")).upper() == "Y"
    eff_stop = entry + (t1 - entry) * 0.5 if t1hit else stop
    reason = exit_px = r_mult = None
    if low <= eff_stop:
        exit_px = min(eff_stop, opn) if opn else eff_stop   # 裂口低開：用開市價（保守）
        if t1hit:
            reason = "止蝕(餘下一半，止蝕已移去入場/T1中間)"
            r_mult = 0.5 * R(t1) + 0.5 * R(exit_px)
        else:
            reason = "止蝕(跌穿前底)"
            r_mult = R(exit_px)
    elif t2 is None and high >= t1:
        exit_px = t1; reason = "止賺(到T1全平)"; r_mult = R(t1)
    elif t2 is not None and high >= t2:
        exit_px = t2; reason = "止賺(T1平一半+T2平一半)"; r_mult = 0.5 * R(t1) + 0.5 * R(t2)
    elif t2 is not None and high >= t1 and not t1hit:
        gv_post({"action": "paper_t1hit", "ticker": tk, "group": grp, "t1hit": "Y"})
        print(f"📍 [{grp}] {tk}: 到T1 ${t1}（平一半，餘下止蝕移去 ${entry + (t1 - entry) * 0.5:.2f}）")
        return
    if reason:
        pct = r_mult * risk / entry * 100
        gv_post({"action": "paper_close", "ticker": tk, "group": grp,
                 "exitDate": today, "exitPx": round(exit_px, 2),
                 "reason": reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
        print(f"平倉 [{grp}] {tk}: {reason} R={r_mult:.2f}")

S1_ENTRY_MARK = "Limit成交"

def cleanup_old_s1_auto(open_pos, closed):
    """一次性：刪走舊做法（訊號日收市即入）嘅 S1 自動記錄，由 Limit 成交做法重新開始。"""
    groups = [a for a, _, _ in S1_AUTO_GROUPS]
    n = 0
    for lst, status in ((open_pos, "open"), (closed, "closed")):
        for x in list(lst):
            if x.get("group") in groups and S1_ENTRY_MARK not in str(x.get("state", "")):
                gv_post({"action": "paper_remove", "ticker": x["ticker"], "group": x["group"],
                         "entry": x.get("entry"), "status": status})
                lst.remove(x); n += 1
    if n:
        print(f"S1 自動：刪走 {n} 張舊做法記錄（訊號日即入），改用 Limit 成交做法")

def open_s1_auto(data, stocks, charts, open_pos, closed, session):
    """上個交易日嘅 🟢🔵 訊號（scan.py 寫嘅 s1Pending）→ 今日 Low 跌到 Limit 先成交。"""
    recent = {}
    for x in list(open_pos) + list(closed):
        g = x.get("group")
        if g in [a for a, _, _ in S1_AUTO_GROUPS]:
            recent.setdefault((x["ticker"], g), []).append(str(x.get("entryDate", "")))
    pend = data.get("s1Pending") or []
    n = miss = 0
    for pd in pend:
        tk = pd["ticker"]
        if not pd.get("signalDate") or pd["signalDate"] >= session:
            continue
        st = stocks.get(tk)
        ch = charts.get(tk) or {}
        if not st:
            continue
        low = st.get("low", st["close"])
        opn = (ch.get("o") or [None])[-1]
        limit = pd["limit"]
        if low > limit:
            miss += 1
            print(f"  S1 掛單冇成交 {tk}：Limit ${limit:.2f}，今日最低 ${low:.2f}")
            continue
        fill = min(limit, opn) if opn else limit   # 裂口低開 → 用開市價成交
        for grp, buf, buf_txt in S1_AUTO_GROUPS:
            gaps = [_days_between(d, session) for d in recent.get((tk, grp), [])]
            if any(g is not None and g <= S1_AUTO_COOLDOWN_DAYS for g in gaps):
                continue
            stop = pd["low"] * buf; t1 = pd["t1"]
            plan = s1_plan(fill, stop, t1)
            if not plan:
                print(f"  S1自動 skip {tk} [{grp}]：成交價 ${fill:.2f} 計 R:R 唔夠 或 已經高過前頂")
                continue
            t2, plan_txt, rr1, rr2 = plan
            dot = "🟢" if pd.get("color") == "green" else "🔵"
            gv_post({"action": "paper_open", "ticker": tk, "group": grp,
                     "state": f"S1自動 {dot} | {buf_txt} | {plan_txt} | {pd.get('universe', '')} | {S1_ENTRY_MARK}(訊號{pd['signalDate'][5:]})",
                     "entryDate": session, "entry": round(fill, 2), "stop": round(stop, 2),
                     "t1": round(t1, 2), "t2": round(t2, 2) if t2 else "",
                     "bonus": pd.get("bonus"), "rsi": pd.get("rsi"), "trend": pd.get("trend"),
                     "pullback": pd.get("pullback"), "spy1m": "", "m1": ""})
            recent.setdefault((tk, grp), []).append(session)
            n += 1
            print(f"成交 [{grp}] {tk} {dot}: 訊號{pd['signalDate']} Limit {limit:.2f} → 成交 {fill:.2f} "
                  f"止蝕 {stop:.2f} T1 {t1:.2f}" + (f" T2 {t2:.2f}" if t2 else "") + f" → {plan_txt}")
            if low <= stop:   # 成交當日已經跌穿止蝕（保守：當日止蝕）
                r = (stop - fill) / (fill - stop)
                gv_post({"action": "paper_close", "ticker": tk, "group": grp, "exitDate": session,
                         "exitPx": round(stop, 2), "reason": "止蝕(成交當日跌穿前底)",
                         "r": round(r, 2), "pct": round((stop - fill) / fill * 100, 1)})
                print(f"平倉 [{grp}] {tk}: 成交當日已跌穿前底 R={r:.2f}")
    print(f"S1 自動記錄：掛單 {len(pend)} 張，成交開 {n} 單，冇成交 {miss} 張")

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

    # 持倉 key = ticker|group（同一隻股可同時喺 A、B 組）
    held = {(p["ticker"], p.get("group", "A")) for p in open_pos}

    # 3. 管理現有持倉：跌穿止賺線 → 平倉；跌穿止損 → 止蝕
    #    A/B 組止賺用 EMA20；C/D 組止賺用 10MA
    #    BUG FIX：入場當日唔 check 平倉（要至少隔一日），否則一買即平
    today = today_str()
    for p in list(open_pos):
        tk = p["ticker"]; grp = p.get("group", "A")
        ed = str(p.get("entryDate", ""))
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

        if grp in [a for a, _, _ in S1_AUTO_GROUPS]:
            if session and not _same_day(ed, session):
                manage_s1_auto(p, st, charts, session)
            continue

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

        # ── S7 組（A/B/C/D）：用收市價 + trail ──
        # 止賺線：A/B = EMA20；C/D = 10MA
        trail = st.get("sma10") if grp in ("C", "D") else st.get("ema20")
        trail_name = "10MA" if grp in ("C", "D") else "20MA"
        # 數據新鮮度：close 同入場價一模一樣（冇變）= 數據未更新，唔平倉
        if abs(close - entry) < 0.001:
            continue
        exit_reason = None
        if close <= stop:
            exit_reason = "止蝕(1.5×ATR)"
        elif trail and close < trail:
            # 保護期：要曾經升穿 entry + 0.5×ATR(估算) 先當「真正止賺」，
            # 未升到就當未達標，唔平倉（避免入場即回調、未賺過錢就俾正常波動篤穿MA走）
            atr_est = (entry - stop) / 1.5 if entry > stop else 0
            buffer_px = entry + S7_BUFFER_MULT * atr_est
            mx = max_close_since(charts, tk, ed)
            if mx is None:
                mx = max(close, st.get("high", close))  # 冇歷史數據 fallback
            if mx >= buffer_px:
                exit_reason = "止賺(跌穿" + trail_name + ")" if close > entry else "止蝕(曾達標後打返轉，跌穿" + trail_name + ")"
            # else：未升穿保護buffer，唔平倉，繼續持有等硬止損
        if exit_reason:
            r_mult = (close - entry) / (entry - stop) if entry > stop else 0
            pct = (close - entry) / entry * 100
            gv_post({"action": "paper_close", "ticker": tk, "group": grp,
                     "exitDate": today, "exitPx": round(close, 2),
                     "reason": exit_reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
            print(f"平倉 [{grp}] {tk}: {exit_reason} R={r_mult:.2f} {pct:+.1f}%")
            held.discard((tk, grp))

    # 4. 揾新入場 — 4 組對比（2×2：入場 × 止賺）：
    #    A = 全部 ready + EMA20止賺   B = Bonus5/5 + EMA20止賺
    #    C = 全部 ready + 10MA止賺    D = Bonus5/5 + 10MA止賺
    closed_today = {(c["ticker"], c.get("group", "A")) for c in closed
                    if _same_day(str(c.get("exitDate", "")), today)}
    if session:
        open_s1_auto(data, stocks, charts, open_pos, closed, session)

    if not S7_OPEN_NEW:
        print("S7 開新單已暫停（S7_OPEN_NEW=False），只管理現有持倉")
        print("Paper trade 完成")
        return
    for tk, st in stocks.items():
        s7 = st["strategies"].get("S7", {})
        if not s7.get("ready"):
            continue
        entry = st["close"]
        atr14 = st.get("atr14")
        if not atr14 or atr14 <= 0:
            continue
        stop = entry - 1.5 * atr14
        if stop <= 0 or stop >= entry:
            continue
        is55 = s7.get("bonusScore", 0) >= 5
        common = {"state": "J Law VCP", "entryDate": today,
                  "entry": round(entry, 2), "stop": round(stop, 2),
                  "bonus": s7.get("bonusScore"), "spy1m": s7.get("spy1m"),
                  "m1": s7.get("keyvals", {}).get("1M%")}
        # 開倉：A(全部+20MA)、B(5/5+20MA)、C(全部+10MA)、D(5/5+10MA)
        groups = [("A", True), ("B", is55), ("C", True), ("D", is55)]
        for g, cond in groups:
            if cond and (tk, g) not in held and (tk, g) not in closed_today:
                gv_post({"action": "paper_open", "ticker": tk, "group": g, **common})
                held.add((tk, g))
        print(f"開倉 {tk}: entry={entry:.2f} stop={stop:.2f}" + (" [5/5]" if is55 else ""))
    print("Paper trade 完成")

if __name__ == "__main__":
    main()
