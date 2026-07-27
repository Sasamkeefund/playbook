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
    "S1V1": {"name": "順勢交易(原版·前頂)", "dir": "Long", "live": True, "reqMax": 4, "bonusMax": 3},
    "S2": {"name": "趨勢終結",  "dir": "Short",      "live": False, "reqMax": 5, "bonusMax": 5},
    "S3": {"name": "突破交易",  "dir": "Long",       "live": True,  "reqMax": 5, "bonusMax": 5},
    "S4": {"name": "假突破",    "dir": "Long/Short", "live": False, "reqMax": 4, "bonusMax": 4},
    "S5": {"name": "支持阻力",  "dir": "Long",       "live": True,  "reqMax": 4, "bonusMax": 4},
    "S6": {"name": "圖表形態",  "dir": "Long",       "live": True,  "reqMax": 5, "bonusMax": 5},
    "S7": {"name": "52週新高動能", "dir": "Long",    "live": True,  "reqMax": 5, "bonusMax": 5},
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
# 相對強度線（股價 / SPY），逐隻股 eval 前由外部設定；None = 唔計 RS（自動過）
_CUR_RS_LINE = None
_CUR_TIMES = None  # 當前股票嘅 bar 時間，計大盤相對升幅用

def rs_strong(idx, lookback=126, buf=0.97):
    """RS 線接近自己 lookback 期內高位 = 跑贏大盤（Minervini/J Law RS）。"""
    rl = _CUR_RS_LINE
    if rl is None or idx >= len(rl) or rl[idx] is None:
        return None  # 冇 SPY 數據時返 None（當條件 skip）
    start = max(0, idx - lookback)
    seg = [v for v in rl[start:idx + 1] if v is not None]
    if len(seg) < 20:
        return None
    return rl[idx] >= max(seg) * buf


