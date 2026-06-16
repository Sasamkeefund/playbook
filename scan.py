#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投機十步曲 · S1-S6 Scanner（夜晚 GitHub Actions 跑）
====================================================
解決舊 app 兩個問題：
  1. 攞 data 唔穩 —— 全部喺 server 度一次過攞、計、寫 data.json，前端唔使自己上網
  2. 同 TradingView 唔同 —— 攞 5 年 history + Wilder RSI/ATR + 收市後跑

條件完全對齊 Playbook_Scanner_v2.pine。
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
HISTORY_RANGE = "5y"      # 5 年 → EMA200 完全收斂，貼近 TradingView
INTERVAL = "1d"
STREAK_LOOKBACK = 60      # 計連續日數最多睇返 60 個交易日
REQUEST_SLEEP = 0.25      # 每隻股之間停一陣，避免被 Yahoo 限流
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

# 六個策略 metadata（前端用）
STRATEGY_META = {
    "S1": {"name": "順勢交易",  "dir": "Long",       "live": True,  "thresh": "ALL 5/5"},
    "S2": {"name": "趨勢終結",  "dir": "Short",      "live": False, "thresh": "4/5+"},
    "S3": {"name": "突破交易",  "dir": "Long",       "live": True,  "thresh": "4/5+"},
    "S4": {"name": "假突破",    "dir": "Long/Short", "live": False, "thresh": "3/4+"},
    "S5": {"name": "支持阻力",  "dir": "Long",       "live": True,  "thresh": "ALL 4/4"},
    "S6": {"name": "圖表形態",  "dir": "Long",       "live": True,  "thresh": "4/5+"},
}


# ─────────────────────────────────────────────────────────────
# 技術指標（對齊 TradingView）
# ─────────────────────────────────────────────────────────────
def ema(values, length):
    """EMA，第一個值用 source 起手（同 Pine ta.ema 一致）。返回同長度 array，warmup 處 None。"""
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
    """Wilder smoothing（Pine ta.rma），用頭 length 個嘅 SMA 起手。"""
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
    """Wilder RSI（對齊 TradingView ta.rsi）。"""
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
    # avg_gain/avg_loss 對應 index i+1
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
    """Wilder ATR（對齊 TradingView ta.atr）。"""
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
    """end_idx（含）往前 length 個嘅平均；唔夠數返 None。"""
    if end_idx + 1 < length:
        return None
    window = values[end_idx - length + 1: end_idx + 1]
    return sum(window) / length


def highest(values, end_idx, length):
    if end_idx + 1 < length:
        return None
    return max(values[end_idx - length + 1: end_idx + 1])


def pct_change(values, idx, lookback):
    """values[idx] / values[idx-lookback] - 1，× 100。"""
    if idx - lookback < 0:
        return None
    base = values[idx - lookback]
    if base == 0:
        return None
    return (values[idx] / base - 1) * 100


# ─────────────────────────────────────────────────────────────
# 策略條件（逐個 bar 計，方便計連續日數）
# 完全對齊 Playbook_Scanner_v2.pine
# ─────────────────────────────────────────────────────────────
def eval_strategies(idx, closes, highs, lows, volumes,
                    ema20a, ema50a, ema200a, rsia, atr5a, atr14a):
    """返回 {S1:{...}, ...}，每個有 conds / score / ready / keyvals。
    缺數據返 None。"""
    e20, e50, e200 = ema20a[idx], ema50a[idx], ema200a[idx]
    r = rsia[idx]
    if None in (e20, e50, e200, r):
        return None
    if idx < 63:  # perf3m 要 63 bars
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

    res = {}

    # ── S1 順勢交易（5/5）──
    c = [
        close > e20,
        close > e50,
        (ema200_slope is not None and close > e200 and ema200_slope > 0),
        (40 <= r <= 70),
        (perf3m is not None and perf1m is not None and perf3m > 5 and perf1m < 25),
    ]
    res["S1"] = {"conds": c, "score": sum(c), "ready": sum(c) == 5,
                 "keyvals": {"RSI": round(r, 1),
                             "1W%": round(perf1w, 1) if perf1w is not None else None,
                             "EMA200slope": "↑" if (ema200_slope or 0) > 0 else "↓"}}

    # ── S2 趨勢終結（4/5）──
    c = [
        close < e50,
        close < e200,
        (perf1d is not None and perf1d < -5),
        (relvol is not None and relvol > 2.0),
        (r < 35),
    ]
    res["S2"] = {"conds": c, "score": sum(c), "ready": sum(c) >= 4,
                 "keyvals": {"RSI": round(r, 1),
                             "1D%": round(perf1d, 1) if perf1d is not None else None,
                             "RelVol": round(relvol, 2) if relvol is not None else None}}

    # ── S3 突破交易（4/5）──
    c = [
        (pct_from_high is not None and pct_from_high <= 4),
        close > e50,
        ((relvol is not None and relvol < 0.75) or (vol5 is not None and vol20 is not None and vol5 < vol20)),
        (a5 is not None and a14 is not None and a5 < a14),
        (perf3m is not None and perf3m > 10),
    ]
    res["S3"] = {"conds": c, "score": sum(c), "ready": sum(c) >= 4,
                 "keyvals": {"距52W高%": round(-pct_from_high, 1) if pct_from_high is not None else None,
                             "3M%": round(perf3m, 1) if perf3m is not None else None,
                             "RelVol": round(relvol, 2) if relvol is not None else None}}

    # ── S4 假突破（3/4）──
    c = [
        (pct_from_high is not None and pct_from_high <= 8),
        (45 <= r <= 65),
        (perf1w is not None and -4 <= perf1w <= 2),
        (perf1m is not None and abs(perf1m) < 8),
    ]
    res["S4"] = {"conds": c, "score": sum(c), "ready": sum(c) >= 3,
                 "keyvals": {"RSI": round(r, 1),
                             "1W%": round(perf1w, 1) if perf1w is not None else None,
                             "1M%": round(perf1m, 1) if perf1m is not None else None}}

    # ── S5 支持阻力（4/4）──
    c = [
        close > e50,
        close > e200,
        (30 <= r <= 55),
        (perf1m is not None and -20 <= perf1m <= -5),
    ]
    res["S5"] = {"conds": c, "score": sum(c), "ready": sum(c) == 4,
                 "keyvals": {"RSI": round(r, 1),
                             "1M%": round(perf1m, 1) if perf1m is not None else None}}

    # ── S6 圖表形態（4/5）──
    c = [
        close > e20,
        close > e50,
        (perf1m is not None and perf1m > 8),
        (perf1w is not None and -5 <= perf1w <= 0),
        (relvol is not None and relvol < 0.8),
    ]
    res["S6"] = {"conds": c, "score": sum(c), "ready": sum(c) >= 4,
                 "keyvals": {"1M%": round(perf1m, 1) if perf1m is not None else None,
                             "1W%": round(perf1w, 1) if perf1w is not None else None,
                             "RelVol": round(relvol, 2) if relvol is not None else None}}

    return res


