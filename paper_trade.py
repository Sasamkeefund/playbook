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
    open_pos = pf.get("open", [])      # [{ticker, entry, stop, entryDate, group}]
    closed = pf.get("closed", [])      # 已平倉

    # 持倉 key = ticker|group（同一隻股可同時喺 A、B 組）
    held = {(p["ticker"], p.get("group", "A")) for p in open_pos}

    # 3. 管理現有持倉：跌穿 20MA → 平倉；跌穿止損 → 止蝕
    for p in list(open_pos):
        tk = p["ticker"]; grp = p.get("group", "A")
        st = stocks.get(tk)
        if not st:
            continue
        close = st["close"]
        ema20 = st.get("ema20")
        stop = float(p["stop"])
        entry = float(p["entry"])
        exit_reason = None
        if close <= stop:
            exit_reason = "止蝕(1.5×ATR)"
        elif ema20 and close < ema20:
            exit_reason = "止賺(跌穿20MA)"
        if exit_reason:
            r_mult = (close - entry) / (entry - stop) if entry > stop else 0
            pct = (close - entry) / entry * 100
            gv_post({"action": "paper_close", "ticker": tk, "group": grp,
                     "exitDate": today_str(), "exitPx": round(close, 2),
                     "reason": exit_reason, "r": round(r_mult, 2), "pct": round(pct, 1)})
            print(f"平倉 [{grp}] {tk}: {exit_reason} R={r_mult:.2f} {pct:+.1f}%")
            held.discard((tk, grp))

    # 4. 揾新入場 — 兩組對比：
    #    A 組 = 純 J Law（S7 ready）
    #    B 組 = S7 ready + VCP（整固≥15日 + 波動收窄）
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
        common = {"entryDate": today_str(), "entry": round(entry, 2),
                  "stop": round(stop, 2), "bonus": s7.get("bonusScore"),
                  "spy1m": s7.get("spy1m"), "m1": s7.get("keyvals", {}).get("1M%")}
        # A 組：純 J Law
        if (tk, "A") not in held:
            gv_post({"action": "paper_open", "ticker": tk, "group": "A",
                     "state": "純J Law", **common})
            print(f"開倉 [A] {tk}: entry={entry:.2f} stop={stop:.2f}")
            held.add((tk, "A"))
        # B 組：S7 ready + VCP（整固≥15 + 收窄）
        consol = s7.get("consolDays") or 0
        vc = s7.get("volContract")
        if consol >= 15 and vc is True and (tk, "B") not in held:
            gv_post({"action": "paper_open", "ticker": tk, "group": "B",
                     "state": "J Law+VCP", **common})
            print(f"開倉 [B] {tk}: entry={entry:.2f} stop={stop:.2f} (整固{consol}日+收窄)")
            held.add((tk, "B"))
    print("Paper trade 完成")

if __name__ == "__main__":
    main()