def zigzag_pivots(highs, lows, min_pct=3.0):
    """標準 ZigZag：一定要跌／升夠 min_pct% 先確認轉勢，先算一個 pivot。
    Return: list of (idx, price, 'H'|'L')，按時間順序。
    呢個保證任何兩個連續嘅 'H' pivot 之間，一定有真正嘅回調（唔會好似前頂升少少即刻又破頂咁誤判）。"""
    n = len(highs)
    pivots = []
    if n < 3:
        return pivots
    ext_type = 'H'
    ext_idx, ext_val = 0, highs[0]
    for i in range(1, n):
        if ext_type == 'H':
            if highs[i] >= ext_val:
                ext_val, ext_idx = highs[i], i
            elif ext_val and (ext_val - lows[i]) / ext_val * 100 >= min_pct:
                pivots.append((ext_idx, ext_val, 'H'))
                ext_type, ext_val, ext_idx = 'L', lows[i], i
        else:
            if lows[i] <= ext_val:
                ext_val, ext_idx = lows[i], i
            elif ext_val and (highs[i] - ext_val) / ext_val * 100 >= min_pct:
                pivots.append((ext_idx, ext_val, 'L'))
                ext_type, ext_val, ext_idx = 'H', highs[i], i
    pivots.append((ext_idx, ext_val, ext_type))
    return pivots


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

    # ── S1 回調形態偵測（用收市 close 跌穿 EMA20，唔睇下影線）──
    # pullbackTouch = 過去 6 日內，有冇一日 close 收市跌穿 EMA20（夠深，加 0.3% buffer 減少鋸齒誤判）
    # pullbackDaysAgo = 最近一次收市跌穿 EMA20 係幾多日前（今日=0）
    # aboveNow = 今日 close 企返 EMA20 上
    pullback_touch = False
    pullback_days_ago = -1
    above_now = (e20 is not None and close >= e20)
    if e20 is not None:
        for back in range(0, 6):
            k = idx - back
            if k < 0:
                break
            if ema20a[k] is None:
                continue
            # 收市 close 跌穿 EMA20（低過 0.3% buffer 先算，避免線上下鋸齒）
            if closes[k] < ema20a[k] * 0.997:
                pullback_touch = True
                if pullback_days_ago < 0:
                    pullback_days_ago = back
    res["S1"]["pullbackTouch"] = pullback_touch
    res["S1"]["pullbackDaysAgo"] = pullback_days_ago
    res["S1"]["aboveNow"] = above_now

    # ── S1V1 原版（前頂支撐版，非 EMA20）──
    # 入場邏輯：Wave1 頂（前頂）→ Wave2 突破前頂（適中距離）→ 略跌穿前頂（假突破）→ 急速反彈返上前頂＝入場
    # R1 過度延伸／R2 突破距離／R3 假突破蠟燭質素／R4 急速反彈 = Required 4項
    # b1 回調快／b2 回調縮量／b3 第2浪速度≥第1浪 = Bonus 3項
    # 關鍵：「反彈」一定要新鮮（今日或前2日內），否則就係舊聞，唔可以話「而家可以入場」
    v1 = {"ready": False, "score": 0, "bonusScore": 0, "conds": [False, False, False, False],
          "bonus": [False, False, False], "keyvals": {}, "wave1Top": None, "wave2Top": None, "breachLow": None,
          "breakoutDistPct": None, "daysToBreach": None, "daysRecover": None, "daysSinceRecover": None, "patternType": None, "cleanLevel": None, "betweenPivots": None,
          "aboveNow": None, "entry": None, "stop": None, "t1": None, "t2": None}
    try:
        win = 70
        lo_bound = max(0, idx - win)
        seg_h, seg_l, seg_c, seg_v = highs[lo_bound:idx+1], lows[lo_bound:idx+1], closes[lo_bound:idx+1], volumes[lo_bound:idx+1]
        n = len(seg_h)
        if n >= 30:
            # 揾晒窗口入面嘅真正 swing high（ZigZag：一定要回調夠 4% 先確認個頂，
            # 避免好似單日插針咁嘅雜訊被當做前頂 —— 用真實數據 backtest 過先定呢個門檻）
            zz = zigzag_pivots(seg_h, seg_l, min_pct=4.0)
            piv = [i for i, price, typ in zz if typ == 'H']
            zz_idx_all = [i for i, price, typ in zz]  # 全部 pivot（H+L）嘅位置，用嚟計「乾淨度」
            # 揾合資格嘅 (Wave1頂, Wave2頂) 配對：Wave2>Wave1，突破距離2-15%（ZigZag已經確保中間有回調）
            # 再要求：跌穿前頂之後嘅反彈，一定要「新鮮」（反彈完成嗰日 = 今日或前2日內），
            #        揀當中反彈最新鮮嗰一對（唔係揀 wave2 最遲嗰對 —— 舊 setup 就算 wave2 好遲都唔算數）
            best = None
            for a in range(len(piv)):
                for bpos in range(a+1, len(piv)):
                    i1, i2 = piv[a], piv[bpos]
                    p1, p2 = seg_h[i1], seg_h[i2]
                    if p2 <= p1:
                        continue
                    dist_pct = (p2 - p1) / p1 * 100
                    if not (2 <= dist_pct <= 15):
                        continue
                    between_low = min(seg_l[i1:i2+1]) if i2 > i1 else p1
                    if (p1 - between_low) / p1 * 100 < 1.5:
                        continue
                    post_l, post_c = seg_l[i2+1:], seg_c[i2+1:]
                    breach_pos = next((i for i, l in enumerate(post_l) if l <= p1), None)
                    if breach_pos is None:
                        continue
                    recover_i = next((i for i in range(breach_pos, len(post_c)) if post_c[i] > p1), None)
                    if recover_i is None:
                        continue
                    recovery_abs_idx = i2 + 1 + recover_i   # 反彈完成嗰日，喺 seg 入面嘅絕對位置
                    days_since_recover = (n - 1) - recovery_abs_idx
                    if days_since_recover < 0 or days_since_recover > 2:
                        continue  # 唔夠新鮮（唔係今日或前2日內反彈），skip
                    if best is None or recovery_abs_idx > best["recovery_abs_idx"]:
                        best = {"i1": i1, "i2": i2, "breach_pos": breach_pos, "recover_i": recover_i,
                                "recovery_abs_idx": recovery_abs_idx, "days_since_recover": days_since_recover}
            if best:
                wave1_idx, wave2_idx = best["i1"], best["i2"]
                wave1_top, wave2_top = seg_h[wave1_idx], seg_h[wave2_idx]
                breakout_dist_pct = (wave2_top - wave1_top) / wave1_top * 100
                # 乾淨度：前頂到Wave2之間，中間插咗幾多個額外pivot（H+L）。
                # 教科書式「一浪清楚衝上→回調→再一浪清楚衝上」應該淨係得1個（果段回調嘅低位），
                # 插得越多，即係中間嗰段越chaotic、嗰個「前頂」對市場嚟講可能冇乜實質阻力意義
                between_pivots = sum(1 for x in zz_idx_all if wave1_idx < x < wave2_idx)
                clean_level = "clean" if between_pivots <= 1 else ("ok" if between_pivots <= 3 else "messy")
                wave1_low = min(seg_l[max(0, wave1_idx-15):wave1_idx+1])
                wave1_days = wave1_idx - seg_l[max(0, wave1_idx-15):wave1_idx+1].index(wave1_low) if wave1_idx > 0 else 1
                wave1_days = max(wave1_days, 1)
                wave1_speed = ((wave1_top - wave1_low) / wave1_low * 100 / wave1_days) if wave1_low else 0
                wave2_days = max(wave2_idx - wave1_idx, 1)
                wave2_speed = breakout_dist_pct / wave2_days
                post_l = seg_l[wave2_idx+1:]
                post_c = seg_c[wave2_idx+1:]
                post_h = seg_h[wave2_idx+1:]
                post_v = seg_v[wave2_idx+1:]
                breach_pos = best["breach_pos"]
                recover_i = best["recover_i"]
                days_recover_v1 = recover_i - breach_pos
                days_since_recover = best["days_since_recover"]
                breach_low = min(post_l[breach_pos:breach_pos+3])  # 跌穿後3日內嘅最低（防單日插針）
                today_above = close > wave1_top
                # Wave2頂 到 跌穿前頂 隔咗幾多日：分類但唔篩走（未有數據判斷邊種好，兩種都留低畀你儲經驗）
                # ≤20日 = 短線（跟checklist原本嘅緊湊節奏）；>20日 = 長線支撐回試（舊阻力位變支撐，另一種形態）
                pattern_type = "短線" if breach_pos <= 20 else "長線支撐回試"
                # ── Required ──
                # R1：Wave2 攀升段有冇過度延伸（10日升幅極端 + 天量）
                look = min(10, wave2_days)
                seg10_c = seg_c[max(0, wave2_idx-look+1):wave2_idx+1]
                seg10_v = seg_v[max(0, wave2_idx-look+1):wave2_idx+1]
                run10 = (seg10_c[-1] - seg10_c[0]) / seg10_c[0] * 100 if len(seg10_c) >= 2 and seg10_c[0] else 0
                vol_avg = sma_at(volumes, idx, 60) or 1
                vol_spike = any(v > vol_avg * 2.5 for v in seg10_v) if seg10_v else False
                r1_ok = not (run10 > 40 and vol_spike)
                # R2：突破距離適中（2%~15%，篩選 pivot pair 嗰陣已經確保）
                r2_ok = True
                # R3：假突破蠟燭質素（breach 嗰日：細蠋身 或 長下影線）
                r3_ok = False
                if breach_pos < len(post_h):
                    bh, bl, bc = post_h[breach_pos], post_l[breach_pos], post_c[breach_pos]
                    prior_i = wave2_idx + breach_pos
                    bo = seg_c[prior_i] if 0 <= prior_i < len(seg_c) else bc
                    day_range = bh - bl
                    body = abs(bc - bo)
                    lower_wick = min(bo, bc) - bl
                    r3_ok = day_range > 0 and (body / day_range < 0.35 or lower_wick / day_range > 0.4)
                # R4：急速反彈返上前頂（3日內收返上，而且要今日/前2日先啱入場窗口）
                r4_ok = days_recover_v1 <= 3 and days_since_recover <= 2
                conds = [r1_ok, r2_ok, r3_ok, r4_ok]
                # ── Bonus ──
                b1_ok = breach_pos <= 5  # 回調快（Wave2頂到跌穿前頂，≤5日）
                pullback_vol = (sum(post_v[:breach_pos+1]) / len(post_v[:breach_pos+1])) if breach_pos >= 0 and post_v[:breach_pos+1] else None
                b2_ok = (pullback_vol is not None and vol_avg and pullback_vol < vol_avg * 0.8)
                b3_ok = wave2_speed >= wave1_speed
                bonus = [b1_ok, b2_ok, b3_ok]
                score = sum(conds)
                v1.update({
                    "ready": score == 4, "score": score, "bonusScore": sum(bonus),
                    "conds": conds, "bonus": bonus,
                    "wave1Top": round(wave1_top, 2), "wave2Top": round(wave2_top, 2),
                    "breachLow": round(breach_low, 2), "breakoutDistPct": round(breakout_dist_pct, 1),
                    "daysToBreach": breach_pos, "daysRecover": days_recover_v1, "patternType": pattern_type,
                    "cleanLevel": clean_level, "betweenPivots": between_pivots,
                    "daysSinceRecover": days_since_recover, "aboveNow": today_above,
                    "entry": round(wave1_top * 1.003, 2), "stop": round(breach_low, 2),
                    "t1": round(wave2_top, 2),
                    "t2": round(wave1_top * 1.003 + (wave2_top - breach_low) * 1.618, 2),
                })
    except Exception:
        pass
    res["S1V1"] = v1

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

    # ── S6 旗形 H1 + 突破偵測（approximate，參考用，TradingView 畫返準）──
    # 邏輯：旗杆 = 近期高位（旗杆頂）；整固區 = 旗杆頂之後嘅橫行；
    #       H1 ≈ 整固區內嘅最高收市（旗形上邊界 = 突破位）
    #       突破 = 今日 close 升穿 H1
    h1 = None
    pct_to_h1 = None
    broke_h1 = False
    flag_low = None
    flag_retrace = None   # 旗形回調佔上升推進浪幾多（Patreon: 要 ≤ 0.236）
    try:
        # 旗杆頂 = 近 20 日內最高 high 嘅位置（approximate impulse top）
        win = 20
        seg_c = closes[max(0, idx-win):idx+1]
        seg_h = highs[max(0, idx-win):idx+1]
        seg_l = lows[max(0, idx-win):idx+1]
        base = max(0, idx-win)
        if len(seg_c) >= 8:
            pole_top = max(seg_h[:-1])               # 旗杆頂（唔計今日）
            pole_idx = seg_h.index(pole_top)         # 喺 segment 內位置
            # 旗杆底 = 旗杆頂之前嘅最低 low（上升推進浪起點）
            pole_low = min(seg_l[:pole_idx+1]) if pole_idx >= 1 else seg_l[0]
            # 整固區 = 旗杆頂之後嘅 bars（旗形喺旗杆後形成）
            consol_h = seg_h[pole_idx:]
            consol_l = seg_l[pole_idx:]
            if len(consol_h) >= 2:
                h1 = max(consol_h[:-1]) if len(consol_h) > 1 else pole_top
                flag_low = min(consol_l) if consol_l else None
                pct_to_h1 = (close - h1) / h1 * 100
                broke_h1 = close > h1 and relvol is not None and relvol > 1.3
                # 回調比例 = (旗杆頂 - 旗形低) / (旗杆頂 - 旗杆底)
                pole_height = pole_top - pole_low
                if pole_height > 0 and flag_low is not None:
                    flag_retrace = (pole_top - flag_low) / pole_height
    except Exception:
        pass
    res["S6"]["h1"] = round(h1, 2) if h1 else None
    res["S6"]["flagLow"] = round(flag_low, 2) if flag_low else None
    res["S6"]["pctToH1"] = round(pct_to_h1, 1) if pct_to_h1 is not None else None
    res["S6"]["brokeH1"] = broke_h1
    res["S6"]["flagRetrace"] = round(flag_retrace, 3) if flag_retrace is not None else None

    # ── S7 J Law / Minervini VCP 整固突破 ──
    # 強勢股（Stage 2 + 跑贏大盤）正喺度 VCP 整固（橫行收窄），等突破。
    # 注意：唔再用「距52週高≤2%」—— 整固股會距高有少少（回落整固中）。
    today_high = highs[idx] if idx < len(highs) else None
    sma10 = sma_at(closes, idx, 10)
    sma20 = sma_at(closes, idx, 20)
    # J Law 相對強度：股票1個月升幅 ≥ 大盤(SPY)1個月升幅 × 2（跑贏1倍以上）
    bar_t = _CUR_TIMES[idx] if (_CUR_TIMES is not None and idx < len(_CUR_TIMES)) else None
    spy_1m = spy_perf(bar_t, 30) if bar_t else None
    if spy_1m is None or perf1m is None:
        rs_jl = None
    elif spy_1m > 0:
        rs_jl = (perf1m > 0 and perf1m >= 2 * spy_1m)
    else:
        rs_jl = (perf1m > 0)
    rs_pass = (rs_jl is True) or (rs_jl is None)

    # VCP 整固偵測（喺定義條件之前計）
    consol_days = None    # 喺窄區間橫行幾多日
    vol_contract = None   # 波動收窄
    try:
        win = closes[max(0, idx-50):idx+1]
        if len(win) >= 15:
            ref = win[-1]
            cnt = 0
            for k in range(len(win)-1, -1, -1):
                if abs(win[k] - ref) / ref <= 0.10:   # ±10% 窄區（整固）
                    cnt += 1
                else:
                    break
            consol_days = cnt
        if atr14a[idx] is not None and idx >= 25:
            recent_atr = sum(a for a in atr14a[idx-9:idx+1] if a is not None) / 10
            prior_atr = sum(a for a in atr14a[idx-19:idx-9] if a is not None) / 10
            if prior_atr > 0:
                vol_contract = recent_atr < prior_atr
    except Exception:
        pass

    # 距高放寬到 ≤15%（整固股通常距高 5-15%，唔貼住新高）
    near_high = pct_from_high is not None and pct_from_high <= 15
    consol_ok = (consol_days or 0) >= 15      # 整固 ≥3週
    contract_ok = vol_contract is True         # 波動收窄

    c = [
        (e200 is not None and close > e200),                         # C1 股價 > 200MA（Stage 2）
        (sma10 is not None and sma20 is not None and sma10 > sma20),  # C2 10MA > 20MA
        (e50 is not None and e200 is not None and e50 > e200),        # C3 黃金排列
        rs_pass,                                                      # C4 跑贏大盤（RS）
        consol_ok,                                                    # C5 整固 ≥3週（VCP base）
        contract_ok,                                                  # C6 波動收窄（VCP 核心）
    ]
    b = [
        near_high,                                                    # b1 距52週高 ≤15%（接近高位）
        (e20 is not None and e50 is not None and e20 > e50),          # b2 EMA20>50
        (50 <= r <= 80),                                              # b3 動能區（RSI）
        (relvol is not None and relvol < 1.0),                        # b4 縮量（整固該縮量）
        (ema200_slope is not None and ema200_slope > 0),              # b5 200日線升緊
    ]
    res["S7"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) == 6,
                 "pctFromHigh": round(pct_from_high, 1) if pct_from_high is not None else None,
                 "rsStrong": rs_jl, "spy1m": round(spy_1m, 1) if spy_1m is not None else None,
                 "consolDays": consol_days, "volContract": vol_contract, "mktOK": _SPY_ABOVE_200,
                 "keyvals": {"距52高%": round(pct_from_high, 1) if pct_from_high is not None else None, "1M%": round(perf1m, 1) if perf1m is not None else None}}


    return res


