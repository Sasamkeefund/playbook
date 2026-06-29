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

def main():
    # 0. 周末唔好跑（美股冇開市，數據冇更新會搞亂平倉）
    wd = datetime.datetime.utcnow().weekday()  # 0=Mon ... 5=Sat 6=Sun
    if wd >= 5:
        print(f"周末（weekday={wd}）— 美股休市，skip paper trade")
        return

    # 1. 攞最新 scan data
    data = json.load(open("data.json"))
    stocks = {s["ticker"]: s for s in data["stocks"]}

    # 2. 攞現有 paper 持倉（Google Sheet "Paper" tab）
    pf = gv_get("paper_list")
    open_pos = pf.get("open", [])      # [{ticker, entry, stop, entryDate, group}]
    closed = pf.get("closed", [])      # 已平倉

    # 持倉 key = ticker|group（同一隻股可同時喺 A、B 組）
    held = {(p["ticker"], p.get("group", "A")) for p in open_pos}

    # 3. 管理現有持倉：跌穿 20MA → 平倉；跌穿止損 → 止蝕
    #    BUG FIX：入場當日唔 check 平倉（要至少隔一日），否則一買即平
    today = today_str()
    for p in list(open_pos):
        tk = p["ticker"]; grp = p.get("group", "A")
        # 入場當日唔平倉（避免同日出入）
        ed = str(p.get("entryDate", ""))
        if _same_day(ed, today):
            continue
        st = stocks.get(tk)
        if not st:
            continue
        close = st["close"]
        ema20 = st.get("ema20")
        stop = float(p["stop"])
        entry = float(p["entry"])
        # 數據新鮮度：如果 close 同入場價一模一樣（冇變）= 數據未更新，唔平倉
        if abs(close - entry) < 0.001:
            continue
        exit_reason = None
        if close <= stop:
            exit_reason = "止蝕(1.5×ATR)"
        elif ema20 and close < ema20:
            exit_reason = "止賺(跌穿20MA)"
        if exit_reason:
            r_mult = (close - entry) / (entry - stop) if entry > stop else 0
            pct = (close - entry) / entry * 100
            gv_post({"action": "paper_close", "ticker": tk, "group": grp,
                     "exitDate": today, "exitPx": round(close, 2),
                     "reason": exit_reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
            print(f"平倉 [{grp}] {tk}: {exit_reason} R={r_mult:.2f} {pct:+.1f}%")
            held.discard((tk, grp))

    # 4. 揾新入場 — 兩組對比：
    #    A 組 = 全部 S7 ready（59隻，測整體）
    #    B 組 = S7 ready + Bonus 5/5（高質精選）
    #    BUG FIX：今日已平倉嘅股，今日唔好再買返（避免重複出入）
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
        common = {"state": "J Law VCP", "entryDate": today,
                  "entry": round(entry, 2), "stop": round(stop, 2),
                  "bonus": s7.get("bonusScore"), "spy1m": s7.get("spy1m"),
                  "m1": s7.get("keyvals", {}).get("1M%")}
        # A 組：全部 S7 ready
        if (tk, "A") not in held and (tk, "A") not in closed_today:
            gv_post({"action": "paper_open", "ticker": tk, "group": "A", **common})
            print(f"開倉 [A] {tk}: entry={entry:.2f} stop={stop:.2f}")
            held.add((tk, "A"))
        # B 組：只 Bonus 5/5（高質精選）
        if s7.get("bonusScore", 0) >= 5 and (tk, "B") not in held and (tk, "B") not in closed_today:
            gv_post({"action": "paper_open", "ticker": tk, "group": "B", **common})
            print(f"開倉 [B] {tk}: entry={entry:.2f} stop={stop:.2f} (Bonus5/5)")
            held.add((tk, "B"))
    print("Paper trade 完成")

if __name__ == "__main__":
    main()