def compute_streaks(closes, highs, lows, volumes,
                    ema20a, ema50a, ema200a, rsia, atr5a, atr14a):
    """由最後一個 bar 往前數，每個策略連續幾多日 ready。"""
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
    """攞 5 年日線。返回 dict(close/high/low/volume) 或 None。"""
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
            raw_c = q.get("close", [])
            raw_h = q.get("high", [])
            raw_l = q.get("low", [])
            raw_v = q.get("volume", [])
            meta = r0.get("meta", {})
            closes, highs, lows, vols = [], [], [], []
            for i in range(len(raw_c)):
                if None in (raw_c[i], raw_h[i], raw_l[i], raw_v[i]):
                    continue
                closes.append(raw_c[i])
                highs.append(raw_h[i])
                lows.append(raw_l[i])
                vols.append(raw_v[i])
            if len(closes) < 64:
                return None
            return {"close": closes, "high": highs, "low": lows,
                    "volume": vols, "meta": meta}
        except Exception as e:
            print(f"  ! {ticker} {host} error: {e}", file=sys.stderr)
            continue
    return None


def get_sp500_tickers():
    """攞 S&P 500 成份股。先試 Wikipedia，失敗就用內置 fallback。"""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=15)
        if resp.status_code == 200:
            import re
            # 抓 table 入面嘅 ticker（第一欄）
            rows = re.findall(r'<td><a[^>]*>([A-Z][A-Z.\-]{0,6})</a>', resp.text)
            tickers = [t.replace(".", "-") for t in rows]
            # 去重保序
            seen = set()
            out = [t for t in tickers if not (t in seen or seen.add(t))]
            if len(out) > 400:
                return out
    except Exception as e:
        print(f"Wikipedia fetch failed: {e}", file=sys.stderr)
    # fallback：可放你自己嘅 tickers.txt
    try:
        with open("tickers.txt") as f:
            return [ln.strip().upper() for ln in f if ln.strip()]
    except FileNotFoundError:
        print("冇 tickers.txt，用細 demo list", file=sys.stderr)
        return ["AAPL", "MSFT", "NVDA", "AMD", "PLTR", "CRWD", "AVGO", "META", "TSLA"]


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def build_record(ticker, hist):
    closes = hist["close"]; highs = hist["high"]
    lows = hist["low"]; volumes = hist["volume"]

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
            "ready": today[s]["ready"],
            "streak": streaks[s],
            "conds": today[s]["conds"],
            "keyvals": today[s]["keyvals"],
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
    }


def main():
    tickers = get_sp500_tickers()
    print(f"掃描 {len(tickers)} 隻股…")

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

    # 每個策略 ready 嘅清單（前端方便用）
    summary = {}
    for s in STRATEGY_META:
        ready = [r["ticker"] for r in records if r["strategies"][s]["ready"]]
        summary[s] = {"count": len(ready), "tickers": ready}

    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "strategyMeta": STRATEGY_META,
        "summary": summary,
        "stocks": records,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✅ 寫好 {OUTPUT_FILE}（{len(records)} 隻成功）")
    for s in STRATEGY_META:
        print(f"  {s} {STRATEGY_META[s]['name']}: {summary[s]['count']} 隻 ready")


if __name__ == "__main__":
    main()