def compute_streaks(closes, highs, lows, volumes,
                    ema20a, ema50a, ema200a, rsia, atr5a, atr14a):
    n = len(closes)
    last = n - 1
    streaks = {s: 0 for s in STRATEGY_META}
    broken = {s: False for s in STRATEGY_META}
    v1_macro_streak = 0
    v1_macro_broken = False
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
        # S1V1 macro streak：只計 S1(EMA20版) conds[2:5]（EMA200↑/RSI40-70/perf3m1m），
        # 唔理 c1/c2（EMA20/EMA50）—— 跟 V1 checklist「詳細 Scanner 只睇三個條件」原文
        if not v1_macro_broken:
            s1c = ev["S1"]["conds"]
            if len(s1c) >= 5 and all(s1c[2:5]):
                v1_macro_streak += 1
            else:
                v1_macro_broken = True
    streaks["_v1MacroStreak"] = v1_macro_streak
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


# 大盤基準（SPY）date->close，做相對強度 RS 用
_SPY_MAP = None
_SPY_TC = None  # (times, closes) 用嚟計大盤升幅
_SPY_ABOVE_200 = None  # 大盤而家係咪 > 自己 200MA（市況過濾）

def spy_perf(idx_time, lookback_days=30):
    """大盤(SPY) 喺指定日期前 lookback 自然日嘅升幅 %。"""
    if _SPY_TC is None:
        return None
    times, closes = _SPY_TC
    if not times:
        return None
    d = int(idx_time) // 86400
    # 揾最接近 idx_time 嘅 SPY bar
    cur = None; cur_i = None
    for i in range(len(times) - 1, -1, -1):
        if times[i] and int(times[i]) // 86400 <= d:
            cur = closes[i]; cur_i = i; break
    if cur is None or cur_i is None:
        return None
    # 往前約 21 個交易日（≈ 1個月）
    j = max(0, cur_i - 21)
    base = closes[j]
    if not base:
        return None
    return (cur / base - 1) * 100

