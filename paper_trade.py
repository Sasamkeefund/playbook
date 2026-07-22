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

def main():
    # 0. 周末唔好跑（美股冇開市，數據冇更新會搞亂平倉）
    wd = datetime.datetime.utcnow().weekday()  # 0=Mon ... 5=Sat 6=Sun
    if wd >= 5:
        print(f"周末（weekday={wd}）— 美股休市，skip paper trade")
        return

    # 1. 攞最新 scan data
    data = json.load(open("data.json"))
    stocks = {s["ticker"]: s for s in data["stocks"]}
    try:
        charts = json.load(open("charts.json"))
    except (FileNotFoundError, json.JSONDecodeError):
        charts = {}

    # 2. 攞現有 paper 持倉（Google Sheet "Paper" tab）
    pf = gv_get("paper_list")
    open_pos = pf.get("open", [])      # [{ticker, entry, stop, entryDate, group}]
    closed = pf.get("closed", [])      # 已平倉

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

        # ── S1 / TV 組：人手揀股(或TradingView真實落單)，用當日 High/Low check T1/T2/止損 ──
        if grp in ("S1", "TV"):
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
