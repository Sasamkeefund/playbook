#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投機十步曲 · S1-S6 Scanner + Bonus Points（夜晚 GitHub Actions 跑）
====================================================================
Phase 1.5 升級：加埋 Bonus 條件（b1-b5）計分

解決舊 app 兩個問題：
  1. 攞 data 唔穩 —— 全部喺 server 度一次過攞、計、寫 data.json，前端唔使自己上網
  2. 同 TradingView 唔同 —— 攞 5 年 history + Wilder RSI/ATR + 收市後跑

輸出 data.json 畀前端讀。

只需要標準庫 + requests：  pip install requests
"""

import json
import time
import sys
from datetime import datetime, timezone

import requests

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
HISTORY_RANGE = "5y"
INTERVAL = "1d"
STREAK_LOOKBACK = 60
REQUEST_SLEEP = 0.25
OUTPUT_FILE = "data.json"

YF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}

# 六個策略 metadata
STRATEGY_META = {
    "S1": {"name": "順勢交易",  "dir": "Long",       "live": True,  "reqMax": 5, "bonusMax": 5},
    "S2": {"name": "趨勢終結",  "dir": "Short",      "live": False, "reqMax": 5, "bonusMax": 5},
    "S3": {"name": "突破交易",  "dir": "Long",       "live": True,  "reqMax": 5, "bonusMax": 5},
    "S4": {"name": "假突破",    "dir": "Long/Short", "live": False, "reqMax": 4, "bonusMax": 4},
    "S5": {"name": "支持阻力",  "dir": "Long",       "live": True,  "reqMax": 4, "bonusMax": 4},
    "S6": {"name": "圖表形態",  "dir": "Long",       "live": True,  "reqMax": 5, "bonusMax": 5},
}


# ─────────────────────────────────────────────────────────────
# 技術指標
# ─────────────────────────────────────────────────────────────
def ema(values, length):
    out = [None] * len(values)
    if len(values) == 0:
        return out
    alpha = 2.0 / (length + 1)
    prev = values[0]
    out[0] = prev
    for i in range(1, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    alpha = 1.0 / length
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(closes, length=14):
    n = len(closes)
    out = [None] * n
    if n < length + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        gains[i] = chg if chg > 0 else 0.0
        losses[i] = -chg if chg < 0 else 0.0
    avg_gain = rma(gains[1:], length)
    avg_loss = rma(losses[1:], length)
    for j in range(len(avg_gain)):
        i = j + 1
        ag, al = avg_gain[j], avg_loss[j]
        if ag is None or al is None:
            continue
        if al == 0:
            out[i] = 100.0
        else:
            rs = ag / al
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def atr(highs, lows, closes, length):
    n = len(closes)
    tr = [None] * n
    if n == 0:
        return [None] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    return rma(tr, length)


def sma_at(values, end_idx, length):
    if end_idx + 1 < length:
        return None
    window = values[end_idx - length + 1: end_idx + 1]
    return sum(window) / length


def highest(values, end_idx, length):
    if end_idx + 1 < length:
        return None
    return max(values[end_idx - length + 1: end_idx + 1])


def lowest(values, end_idx, length):
    if end_idx + 1 < length:
        return None
    return min(values[end_idx - length + 1: end_idx + 1])


def pct_change(values, idx, lookback):
    if idx - lookback < 0:
        return None
    base = values[idx - lookback]
    if base == 0:
        return None
    return (values[idx] / base - 1) * 100


# ─────────────────────────────────────────────────────────────
# 策略評估（包含 bonus）
# ─────────────────────────────────────────────────────────────
def eval_strategies(idx, closes, highs, lows, volumes,
                    ema20a, ema50a, ema200a, rsia, atr5a, atr14a):
    """返回 {S1:{conds, bonus, score, bonusScore, ready, keyvals}, ...}。"""
    e20, e50, e200 = ema20a[idx], ema50a[idx], ema200a[idx]
    r = rsia[idx]
    if None in (e20, e50, e200, r):
        return None
    if idx < 63:
        return None

    close = closes[idx]
    perf1d = pct_change(closes, idx, 1)
    perf1w = pct_change(closes, idx, 5)
    perf1m = pct_change(closes, idx, 21)
    perf3m = pct_change(closes, idx, 63)

    relvol_base = sma_at(volumes, idx, 20)
    relvol = volumes[idx] / relvol_base if relvol_base else None
    vol5 = sma_at(volumes, idx, 5)
    vol20 = sma_at(volumes, idx, 20)

    h52 = highest(highs, idx, 252) or highest(highs, idx, idx + 1)
    pct_from_high = (h52 - close) / h52 * 100 if h52 else None

    ema200_slope = (e200 - ema200a[idx - 10]) if (idx >= 10 and ema200a[idx - 10] is not None) else None

    a5, a14 = atr5a[idx], atr14a[idx]

    # bonus 用：距 EMA 百分比、近期波幅（對齊 Cowork 獨立 scanner）
    dist_ema50 = (close - e50) / e50 * 100
    dist_ema200 = (close - e200) / e200 * 100
    hl5_lo = lowest(lows, idx, 5)
    hl5_hi = highest(highs, idx, 5)
    hiLo5 = (hl5_hi - hl5_lo) / hl5_lo * 100 if (hl5_lo and hl5_hi) else None
    hl10_lo = lowest(lows, idx, 10)
    hl10_hi = highest(highs, idx, 10)
    hiLo10 = (hl10_hi - hl10_lo) / hl10_lo * 100 if (hl10_lo and hl10_hi) else None

    res = {}

    # ── EMA20 假突破偵測（參考用，可能同 TradingView 差 1 日）──
    # daysBelow   = 最近一次連續「收市跌穿 EMA20」嘅日數
    # daysRecover = 跌穿之後用咗幾多日 close 返上 EMA20（今日返到 = 1；仲喺下面 = 0）
    days_below = 0
    days_recover = 0
    if e20 is not None:
        if close < e20:
            # 今日仲喺下面 → 數連續跌穿日數，未收復
            j = idx
            while j >= 0 and ema20a[j] is not None and closes[j] < ema20a[j]:
                days_below += 1
                j -= 1
            days_recover = 0
        else:
            # 今日喺上面 → 數今日往前連續喺上面嘅日數（收復後日數）
            j = idx
            rec = 0
            while j >= 0 and ema20a[j] is not None and closes[j] >= ema20a[j]:
                rec += 1
                j -= 1
            # j 而家指住最近一個跌穿日（如有）
            if j >= 0 and ema20a[j] is not None and closes[j] < ema20a[j]:
                days_recover = rec
                k = j
                while k >= 0 and ema20a[k] is not None and closes[k] < ema20a[k]:
                    days_below += 1
                    k -= 1
            else:
                days_below = 0
                days_recover = 0

    # ── S1 順勢交易（Required 5/5, Bonus 5/5）──
    c = [
        close > e20,
        close > e50,
        (ema200_slope is not None and close > e200 and ema200_slope > 0),
        (40 <= r <= 70),
        (perf3m is not None and perf1m is not None and perf3m > 5 and perf1m < 25),
    ]
    b = [
        e20 > e50,
        e50 > e200,
        (relvol is not None and relvol < 0.9),
        (perf1w is not None and -8 <= perf1w <= 0),
        (pct_from_high is not None and pct_from_high < 15),
    ]
    res["S1"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) == 5,
                 "daysBelow20": days_below, "daysRecover": days_recover,
                 "keyvals": {"RSI": round(r, 1), "1W%": round(perf1w, 1) if perf1w is not None else None}}

    # ── S2 趨勢終結（Required 4/5, Bonus 5/5）──
    c = [
        close < e50,
        close < e200,
        (perf1d is not None and perf1d < -5),
        (relvol is not None and relvol > 2.0),
        (r < 35),
    ]
    b = []  # Cowork 冇定義 S2 bonus（未實戰）
    res["S2"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": 0, "ready": sum(c) >= 4,
                 "keyvals": {"RSI": round(r, 1), "1D%": round(perf1d, 1) if perf1d is not None else None}}

    # ── S3 突破交易（Required 4/5, Bonus 5/5）──
    c = [
        (pct_from_high is not None and pct_from_high <= 4),
        close > e50,
        ((relvol is not None and relvol < 0.75) or (vol5 is not None and vol20 is not None and vol5 < vol20)),
        (a5 is not None and a14 is not None and a5 < a14),
        (perf3m is not None and perf3m > 10),
    ]
    b = [
        close > e20,
        close > e200,
        (50 <= r <= 70),
        (perf1w is not None and -3 <= perf1w <= 3),
        (pct_from_high is not None and pct_from_high <= 2),
    ]
    res["S3"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) >= 4,
                 "keyvals": {"距52W高%": round(-pct_from_high, 1) if pct_from_high is not None else None, "3M%": round(perf3m, 1) if perf3m is not None else None}}

    # ── S4 假突破（Required 3/4, Bonus 4/4）──
    c = [
        (pct_from_high is not None and pct_from_high <= 8),
        (45 <= r <= 65),
        (perf1w is not None and -4 <= perf1w <= 2),
        (perf1m is not None and abs(perf1m) < 8),
    ]
    b = []  # Cowork 冇定義 S4 bonus（未實戰）
    res["S4"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": 0, "ready": sum(c) >= 3,
                 "keyvals": {"RSI": round(r, 1), "1W%": round(perf1w, 1) if perf1w is not None else None}}

    # ── S5 支持阻力（Required 4/4, Bonus 4/4）──
    c = [
        close > e50,
        close > e200,
        (30 <= r <= 55),
        (perf1m is not None and -20 <= perf1m <= -5),
    ]
    b = [
        (-3 <= dist_ema50 <= 5),
        (0 <= dist_ema200 <= 15),
        (relvol is not None and relvol < 0.8),
        (perf1w is not None and -5 <= perf1w <= 0),
        (perf3m is not None and perf3m > 0),
    ]
    res["S5"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) == 4,
                 "keyvals": {"RSI": round(r, 1), "1M%": round(perf1m, 1) if perf1m is not None else None}}

    # ── S6 圖表形態（Required 4/5, Bonus 5/5）──
    c = [
        close > e20,
        close > e50,
        (perf1m is not None and perf1m > 8),
        (perf1w is not None and -5 <= perf1w <= 0),
        (relvol is not None and relvol < 0.8),
    ]
    b = [
        (hiLo5 is not None and hiLo5 < 4.0),
        close > e200,
        (pct_from_high is not None and pct_from_high <= 15),
        (hiLo10 is not None and hiLo10 < 7.0),
        (perf3m is not None and perf3m > 15),
    ]
    res["S6"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) >= 4,
                 "keyvals": {"1M%": round(perf1m, 1) if perf1m is not None else None, "1W%": round(perf1w, 1) if perf1w is not None else None}}

    return res


def compute_streaks(closes, highs, lows, volumes,
                    ema20a, ema50a, ema200a, rsia, atr5a, atr14a):
    n = len(closes)
    last = n - 1
    streaks = {s: 0 for s in STRATEGY_META}
    broken = {s: False for s in STRATEGY_META}
    start = max(0, last - STREAK_LOOKBACK)
    for idx in range(last, start - 1, -1):
        ev = eval_strategies(idx, closes, highs, lows, volumes,
                             ema20a, ema50a, ema200a, rsia, atr5a, atr14a)
        if ev is None:
            break
        for s in STRATEGY_META:
            if not broken[s]:
                if ev[s]["ready"]:
                    streaks[s] += 1
                else:
                    broken[s] = True
    return streaks


# ─────────────────────────────────────────────────────────────
# 攞數據
# ─────────────────────────────────────────────────────────────
def fetch_history(ticker):
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval={INTERVAL}&range={HISTORY_RANGE}")
        try:
            resp = requests.get(url, headers=YF_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            result = data.get("chart", {}).get("result")
            if not result:
                continue
            r0 = result[0]
            q = r0["indicators"]["quote"][0]
            raw_t = r0.get("timestamp", [])
            raw_o = q.get("open", [])
            raw_c = q.get("close", [])
            raw_h = q.get("high", [])
            raw_l = q.get("low", [])
            raw_v = q.get("volume", [])
            closes, highs, lows, vols, times, opens = [], [], [], [], [], []
            for i in range(len(raw_c)):
                if None in (raw_c[i], raw_h[i], raw_l[i], raw_v[i]):
                    continue
                closes.append(raw_c[i])
                highs.append(raw_h[i])
                lows.append(raw_l[i])
                vols.append(raw_v[i])
                times.append(raw_t[i] if i < len(raw_t) else 0)
                opens.append(raw_o[i] if (i < len(raw_o) and raw_o[i] is not None) else raw_c[i])
            if len(closes) < 64:
                return None
            return {"close": closes, "high": highs, "low": lows, "volume": vols, "time": times, "open": opens}
        except Exception as e:
            continue
    return None


def get_sp500_tickers():
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=15)
        if resp.status_code == 200:
            import re
            rows = re.findall(r'<td><a[^>]*>([A-Z][A-Z.\-]{0,6})</a>', resp.text)
            tickers = [t.replace(".", "-") for t in rows]
            seen = set()
            out = [t for t in tickers if not (t in seen or seen.add(t))]
            if len(out) > 400:
                return out
    except:
        pass
    try:
        with open("tickers.txt") as f:
            return [ln.strip().upper() for ln in f if ln.strip()]
    except FileNotFoundError:
        return ["AAPL", "MSFT", "NVDA", "AMD", "PLTR", "CRWD", "AVGO", "META", "TSLA"]


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def build_record(ticker, hist):
    closes = hist["close"]
    highs = hist["high"]
    lows = hist["low"]
    volumes = hist["volume"]

    ema20a = ema(closes, 20)
    ema50a = ema(closes, 50)
    ema200a = ema(closes, 200)
    rsia = rsi(closes, 14)
    atr5a = atr(highs, lows, closes, 5)
    atr14a = atr(highs, lows, closes, 14)

    last = len(closes) - 1
    today = eval_strategies(last, closes, highs, lows, volumes,
                            ema20a, ema50a, ema200a, rsia, atr5a, atr14a)
    if today is None:
        return None
    streaks = compute_streaks(closes, highs, lows, volumes,
                              ema20a, ema50a, ema200a, rsia, atr5a, atr14a)

    strategies = {}
    for s in STRATEGY_META:
        strategies[s] = {
            "score": today[s]["score"],
            "bonusScore": today[s]["bonusScore"],
            "ready": today[s]["ready"],
            "streak": streaks[s],
            "conds": today[s]["conds"],
            "bonus": today[s]["bonus"],
            "keyvals": today[s]["keyvals"],
        }
        # S1 假突破偵測（參考用）
        if s == "S1":
            db = today[s].get("daysBelow20", 0)
            dr = today[s].get("daysRecover", 0)
            strategies[s]["daysBelow20"] = db
            strategies[s]["daysRecover"] = dr
            # 跌穿前 S1 連續 5/5 日數（s1_ready streak before the dip）
            streak_before = 0
            if db > 0:
                dip_start = last - dr - db + 1   # 跌穿段第一日
                j = dip_start - 1                # 跌穿前一日
                guard = 0
                while j >= 63 and guard < STREAK_LOOKBACK:
                    ev = eval_strategies(j, closes, highs, lows, volumes,
                                         ema20a, ema50a, ema200a, rsia, atr5a, atr14a)
                    if ev is None or not ev["S1"]["ready"]:
                        break
                    streak_before += 1
                    j -= 1
                    guard += 1
            strategies[s]["streakBefore"] = streak_before

    # K 線 + EMA 圖數據（最近 120 根，畀 app 內畫圖）
    times = hist.get("time", [])
    opens = hist.get("open", closes)
    CB = 120
    start = max(0, last - CB + 1)
    chart = {
        "t": [times[i] if i < len(times) else 0 for i in range(start, last + 1)],
        "o": [round(opens[i], 2) for i in range(start, last + 1)],
        "h": [round(highs[i], 2) for i in range(start, last + 1)],
        "l": [round(lows[i], 2) for i in range(start, last + 1)],
        "c": [round(closes[i], 2) for i in range(start, last + 1)],
        "e20": [round(ema20a[i], 2) if ema20a[i] is not None else None for i in range(start, last + 1)],
        "e50": [round(ema50a[i], 2) if ema50a[i] is not None else None for i in range(start, last + 1)],
        "e200": [round(ema200a[i], 2) if ema200a[i] is not None else None for i in range(start, last + 1)],
    }

    return {
        "ticker": ticker,
        "close": round(closes[last], 2),
        "chgPct": round(pct_change(closes, last, 1) or 0, 2),
        "ema20": round(ema20a[last], 2),
        "ema50": round(ema50a[last], 2),
        "ema200": round(ema200a[last], 2),
        "rsi": round(rsia[last], 1),
        "strategies": strategies,
        "chart": chart,
    }


def load_previous_ready():
    """讀返上次 data.json 入面每個策略 ready 嘅 list，用嚟比較邊隻係新入。"""
    prev = {s: set() for s in STRATEGY_META}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        for s in STRATEGY_META:
            tickers = old.get("summary", {}).get(s, {}).get("tickers", [])
            prev[s] = set(tickers)
        print(f"讀到上次 list（S1 上次 {len(prev['S1'])} 隻）")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        print("冇上次 data.json（第一次跑），全部唔標 NEW")
    return prev


def main():
    tickers = get_sp500_tickers()
    print(f"掃描 {len(tickers)} 隻股…")

    prev_ready = load_previous_ready()

    records = []
    ok = 0
    for i, t in enumerate(tickers, 1):
        hist = fetch_history(t)
        if hist:
            rec = build_record(t, hist)
            if rec:
                records.append(rec)
                ok += 1
        if i % 25 == 0:
            print(f"  {i}/{len(tickers)} … (成功 {ok})")
        time.sleep(REQUEST_SLEEP)

    summary = {}
    for s in STRATEGY_META:
        ready = [r["ticker"] for r in records if r["strategies"][s]["ready"]]
        summary[s] = {"count": len(ready), "tickers": ready}

    # 標記每隻股每個策略係咪「新入」（上次唔 ready，今次 ready）
    for r in records:
        for s in STRATEGY_META:
            is_ready_now = r["strategies"][s]["ready"]
            was_ready = r["ticker"] in prev_ready[s]
            r["strategies"][s]["isNew"] = bool(is_ready_now and not was_ready)

    # 把 K 線圖數據抽出嚟，寫去 charts.json（keep data.json 細，app 開圖先 load）
    charts = {}
    for r in records:
        ch = r.pop("chart", None)
        if ch is not None:
            charts[r["ticker"]] = ch

    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "strategyMeta": STRATEGY_META,
        "summary": summary,
        "stocks": records,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    with open("charts.json", "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✅ 寫好 {OUTPUT_FILE} + charts.json（{len(records)} 隻成功）")
    for s in STRATEGY_META:
        print(f"  {s} {STRATEGY_META[s]['name']}: {summary[s]['count']} 隻 ready")


if __name__ == "__main__":
    main()