def load_spy():
    """fetch SPY，建 date->close map（module 快取）。"""
    global _SPY_MAP, _SPY_TC, _SPY_ABOVE_200
    if _SPY_MAP is not None:
        return _SPY_MAP
    h = fetch_history("SPY")
    m = {}
    if h:
        for t, c in zip(h["time"], h["close"]):
            if t and c:
                m[int(t) // 86400] = c
        _SPY_TC = (h["time"], h["close"])
        # 市況：大盤而家係咪喺自己 200MA 之上（Minervini「M」過濾）
        spy_c = h["close"]
        if len(spy_c) >= 200:
            spy_ma200 = sum(spy_c[-200:]) / 200
            _SPY_ABOVE_200 = spy_c[-1] > spy_ma200
    _SPY_MAP = m
    return m


def build_rs_line(times, closes):
    """RS 線 = 股價 / 同日 SPY 收市。返回同 closes 等長嘅 list（None=冇對應SPY）。"""
    spy = load_spy()
    if not spy:
        return None
    out = []
    for t, c in zip(times, closes):
        if not t:
            out.append(None); continue
        d = int(t) // 86400
        sp = spy.get(d) or spy.get(d - 1) or spy.get(d + 1)
        out.append((c / sp) if sp else None)
    return out


def get_russell1000_tickers():
    """大中型美股（市值排序，J Law filter）— S7 用。
    來源：Ate329/top-us-stock-tickers（每日更新，有市值/價格）。
    J Law 條件：市值 3B-2T、價格 ≥$10。取頭 ~1200 隻（≈ Russell 1000+）。"""
    import csv, io
    url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/all.csv"
    try:
        resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=25)
        if resp.status_code == 200 and len(resp.text) > 5000:
            out = []
            rd = csv.DictReader(io.StringIO(resp.text))
            for row in rd:
                try:
                    sym = (row.get("symbol") or "").strip().upper()
                    mc = float(row.get("marketCap") or 0)
                    px = float(row.get("price") or 0)
                except (ValueError, TypeError):
                    continue
                if not sym or not sym.replace("-", "").replace(".", "").isalpha():
                    continue
                # J Law filter：市值 3B-2T、價格 ≥$10
                if 3e9 <= mc <= 2e12 and px >= 10:
                    out.append(sym.replace(".", "-"))
                if len(out) >= 1200:
                    break
            if len(out) > 800:
                return out
    except Exception:
        pass
    return get_sp500_tickers()


