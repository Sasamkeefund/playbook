#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Market Overview 數據層 —— 只掃 SPY / QQQ / ^VIX
================================================
朝早每 30 分鐘跑（GitHub Actions），寫 market.json。
判斷邏輯照搬 S1 Google Sheets 嘅 pre-market check。

用 chart API + includePrePost=true，攞最新成交價（包括 pre-market），
唔使 quote endpoint（嗰個要 crumb，唔穩）。

只需要：pip install requests
"""

import json
from datetime import datetime, timezone
import requests

OUTPUT_FILE = "market.json"
TICKERS = ["SPY", "QQQ", "^VIX"]

YF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}


def fetch_quote(ticker):
    """攞最新價（含 pre/post）+ 前收市。返回 dict 或 None。"""
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=5m&range=1d&includePrePost=true")
        try:
            resp = requests.get(url, headers=YF_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            result = data.get("chart", {}).get("result")
            if not result:
                continue
            r0 = result[0]
            meta = r0.get("meta", {})
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            reg_price = meta.get("regularMarketPrice")

            # 最新成交價：由 quote.close 攞最後一個非 null（包括 pre-market bar）
            closes = r0.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            last_price = None
            for c in reversed(closes):
                if c is not None:
                    last_price = c
                    break
            if last_price is None:
                last_price = reg_price

            if last_price is None or prev_close is None or prev_close == 0:
                continue

            chg_pct = (last_price - prev_close) / prev_close * 100

            # 判斷而家係咪 pre-market（regular 未開）
            ctp = meta.get("currentTradingPeriod", {})
            now = datetime.now(timezone.utc).timestamp()
            reg_start = ctp.get("regular", {}).get("start")
            is_premarket = bool(reg_start and now < reg_start)

            return {
                "price": round(last_price, 2),
                "prevClose": round(prev_close, 2),
                "chgPct": round(chg_pct, 2),
                "isPremarket": is_premarket,
            }
        except Exception:
            continue
    return None


def build_verdict(spy, qqq, vix):
    """照搬 S1 Google Sheets 嘅大市判斷。"""
    if not spy or not qqq:
        return {"text": "— 攞唔到大市數據", "color": "amber"}

    avg = (spy["chgPct"] + qqq["chgPct"]) / 2
    premarket = spy.get("isPremarket") or qqq.get("isPremarket")
    tag = "Pre-market" if premarket else "大市"

    if avg < -1.5:
        v = {"text": f"🔴 {tag}大跌，所有 Limit Order 要小心！", "color": "red"}
    elif avg < -0.3:
        v = {"text": f"⚠️ {tag}輕微跌，留意個別股走勢", "color": "amber"}
    elif avg <= 0.5:
        v = {"text": f"— {tag}中性，Limit Order 正常", "color": "neutral"}
    else:
        v = {"text": f"✅ {tag}偏好，唔影響 Limit Order", "color": "green"}

    # VIX 疊加警告
    if vix:
        vlevel = vix["price"]
        if vlevel >= 30:
            v["vix"] = f"🔴 VIX {vlevel} — 市場恐慌，極度小心"
        elif vlevel >= 25:
            v["vix"] = f"⚠️ VIX {vlevel} — 波動偏高"
        elif vlevel >= 20:
            v["vix"] = f"VIX {vlevel} — 波動中性偏高"
        else:
            v["vix"] = f"✅ VIX {vlevel} — 市場平靜"
    v["avg"] = round(avg, 2)
    return v


def main():
    quotes = {}
    for t in TICKERS:
        quotes[t] = fetch_quote(t)
        print(f"{t}: {quotes[t]}")

    spy, qqq, vix = quotes["SPY"], quotes["QQQ"], quotes["^VIX"]
    verdict = build_verdict(spy, qqq, vix)

    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "spy": spy,
        "qqq": qqq,
        "vix": vix,
        "verdict": verdict,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✅ 寫好 {OUTPUT_FILE}")
    print(f"   判斷：{verdict['text']}")
    if verdict.get("vix"):
        print(f"   {verdict['vix']}")


if __name__ == "__main__":
    main()
