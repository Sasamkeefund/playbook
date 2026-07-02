#!/usr/bin/env python3
"""
S6 旗形突破日 RV 自動記錄 — 每日 scan 後自動行
邏輯：FibAB 表入面淨係填咗 h1(頸線) 但仲未有 breakoutDate 嘅 ticker，
      如果今日 close > h1，即係「第一次偵測到突破」→ 鎖定嗰日嘅 RV + 手法(A/C)存落 Sheet。
      之後 watchlist.html 就唔會再用「即時 RV」，改用呢個鎖定咗嘅 breakoutRV/method。
"""
import json, datetime, urllib.request, urllib.parse

WL_API = "https://script.google.com/macros/s/AKfycbw-taBatpcuHXt5daIGq3Bo7lGw9OvdtsNeg292qdN0eW4RyxWLV4qf-oACvaVHHUI-Bg/exec"
RV_THRESHOLD = 1.5  # RV >= 1.5 = 手法A(放量) ; < 1.5 = 手法C(縮量)


def gv_get(action):
    url = WL_API + "?" + urllib.parse.urlencode({"action": action})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("GET fail", e)
        return {}


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


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def main():
    wd = datetime.datetime.utcnow().weekday()
    if wd >= 5:
        print(f"周末（weekday={wd}）— 美股休市，skip S6 breakout")
        return

    data = json.load(open("data.json"))
    stocks = {s["ticker"]: s for s in data["stocks"]}

    fib_map = gv_get("fib_all")  # { TICKER: {a, b, h1, breakoutDate, breakoutRV, method} }
    if not fib_map:
        print("FibAB 表冇資料，skip")
        return

    today = today_str()
    hit = 0
    for ticker, fb in fib_map.items():
        h1 = _num(fb.get("h1"))
        if h1 is None:
            continue
        # 已經記錄過突破 → 唔再覆寫（鎖定）
        if fb.get("breakoutDate"):
            continue
        st = stocks.get(ticker.upper())
        if not st:
            continue
        close = st.get("close")
        rv = st.get("rvol")
        if close is None or close <= h1:
            continue
        method = "A" if (rv is not None and rv >= RV_THRESHOLD) else "C"
        gv_post({
            "action": "fib_breakout",
            "ticker": ticker,
            "breakoutDate": today,
            "breakoutRV": rv,
            "method": method,
        })
        hit += 1
        print(f"🚩 S6 突破鎖定 {ticker}: close={close} > h1={h1}  RV={rv}  手法{method}")

    print(f"S6 breakout 完成，今次新鎖定 {hit} 隻")


if __name__ == "__main__":
    main()