def get_sp500_tickers():
    # 1) Wikipedia（有時俾 block / 結構變咗攞唔到）—— 試2次，畀網絡波動多個機會
    for attempt in range(2):
        try:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=20)
            if resp.status_code == 200:
                import re
                rows = re.findall(r'<td><a[^>]*>([A-Z][A-Z.\-]{0,6})</a>', resp.text)
                tickers = [t.replace(".", "-") for t in rows]
                seen = set()
                out = [t for t in tickers if not (t in seen or seen.add(t))]
                if len(out) > 400:
                    return out
            break  # 攞到 response（就算唔啱）都唔使再試，慳時間
        except Exception:
            if attempt == 0:
                time.sleep(2)
    # 2) GitHub 上公開嘅 S&P500 成份股 CSV（穩陣好多，GH Actions 通常唔會俾 block）—— 試2次
    for attempt in range(2):
        try:
            import csv, io
            url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
            resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=20)
            if resp.status_code == 200 and len(resp.text) > 3000:
                rd = csv.DictReader(io.StringIO(resp.text))
                out = []
                seen = set()
                for row in rd:
                    sym = (row.get("Symbol") or "").strip().upper().replace(".", "-")
                    if sym and sym not in seen:
                        seen.add(sym)
                        out.append(sym)
                if len(out) > 400:
                    return out
            break
        except Exception:
            if attempt == 0:
                time.sleep(2)
    # 3) Repo 入面寫死嘅後備清單（sp500_tickers.txt，會隔一排手動/腳本更新一次）——
    #    保證就算上面兩個 live source 同一時間都失敗，都唔會跌落得返9隻嗰個極端 fallback
    try:
        with open("sp500_tickers.txt") as f:
            out = [ln.strip().upper() for ln in f if ln.strip()]
        if len(out) > 400:
            print(f"⚠️ S&P500 live source 攞唔到，用返 repo 入面嘅後備清單（{len(out)} 隻，可能唔係最新）")
            return out
    except FileNotFoundError:
        pass
    # 4) 本地 tickers.txt
    try:
        with open("tickers.txt") as f:
            return [ln.strip().upper() for ln in f if ln.strip()]
    except FileNotFoundError:
        pass
    # 5) 最後手段：極細清單（4個 source 都失敗先會用到，理論上唔應該行到呢度）
    print("🔴 S&P500 全部 source 都攞唔到，跌落最後嗰個9隻極細清單！要人手check")
    return ["AAPL", "MSFT", "NVDA", "AMD", "PLTR", "CRWD", "AVGO", "META", "TSLA"]


