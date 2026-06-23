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

def main():
    # 1. 攞最新 scan data
    data = json.load(open("data.json"))
    stocks = {s["ticker"]: s for s in data["stocks"]}

    # 2. 攞現有 paper 持倉（Google Sheet "Paper" tab）
    pf = gv_get("paper_list")
    open_pos = pf.get("open", [])      # [{ticker, entry, stop, entryDate}]
    closed = pf.get("closed", [])      # 已平倉

    held = {p["ticker"] for p in open_pos}

    # 3. 管理現有持倉：跌穿 20MA → 平倉；跌穿止損 → 止蝕
    for p in list(open_pos):
        tk = p["ticker"]
        st = stocks.get(tk)
        if not st:
            continue
        close = st["close"]
        ema20 = st.get("ema20")
        stop = float(p["stop"])
        entry = float(p["entry"])
        exit_reason = None
        if close <= stop:
            exit_reason = "止蝕(穿突破位)"
        elif ema20 and close < ema20:
            exit_reason = "止賺(跌穿20MA)"
        if exit_reason:
            r_mult = (close - entry) / (entry - stop) if entry > stop else 0
            pct = (close - entry) / entry * 100
            gv_post({"action": "paper_close", "ticker": tk,
                     "exitDate": today_str(), "exitPx": round(close, 2),
                     "reason": exit_reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
            print(f"平倉 {tk}: {exit_reason} R={r_mult:.2f} {pct:+.1f}%")
            held.discard(tk)

    # 4. 揾新入場：最嚴 S7（突破放量/回測 + 旗桿強 + 跑贏×2 + Bonus≥4）
    for tk, st in stocks.items():
        if tk in held:
            continue
        s7 = st["strategies"].get("S7", {})
        if not s7.get("ready"):
            continue
        state = s7.get("state")
        if state not in ("突破放量", "回測"):
            continue
        # 最嚴 J Law filter
        if s7.get("pole") != "強":
            continue
        if s7.get("bonusScore", 0) < 4:
            continue
        rs = s7.get("rsStrong")
        if rs is not True:   # 必須確實跑贏（唔當 None）
            continue
        # 入場（用今日 close approximate 隔日開市；止損 = 突破位下）
        entry = st["close"]
        resist = s7.get("resist") or entry * 0.97
        stop = min(resist * 0.99, entry * 0.95)   # 突破位下方，最多 -5%
        if stop >= entry:
            continue
        gv_post({"action": "paper_open", "ticker": tk,
                 "entryDate": today_str(), "entry": round(entry, 2),
                 "stop": round(stop, 2), "state": state,
                 "bonus": s7.get("bonusScore"), "spy1m": s7.get("spy1m"),
                 "m1": s7.get("keyvals", {}).get("1M%")})
        print(f"開倉 {tk}: entry={entry:.2f} stop={stop:.2f} [{state}]")
        held.add(tk)

    print("Paper trade 完成")

if __name__ == "__main__":
    main()