def get_sector_map():
    """
    Ticker -> GICS Sector（例如 'Energy', 'Financials', 'Information Technology'）。
    直接攞返 get_sp500_tickers() 用緊嗰份 GitHub CSV，因為佢本身已經有 'GICS Sector' 呢欄，
    零額外 network call、零額外風險。淨係 S&P500 成份股先有（非S&P500嘅股會冇呢個資料，屬正常）。
    失敗就靜靜雞回傳空dict，唔會影響其他任何嘢。
    """
    try:
        import csv, io
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        resp = requests.get(url, headers={"User-Agent": YF_HEADERS["User-Agent"]}, timeout=20)
        if resp.status_code == 200 and len(resp.text) > 3000:
            rd = csv.DictReader(io.StringIO(resp.text))
            out = {}
            for row in rd:
                sym = (row.get("Symbol") or "").strip().upper().replace(".", "-")
                sector = (row.get("GICS Sector") or "").strip()
                if sym and sector:
                    out[sym] = sector
            if len(out) > 400:
                return out
    except Exception:
        pass
    return {}


def get_earnings_calendar(days_back=5, days_forward=7):
    """
    Ticker -> 業績日期字串（YYYY-MM-DD）。用 NASDAQ 公開嘅「按日子」業績日曆 API，
    一次過攞返「嗰日邊啲股出業績」，唔使逐隻股問（1200幾隻淨係問返 ~12 次，唔係1200幾次）。
    NASDAQ 呢個 API 有時會唔穩（可能要 header 先俾access，或者間唔中404），所以逐日獨立 try/except，
    一日攞唔到就skip嗰日，唔會累到成個 scan 停擺；亦唔會影響任何價格/策略數據。
    """
    from datetime import timedelta
    out = {}
    today = datetime.now(timezone.utc).date()
    headers = {
        "User-Agent": YF_HEADERS["User-Agent"],
        "Accept": "application/json",
    }
    for delta in range(-days_back, days_forward + 1):
        day = today + timedelta(days=delta)
        if day.weekday() >= 5:  # 週末冇交易，唔使問
            continue
        date_str = day.strftime("%Y-%m-%d")
        try:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue
            data = resp.json()
            rows = (data.get("data") or {}).get("rows") or []
            for row in rows:
                sym = (row.get("symbol") or "").strip().upper()
                if sym:
                    out[sym] = date_str
        except Exception:
            continue
    return out


# 2026 FOMC 議息日（已查證，官方公布）——用嗰兩日入面最後一日（宣布政策決定嗰日）
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
# CPI 公布日（已查證幾個實際日子；其餘月份用「約第2個星期二」估算，可能有1-2日誤差）
CPI_2026 = ["2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12",
            "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-13",
            "2026-11-12", "2026-12-10"]


def get_macro_calendar():
    """
    宏觀經濟大事日曆（FOMC/CPI/非農），影響成個大市，唔關邊隻股事。
    FOMC/CPI 用查證咗嘅實際日子；非農（NFP）用「每月第一個星期五」呢個公開、穩定嘅規則自己計，
    唔使額外查（呢條規則本身好少例外，準確度好高）。
    """
    from datetime import timedelta
    events = []
    for d in FOMC_2026:
        events.append({"date": d, "name": "FOMC 議息", "icon": "🏛️"})
    for d in CPI_2026:
        events.append({"date": d, "name": "CPI 通脹數據", "icon": "📊"})
    for month in range(1, 13):
        day = datetime(2026, month, 1)
        while day.weekday() != 4:
            day += timedelta(days=1)
        events.append({"date": day.strftime("%Y-%m-%d"), "name": "非農就業報告", "icon": "👷"})
    events.sort(key=lambda x: x["date"])
    return events


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def build_record(ticker, hist):
    global _CUR_RS_LINE, _CUR_TIMES
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

    # 設定 RS 線 + bar 時間（相對 SPY），畀 S7 用
    _CUR_TIMES = hist.get("time", [])
    _CUR_RS_LINE = build_rs_line(hist.get("time", []), closes) if ticker != "SPY" else None

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
            # 回調形態（用 low 觸 EMA20，較穩健）
            strategies[s]["pullbackTouch"] = today[s].get("pullbackTouch", False)
            strategies[s]["pullbackDaysAgo"] = today[s].get("pullbackDaysAgo", -1)
            strategies[s]["aboveNow"] = today[s].get("aboveNow", False)
            # 過去 20 日內最高連續 5/5（穩健反映「趨勢曾經健康」，唔靠精準跌穿日）
            recent_max = 0
            run = 0
            for back in range(0, 21):
                k = last - back
                if k < 63:
                    break
                ev = eval_strategies(k, closes, highs, lows, volumes,
                                     ema20a, ema50a, ema200a, rsia, atr5a, atr14a)
                if ev is not None and ev["S1"]["ready"]:
                    run += 1
                    if run > recent_max:
                        recent_max = run
                else:
                    run = 0
            strategies[s]["recentMaxStreak"] = recent_max
        # S1V1 原版（前頂支撐）：pass-through 波段價位 + entry/stop/T1/T2
        if s == "S1V1":
            for k in ("wave1Top", "wave2Top", "breachLow", "breakoutDistPct",
                      "daysToBreach", "daysRecover", "daysSinceRecover", "patternType", "cleanLevel", "betweenPivots", "aboveNow", "entry", "stop", "t1", "t2"):
                strategies[s][k] = today[s].get(k)
            # 粗篩用：借 S1(EMA20版) 嘅 streak + EMA200斜率，做「大趨勢確認」參考（V1 checklist Step0 要求）
            strategies[s]["macroStreak"] = streaks.get("_v1MacroStreak", 0)
            strategies[s]["macroEma200Up"] = bool(today["S1"]["conds"][2]) if len(today["S1"]["conds"]) > 2 else None
        # S7 距52週高
        if s == "S7":
            strategies[s]["pctFromHigh"] = today[s].get("pctFromHigh")
            strategies[s]["spy1m"] = today[s].get("spy1m")
            strategies[s]["rsStrong"] = today[s].get("rsStrong")
            strategies[s]["consolDays"] = today[s].get("consolDays")
            strategies[s]["volContract"] = today[s].get("volContract")
            strategies[s]["mktOK"] = today[s].get("mktOK")
        # S6 旗形 H1 / 突破（approximate）
        if s == "S6":
            strategies[s]["h1"] = today[s].get("h1")
            strategies[s]["flagLow"] = today[s].get("flagLow")
            strategies[s]["pctToH1"] = today[s].get("pctToH1")
            strategies[s]["brokeH1"] = today[s].get("brokeH1", False)
            strategies[s]["flagRetrace"] = today[s].get("flagRetrace")

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
        "high": round(highs[last], 2),
        "low": round(lows[last], 2),
        "chgPct": round(pct_change(closes, last, 1) or 0, 2),
        "ema20": round(ema20a[last], 2),
        "ema50": round(ema50a[last], 2),
        "ema200": round(ema200a[last], 2),
        "rsi": round(rsia[last], 1),
        "atr14": round(atr14a[last], 2) if atr14a[last] else None,
        "sma10": round(sma_at(closes, last, 10), 2) if sma_at(closes, last, 10) is not None else None,
        "rvol": round(volumes[last] / sma_at(volumes, last, 20), 2) if sma_at(volumes, last, 20) else None,
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
    sp500 = set(get_sp500_tickers())
    sector_map = get_sector_map()
    print(f"Sector 分類：{len(sector_map)} 隻（淨係 S&P500 成份股有）")
    earnings_map = get_earnings_calendar()
    print(f"業績日曆：攞到 {len(earnings_map)} 隻嘅業績日期（可能因為 NASDAQ API 唔穩而係0，唔會影響其他數據）")
    # S7 想要中型爆發股 → 用 Russell 1000；S1-S6 喺 app 度 filter 返 S&P 500
    tickers = get_russell1000_tickers()
    # 確保 S&P 500 全部包到（萬一 IWB 攞唔齊）
    for t in sp500:
        if t not in tickers:
            tickers.append(t)
    print(f"掃描 {len(tickers)} 隻股（Russell 1000，S&P500={len(sp500)}）…")

    prev_ready = load_previous_ready()

    records = []
    ok = 0
    for i, t in enumerate(tickers, 1):
        hist = fetch_history(t)
        if hist:
            rec = build_record(t, hist)
            if rec:
                rec["inSP500"] = (t in sp500)   # 標記，app 用嚟 filter S1-S6
                rec["sector"] = sector_map.get(t)  # None = 唔喺S&P500入面／攞唔到
                edate = earnings_map.get(t)
                rec["earningsDate"] = edate
                if edate:
                    try:
                        days_diff = (datetime.strptime(edate, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                        rec["daysToEarnings"] = days_diff
                    except Exception:
                        rec["daysToEarnings"] = None
                else:
                    rec["daysToEarnings"] = None
                records.append(rec)
                ok += 1
        if i % 25 == 0:
            print(f"  {i}/{len(tickers)} … (成功 {ok})")
        time.sleep(REQUEST_SLEEP)

    summary = {}
    for s in STRATEGY_META:
        if s == "S7":
            ready = [r["ticker"] for r in records if r["strategies"][s]["ready"]]
        else:
            ready = [r["ticker"] for r in records if r["strategies"][s]["ready"] and r.get("inSP500")]
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
