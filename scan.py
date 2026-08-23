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
    "S4": {"name": "假突破",    "dir": "Short",      "live": True,  "reqMax": 4, "bonusMax": 4},
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


def detect_s3_buildup(idx, highs, lows, closes, volumes):
    """
    S3 Buildup 突破偵測：搵返 A(推進浪起點)→B(Buildup頂/突破線)→Buildup底部，
    確認 b6（Buildup底 > B - impulse×0.236），再睇今日係咪已經放量突破。
    公式跟返 s3_checklist.html 已驗證嘅版本：
      止損 = Buildup底部 × 0.98（手法A；手法B/C改用假突破/橫行最低Close × 0.98）
      T1 = B + BuildupHeight × 1.5
      T2 = B + ImpulseHeight × 0.618
    搵唔到合理結構就回傳 None。
    """
    win = 70
    start = max(0, idx - win)
    seg_h = highs[start:idx + 1]
    seg_l = lows[start:idx + 1]
    seg_c = closes[start:idx + 1]
    seg_v = volumes[start:idx + 1]
    n = len(seg_c)
    if n < 15:
        return None

    zz = zigzag_pivots(seg_h, seg_l, min_pct=4.0)
    h_piv = [(i, p) for i, p, t in zz if t == 'H']
    l_piv = [(i, p) for i, p, t in zz if t == 'L']
    if not h_piv:
        return None

    best = None
    for b_idx, b_price in reversed(h_piv):
        if b_idx > n - 4:
            continue  # B 太貼近今日，未有足夠日子形成 Buildup（起碼要3日）
        cands_a = [(i, p) for i, p in l_piv if i < b_idx]
        if not cands_a:
            continue
        a_idx, a_price = cands_a[-1]
        impulse_h = b_price - a_price
        if impulse_h <= 0 or a_price <= 0:
            continue
        if impulse_h / a_price * 100 < 8:
            continue  # 推進浪太細，唔算數（避免噪音）
        post_l = seg_l[b_idx + 1:n]
        if not post_l:
            continue
        buildup_low = min(post_l)
        fib236 = b_price - impulse_h * 0.236
        if buildup_low <= fib236:
            continue  # b6 唔過：回調過深
        best = {"a_idx": a_idx, "a": a_price, "b_idx": b_idx, "b": b_price,
                "buildup_low": buildup_low, "impulse_h": impulse_h}
        break  # h_piv 已經係時間順序，reversed後第一個岩嘅就係最近嘅合理B

    if not best:
        return None

    b_price, buildup_low, a_price, impulse_h = best["b"], best["buildup_low"], best["a"], best["impulse_h"]
    buildup_h = b_price - buildup_low
    if buildup_h <= 0:
        return None

    today_c = seg_c[-1]
    today_v = seg_v[-1]
    prior_v = seg_v[max(0, n - 21):n - 1]
    avg_v20 = sum(prior_v) / len(prior_v) if prior_v else None
    rvol_today = (today_v / avg_v20) if avg_v20 else None

    broke_today = today_c > b_price
    method_a_ok = broke_today and rvol_today is not None and rvol_today >= 1.5

    stop = buildup_low * 0.98
    t1 = b_price + buildup_h * 1.5
    t2 = b_price + impulse_h * 0.618

    # 假突破：曾經跌穿 Buildup 底部（結構已經失敗，唔算數）
    failed = today_c < buildup_low if n else False

    days_since_b = (n - 1) - best["b_idx"]

    # 「穿咗幾多次」——由B之後到今日，逐日行一次，數幾多次由 close<=B 變做 close>B。
    # 每一次轉變都係一次獨立嘅突破嘗試：第1次=手法A情境；第2次或以上=已經有假突破，而家係手法B「再次突破」。
    post_c_all = seg_c[best["b_idx"] + 1:n]
    cross_count = 0
    was_above = False
    for pc in post_c_all:
        is_above = pc > b_price
        if is_above and not was_above:
            cross_count += 1
        was_above = is_above

    return {
        "a": round(a_price, 2), "b": round(b_price, 2), "buildupLow": round(buildup_low, 2),
        "buildupHeight": round(buildup_h, 2), "impulseHeight": round(impulse_h, 2),
        "brokeToday": broke_today, "rvolToday": round(rvol_today, 2) if rvol_today is not None else None,
        "methodAOk": method_a_ok, "failed": failed, "daysSinceB": days_since_b, "crossCount": cross_count,
        "stop": round(stop, 2), "t1": round(t1, 2), "t2": round(t2, 2),
        "entry": round(today_c, 2) if method_a_ok else None,
    }


def detect_s4_false_breakout(idx, highs, lows, closes, volumes, diag_out=None):
    """
    S4 假突破（淡倉）Playbook（原文：My Play Book - 橫行區假突破策略）：
    急速/過度延伸嘅升勢（pole）之後，喺高位形成一段橫行派貨區（range）；
    價格向上假突破呢個區，但好快返回派貨區之內 —— 返回嗰一刻就係淡倉入場位。

    止蝕 = 假突破期間嘅最高位
    T1   = 橫行區中間點（原文：可代替「假突破升浪的0.618」，兩者數值非常接近）
    T2   = 橫行區底部
    T3   = 假設橫行區跌穿，用返橫行區前嘅升浪（pole）的0.618

    搵唔到就回傳 None。diag_out（optional）記低行到邊一步、實際數值係咩。

    重要：pole頂一定要係一個「真正、有意義嘅」近期高位（用35日lookback搵），
    唔可以淨係「最近10日rolling window嘅高位」——試過真實case（NTRS）證明咗，
    如果唔搵闊啲，會將「仲喺度跌緊/未跌完」錯認做「橫行區」，
    因為真正阻力（例如12日前嘅高位）可能啱啱好喺rolling window之外。
    """
    win = 60
    start = max(0, idx - win)
    seg_h = highs[start:idx + 1]
    seg_l = lows[start:idx + 1]
    seg_c = closes[start:idx + 1]
    seg_v = (volumes[start:idx + 1] if volumes else [None] * len(seg_c))
    n = len(seg_c)
    pole_lookback = 35   # 搵真正pole頂嘅lookback（要闊過range本身，先唔會漏咗更早、更relevant嘅高位）
    min_range_days = 5   # pole頂之後最少要幾多日先算形成到range（唔可以啱啱破頂就話突破）
    fresh_days = 6        # breakout要喺最近幾日之內先算新鮮
    revert_window = 6     # breakout之後最多畀幾多日等佢返落嚟

    if n < pole_lookback + min_range_days:
        if diag_out is not None: diag_out['failedAt'] = 'history_too_short'
        return None

    # Step 1a：搵真正嘅pole頂 —— pole_lookback日之內嘅最高high，
    # 但要排除最近fresh_days日（留返呢段畀breakout檢查用，唔可以啱啱先破嘅頂都當自己pole頂）
    search_start = max(0, n - 1 - pole_lookback)
    search_end = n - fresh_days
    if search_end - search_start < min_range_days:
        if diag_out is not None: diag_out['failedAt'] = 'history_too_short'
        return None
    pole_idx, pole_top = None, -1
    for j in range(search_start, search_end):
        if seg_h[j] > pole_top:
            pole_top = seg_h[j]
            pole_idx = j
    if pole_idx is None:
        if diag_out is not None: diag_out['failedAt'] = 'no_pole_peak'
        return None
    if (search_end - pole_idx) < min_range_days:
        if diag_out is not None: diag_out['failedAt'] = 'pole_too_recent'
        return None
    if diag_out is not None:
        diag_out['poleTopFound'] = True
        diag_out['poleTop'] = round(pole_top, 2)

    # Step 1b：由search_end開始向前搵breakout day —— high > pole_top（真正嘅頂，唔係rolling window）
    brk_idx = None
    for j in range(search_end, n):
        if seg_h[j] > pole_top:
            brk_idx = j
            break
    if brk_idx is None:
        if diag_out is not None: diag_out['failedAt'] = 'no_recent_breakout'
        return None

    range_h_seg = seg_h[pole_idx + 1:brk_idx]
    range_l_seg = seg_l[pole_idx + 1:brk_idx]
    range_top = pole_top
    range_low = min(range_l_seg) if range_l_seg else None
    if not range_l_seg or range_low <= 0:
        if diag_out is not None: diag_out['failedAt'] = 'invalid_range'
        return None
    range_pct = (range_top - range_low) / range_low * 100
    if diag_out is not None:
        diag_out['brkIdxFound'] = True
        diag_out['rangeTop'] = round(range_top, 2)
        diag_out['rangeLow'] = round(range_low, 2)
        diag_out['rangeDays'] = brk_idx - (pole_idx + 1)
        diag_out['rangePct'] = round(range_pct, 2)
    if range_pct > 12:
        if diag_out is not None: diag_out['failedAt'] = 'range_too_wide'
        return None

    # 窄幅唔等於橫行——平緩但持續嘅升勢，高低幅度都可以好窄。
    # 要額外check個range係咪真係「嚟嚟去去」（橫行），唔係淨係「慢慢咁繼續升」：
    # 攞range窗口頭尾嘅close，佢哋嘅淨移動（drift）如果佔咗成個range高低差好大部分，即係其實仲喺度trend緊，唔係休息緊
    range_c_seg = seg_c[pole_idx + 1:brk_idx]
    net_drift = range_c_seg[-1] - range_c_seg[0]
    drift_ratio = (abs(net_drift) / (range_top - range_low)) if (range_top - range_low) > 0 else 1
    if diag_out is not None: diag_out['driftRatio'] = round(drift_ratio, 2)
    if drift_ratio > 0.35:
        if diag_out is not None: diag_out['failedAt'] = 'range_not_flat'
        return None

    # 真正嘅派貨區要兩邊（頂+底）都俾人試過唔止一次，唔係淨係「跌落底一次，之後一路升去頂」
    touch_tol = 1.5  # 貼近邊界嘅容忍度（%）
    low_touches = sum(1 for x in range_l_seg if (x - range_low) / range_low * 100 <= touch_tol)
    high_touches = sum(1 for x in range_h_seg if (range_top - x) / range_top * 100 <= touch_tol)
    if diag_out is not None:
        diag_out['lowTouches'] = low_touches
        diag_out['highTouches'] = high_touches
    if low_touches < 2 or high_touches < 2:
        if diag_out is not None: diag_out['failedAt'] = 'range_not_two_sided'
        return None

    # Step 2：假突破期間嘅最高（可能唔止一日）+ 搵return day（close返落range_top之下）
    brk_high = seg_h[brk_idx]
    return_idx = None
    for j in range(brk_idx, min(brk_idx + revert_window, n)):
        if seg_h[j] > brk_high:
            brk_high = seg_h[j]
        if seg_c[j] < range_top:
            return_idx = j
            break
    if return_idx is None:
        if diag_out is not None:
            diag_out['failedAt'] = 'not_reverted_yet'
            diag_out['brkHigh'] = round(brk_high, 2)
        return None

    # 原文明確提過：「假突破的幅度不能太多也不能太少」「太少嘅話...橫行狀態不會因此被改變...
    # 太細嘅假突破機會會建議放棄」。淨係 high > pole_top 唔夠，要有夠意義嘅幅度先算「真.假突破」。
    breakout_excess_pct = (brk_high - pole_top) / pole_top * 100
    if diag_out is not None: diag_out['breakoutExcessPct'] = round(breakout_excess_pct, 3)
    if breakout_excess_pct < 0.3:
        if diag_out is not None: diag_out['failedAt'] = 'breakout_too_small'
        return None

    breakout_duration = return_idx - brk_idx + 1
    days_since_return = (n - 1) - return_idx
    if diag_out is not None:
        diag_out['brkHigh'] = round(brk_high, 2)
        diag_out['breakoutDuration'] = breakout_duration
        diag_out['daysSinceReturn'] = days_since_return
    if days_since_return > 5:
        if diag_out is not None: diag_out['failedAt'] = 'return_too_old'
        return None

    # 原文明確警告：「太細嘅假突破...短時間內會再出現一次更大嘅假突破，呢次假突破會將先前
    # 較微細嘅假突破止損觸發」。即係話「返回」一日唔代表數，之後直到今日，如果價格再次
    # 升穿返個止蝕位（brk_high），即係呢個「假突破」已經失敗、變咗做真突破，成個setup要報廢。
    for j in range(return_idx + 1, n):
        if seg_h[j] > brk_high:
            if diag_out is not None:
                diag_out['failedAt'] = 'stop_retriggered'
                diag_out['retriggerHigh'] = round(seg_h[j], 2)
            return None

    # Step 3：搵A（pole起點，畀T3/steepness bonus用）—— pole_idx之前嘅最低位
    a_lookback = 30
    a_start = max(0, pole_idx - a_lookback)
    pole_seg_l = seg_l[a_start:pole_idx + 1]
    a_price = min(pole_seg_l) if pole_seg_l else None
    pole_pct = ((range_top - a_price) / a_price * 100) if (a_price and a_price > 0) else None
    if diag_out is not None:
        diag_out['poleA'] = round(a_price, 2) if a_price is not None else None
        diag_out['polePct'] = round(pole_pct, 2) if pole_pct is not None else None

    entry = range_top
    stop = brk_high
    t1 = (range_top + range_low) / 2
    t2 = range_low
    t3 = (range_top - (range_top - a_price) * 0.618) if a_price else range_low

    wick_ratio = None
    if (brk_high - seg_l[brk_idx]) > 0:
        wick_ratio = (brk_high - seg_c[brk_idx]) / (brk_high - seg_l[brk_idx])

    brk_vol = seg_v[brk_idx]
    vol_slice = [v for v in seg_v[max(0, brk_idx - 20):brk_idx] if v]
    avg_vol20 = (sum(vol_slice) / len(vol_slice)) if vol_slice else None
    brk_rvol = (brk_vol / avg_vol20) if (brk_vol and avg_vol20) else None

    if diag_out is not None:
        diag_out['wickRatio'] = round(wick_ratio, 2) if wick_ratio is not None else None
        diag_out['brkRvol'] = round(brk_rvol, 2) if brk_rvol is not None else None
        diag_out['failedAt'] = None

    return {
        "poleA": round(a_price, 2) if a_price else None, "poleTop": round(range_top, 2),
        "rangeTop": round(range_top, 2), "rangeLow": round(range_low, 2),
        "entry": round(entry, 2), "stop": round(stop, 2),
        "t1": round(t1, 2), "t2": round(t2, 2), "t3": round(t3, 2),
        "breakoutDuration": breakout_duration, "daysSinceReturn": days_since_return,
        "polePct": round(pole_pct, 2) if pole_pct is not None else None,
        "wickRatio": round(wick_ratio, 2) if wick_ratio is not None else None,
        "brkRvol": round(brk_rvol, 2) if brk_rvol is not None else None,
    }


def detect_s5_confluence(idx, highs, lows, closes, diag_out=None):
    """
    S5 支持阻力會被尊重（Playbook）：
    搵一段「直線向上」嘅 LTF 升浪(A底→B頂，中間唔可以有太多內部反覆)，
    A點之前有一段窄幅嘅 Congestion Area（未經計算嘅支持阻力，主觀性低）；
    現價回調到 0.786 fib，同呢個 Congestion Area 重疊 = confluence，先算入場條件成立。

    入場價 = LTF回調0.786 同 Congestion Area 重疊嘅位置
    止損   = LTF升浪(A)底部
    T1     = 回調段(B到現價最低)嘅反彈 0.618
    T2     = LTF升浪頂部(B)

    搵唔到合理confluence就回傳 None（唔會扮識，寧願話"未搵到"）。

    diag_out（optional）：如果傳一個dict入嚟，會畀呢個function喺內部逐步寫低行到邊一步、
    嗰步嘅實際數值係咩，等你搵唔到confluence嗰陣可以知道係邊一關卡住，唔係淨係得一個None。
    唔傳（default）就完全冧有額外開銷，行為同之前一模一樣——股票版嗰個call site冧使郁。
    """
    win = 60
    start = max(0, idx - win)
    seg_h = highs[start:idx + 1]
    seg_l = lows[start:idx + 1]
    seg_c = closes[start:idx + 1]
    n = len(seg_c)
    if n < 20:
        if diag_out is not None: diag_out['failedAt'] = 'history_too_short'
        return None

    zz = zigzag_pivots(seg_h, seg_l, min_pct=4.0)
    h_piv = [(i, p) for i, p, t in zz if t == 'H']
    l_piv = [(i, p) for i, p, t in zz if t == 'L']
    if not h_piv or not l_piv:
        if diag_out is not None: diag_out['failedAt'] = 'no_pivots'
        return None

    # B：最近一個 H pivot（LTF升浪頂），要有足夠時間先算真正回調完成（唔可以係呢兩日先啱啱破頂）
    b_idx, b_price = h_piv[-1]
    if b_idx >= n - 5:
        if len(h_piv) >= 2:
            b_idx, b_price = h_piv[-2]
        else:
            if diag_out is not None: diag_out['failedAt'] = 'b_too_recent_no_alt'
            return None
    if diag_out is not None:
        diag_out['bFound'] = True
        diag_out['bPrice'] = round(b_price, 5)

    # A：B之前嘅 L pivot（LTF升浪起點）
    cands_a = [(i, p) for i, p in l_piv if i < b_idx]
    if not cands_a:
        if diag_out is not None: diag_out['failedAt'] = 'no_a_before_b'
        return None
    a_idx, a_price = cands_a[-1]
    impulse_h = b_price - a_price
    if diag_out is not None:
        diag_out['aFound'] = True
        diag_out['aPrice'] = round(a_price, 5)
    if impulse_h <= 0 or a_price <= 0:
        if diag_out is not None: diag_out['failedAt'] = 'invalid_impulse'
        return None
    leg_pct = impulse_h / a_price * 100
    if diag_out is not None: diag_out['legPct'] = round(leg_pct, 2)
    if leg_pct < 6:
        if diag_out is not None: diag_out['failedAt'] = 'leg_too_small'
        return None  # 升浪太細，唔算數（避免噪音）
    if (b_idx - a_idx) < 5:
        if diag_out is not None: diag_out['failedAt'] = 'leg_too_short'
        return None  # A到B少過5日就成形，太急太短，好可能係插針式短炒，唔係健康嘅持續買盤

    # 「直線向上」檢查：A到B之間，唔可以有太多內部反覆pivot（超過1個轉勢就唔算直線）
    mid_piv = [x for x in zz if a_idx < x[0] < b_idx]
    if len(mid_piv) > 2:
        if diag_out is not None: diag_out['failedAt'] = 'not_straight_line'
        return None

    # Congestion Area：A點之前 5-12 日嘅窄幅波動區
    cwin = seg_h[max(0, a_idx - 12):a_idx + 1], seg_l[max(0, a_idx - 12):a_idx + 1]
    conges_h, conges_l = cwin
    if len(conges_h) < 4:
        if diag_out is not None: diag_out['failedAt'] = 'congestion_too_short'
        return None
    conges_top, conges_bottom = max(conges_h), min(conges_l)
    if conges_bottom <= 0:
        if diag_out is not None: diag_out['failedAt'] = 'invalid_congestion'
        return None
    conges_range_pct = (conges_top - conges_bottom) / conges_bottom * 100
    if diag_out is not None: diag_out['congesRangePct'] = round(conges_range_pct, 2)
    if conges_range_pct > 9:
        if diag_out is not None: diag_out['failedAt'] = 'congestion_too_wide'
        return None  # 太闊，唔算「窄幅」整理區

    # 現價回調 0.786
    fib786 = b_price - impulse_h * 0.786
    today_c = seg_c[-1]
    if diag_out is not None:
        diag_out['fib786'] = round(fib786, 5)
        diag_out['congesTopRaw'] = round(conges_top, 5)
        diag_out['congesBottomRaw'] = round(conges_bottom, 5)

    # Confluence 檢查：fib786 要落喺 Congestion Area 範圍（留少少彈性）
    overlap = (conges_bottom * 0.99) <= fib786 <= (conges_top * 1.01)
    if not overlap:
        if diag_out is not None: diag_out['failedAt'] = 'no_overlap'
        return None

    if diag_out is not None: diag_out['failedAt'] = None  # 全部check都過晒

    # 已經跌穿 A 底 = 結構失敗
    failed = today_c < a_price

    entry = round(fib786, 2)
    stop = round(a_price, 2)
    post_b_low = min(seg_l[b_idx + 1:n]) if n > b_idx + 1 else today_c
    t1 = round(post_b_low + (b_price - post_b_low) * 0.618, 2)
    t2 = round(b_price, 2)

    days_since_b = (n - 1) - b_idx
    touched = today_c <= entry * 1.01  # 現價已經貼近/跌到入場位

    return {
        "a": round(a_price, 2), "b": round(b_price, 2),
        "congesTop": round(conges_top, 2), "congesBottom": round(conges_bottom, 2),
        "fib786": entry, "entry": entry, "stop": stop, "t1": t1, "t2": t2,
        "failed": failed, "touched": touched, "daysSinceB": days_since_b,
    }


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
    # dipBreachEma50 = 呢段跌穿EMA20期間，有冇任何一日 close 都連埋 EMA50 一齊跌穿
    #   （原文Playbook：跌穿EMA20但冧穿EMA50 = 健康回調；連EMA50都跌埋 = 較弱訊號，兩者質素唔同）
    days_below = 0
    days_recover = 0
    dip_breach_ema50 = False
    if e20 is not None:
        if close < e20:
            # 今日仲喺下面 → 數連續跌穿日數，未收復
            j = idx
            while j >= 0 and ema20a[j] is not None and closes[j] < ema20a[j]:
                days_below += 1
                if ema50a[j] is not None and closes[j] < ema50a[j]:
                    dip_breach_ema50 = True
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
                    if ema50a[k] is not None and closes[k] < ema50a[k]:
                        dip_breach_ema50 = True
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
    # 每項 Required/Bonus 背後嘅實際數值（唔淨係true/false），俾你之後可以做真正數據分析
    c_vals = [
        round((close - e20) / e20 * 100, 2) if e20 else None,
        round((close - e50) / e50 * 100, 2) if e50 else None,
        round(ema200_slope, 4) if ema200_slope is not None else None,
        round(r, 1) if r is not None else None,
        (f"{round(perf3m,1)}/{round(perf1m,1)}" if perf3m is not None and perf1m is not None else None),
    ]
    b_vals = [
        round((e20 - e50) / e50 * 100, 2) if e50 else None,
        round((e50 - e200) / e200 * 100, 2) if e200 else None,
        round(relvol, 2) if relvol is not None else None,
        round(perf1w, 1) if perf1w is not None else None,
        round(pct_from_high, 1) if pct_from_high is not None else None,
    ]
    res["S1"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) == 5,
                 "daysBelow20": days_below, "daysRecover": days_recover, "dipBreachEma50": dip_breach_ema50,
                 "condVals": c_vals, "bonusVals": b_vals,
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

    # ── S3 突破交易（Buildup，Required 4/5, Bonus 5/5）──
    s3_buildup = detect_s3_buildup(idx, highs, lows, closes, volumes)
    if s3_buildup:
        c = [
            True,                                              # R1: 搵到合理Buildup結構（A/B/底部齊、b6過）
            not s3_buildup["failed"],                          # R2: 未跌穿Buildup底部（結構未失敗）
            close > e50,                                       # R3: 大趨勢健康
            s3_buildup["daysSinceB"] <= 40,                    # R4: Buildup未拖太耐（B太舊，結構意義降低）
            (perf3m is not None and perf3m > 10),              # R5: 3個月升幅夠（確保impulse係真趨勢，唔係噪音）
        ]
        b = [
            s3_buildup["methodAOk"],                                                       # b1 今日放量突破（手法A）
            close > e200,                                                                   # b2 長線趨勢都健康
            (50 <= r <= 70),                                                                # b3 RSI 健康區間
            (s3_buildup["rvolToday"] is not None and s3_buildup["rvolToday"] < 0.9 and not s3_buildup["brokeToday"]),  # b4 Buildup期縮量（未突破時）
            (s3_buildup["daysSinceB"] <= 20),                                                # b5 Buildup仲算新鮮
        ]
        keyvals = {
            "A(推進浪底)": s3_buildup["a"], "B(突破線)": s3_buildup["b"],
            "Buildup底": s3_buildup["buildupLow"], "距B幾多日": s3_buildup["daysSinceB"],
            "穿咗幾多次": s3_buildup["crossCount"],
        }
        res["S3"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) >= 4,
                     "keyvals": keyvals,
                     "buildupA": s3_buildup["a"], "buildupB": s3_buildup["b"], "buildupLow": s3_buildup["buildupLow"],
                     "brokeToday": s3_buildup["brokeToday"], "rvolToday": s3_buildup["rvolToday"],
                     "methodAOk": s3_buildup["methodAOk"], "daysSinceB": s3_buildup["daysSinceB"],
                     "crossCount": s3_buildup["crossCount"],
                     "entry": s3_buildup["entry"], "stop": s3_buildup["stop"],
                     "t1": s3_buildup["t1"], "t2": s3_buildup["t2"]}
    else:
        res["S3"] = {"conds": [False]*5, "bonus": [False]*5, "score": 0, "bonusScore": 0, "ready": False,
                     "keyvals": {}, "buildupA": None, "buildupB": None, "buildupLow": None,
                     "brokeToday": None, "rvolToday": None, "methodAOk": None, "daysSinceB": None,
                     "crossCount": None,
                     "entry": None, "stop": None, "t1": None, "t2": None}

    # ── S4 假突破（淡倉，Required 4/4, Bonus 4/4）── 跟原文Playbook：橫行區假突破策略
    s4_fb = detect_s4_false_breakout(idx, highs, lows, closes, volumes)
    if s4_fb:
        risk = s4_fb["stop"] - s4_fb["entry"]
        rr_t2 = ((s4_fb["entry"] - s4_fb["t2"]) / risk) if risk > 0 else 0
        c = [
            (s4_fb["daysSinceReturn"] <= 3),                                    # R1 訊號夠新鮮（3日內都算，太窄冧夠嘢睇）
            (s4_fb["breakoutDuration"] <= 3),                                   # R2 假突破時間夠短（原文條件五）
            (rr_t2 >= 1.5),                                                     # R3 風險回報基本合理
            (s4_fb["rangeTop"] > 0 and (s4_fb["rangeTop"]-s4_fb["rangeLow"])/s4_fb["rangeLow"]*100 <= 10),  # R4 橫行區夠窄
        ]
        b = [
            (s4_fb["polePct"] is not None and s4_fb["polePct"] >= 15),          # B1 升浪夠急夠延伸（原文條件一/二）
            (s4_fb["wickRatio"] is not None and s4_fb["wickRatio"] >= 0.5),     # B2 假突破有明顯上影線（原文條件四/六）
            (s4_fb["brkRvol"] is not None and s4_fb["brkRvol"] < 1.0),          # B3 假突破冇帶量（原文條件七）
            (s4_fb["daysSinceReturn"] <= 1),                                    # B4 特別新鮮（今日/琴日），入場窗口最好
        ]
        res["S4"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) >= 4,
                     "keyvals": {"橫行區": f"{s4_fb['rangeLow']}-{s4_fb['rangeTop']}", "假突破高位": s4_fb["stop"]},
                     "poleA": s4_fb["poleA"], "rangeTop": s4_fb["rangeTop"], "rangeLow": s4_fb["rangeLow"],
                     "entry": s4_fb["entry"], "stop": s4_fb["stop"],
                     "t1": s4_fb["t1"], "t2": s4_fb["t2"], "t3": s4_fb["t3"],
                     "breakoutDuration": s4_fb["breakoutDuration"], "daysSinceReturn": s4_fb["daysSinceReturn"],
                     "polePct": s4_fb["polePct"], "wickRatio": s4_fb["wickRatio"], "brkRvol": s4_fb["brkRvol"]}
    else:
        res["S4"] = {"conds": [False]*4, "bonus": [False]*4, "score": 0, "bonusScore": 0, "ready": False,
                     "keyvals": {}, "poleA": None, "rangeTop": None, "rangeLow": None,
                     "entry": None, "stop": None, "t1": None, "t2": None, "t3": None,
                     "breakoutDuration": None, "daysSinceReturn": None,
                     "polePct": None, "wickRatio": None, "brkRvol": None}

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
    s5_conf = detect_s5_confluence(idx, highs, lows, closes)
    res["S5"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b), "ready": sum(c) == 4,
                 "keyvals": {"RSI": round(r, 1), "1M%": round(perf1m, 1) if perf1m is not None else None},
                 "confluenceFound": s5_conf is not None,
                 "confA": s5_conf["a"] if s5_conf else None, "confB": s5_conf["b"] if s5_conf else None,
                 "congesTop": s5_conf["congesTop"] if s5_conf else None,
                 "congesBottom": s5_conf["congesBottom"] if s5_conf else None,
                 "entry": s5_conf["entry"] if s5_conf else None, "stop": s5_conf["stop"] if s5_conf else None,
                 "t1": s5_conf["t1"] if s5_conf else None, "t2": s5_conf["t2"] if s5_conf else None,
                 "touched": s5_conf["touched"] if s5_conf else None,
                 "confFailed": s5_conf["failed"] if s5_conf else None,
                 "daysSinceB": s5_conf["daysSinceB"] if s5_conf else None}

    # ── S6 旗形 H1 + 突破偵測（approximate，參考用，TradingView 畫返準）──
    # 邏輯：旗杆 = 近期高位（旗杆頂）；整固區 = 旗杆頂之後嘅橫行；
    #       H1 ≈ 整固區內嘅最高收市（旗形上邊界 = 突破位）
    #       突破 = 今日 close 升穿 H1
    h1 = None
    pct_to_h1 = None
    broke_h1 = False
    flag_low = None
    flag_retrace = None   # 旗形回調佔上升推進浪幾多（Patreon: 要 ≤ 0.236）
    days_since_pole = None
    try:
        # 原文：「所有判斷用 Close，唔用日內 High/Low」——B點=急升最高Close，A點=急升前最低Close，
        # 旗形最低都係close嚟講，唔係intraday嘅high/low。之前呢度用緊seg_h/seg_l係錯嘅，而家改用seg_c。
        win = 20
        seg_c = closes[max(0, idx-win):idx+1]
        base = max(0, idx-win)
        if len(seg_c) >= 8:
            pole_top = max(seg_c[:-1])               # B點：旗杆頂（急升最高Close，唔計今日）
            pole_idx = seg_c.index(pole_top)         # 喺 segment 內位置
            days_since_pole = (len(seg_c) - 1) - pole_idx
            # A點：旗杆頂之前嘅最低 Close（上升推進浪起點）
            pole_low = min(seg_c[:pole_idx+1]) if pole_idx >= 1 else seg_c[0]
            # 整固區 = 旗杆頂之後嘅 bars（旗形喺旗杆後形成）
            consol_c = seg_c[pole_idx:]
            if len(consol_c) >= 2:
                h1 = max(consol_c[:-1]) if len(consol_c) > 1 else pole_top
                flag_low = min(consol_c) if consol_c else None
                pct_to_h1 = (close - h1) / h1 * 100
                # 回調比例 = (旗杆頂 - 旗形最低Close) / (旗杆頂 - 旗杆底)
                pole_height = pole_top - pole_low
                if pole_height > 0 and flag_low is not None:
                    flag_retrace = (pole_top - flag_low) / pole_height
                # 突破要真正嘅旗形先算：
                #   (1) 頸線起碼要形成咗5日以上（少過5日冧夠時間整固，只係「琴日高位今日升穿」）
                #   (2) 回調唔可以太深（>80%即係已經跌穿返旗杆起點，唔算旗形，係另一種結構）
                valid_flag_age = days_since_pole is not None and days_since_pole >= 4
                valid_flag_depth = flag_retrace is not None and flag_retrace <= 0.88
                broke_h1 = (close > h1 and relvol is not None and relvol > 1.3
                            and valid_flag_age and valid_flag_depth)
    except Exception:
        pass

    # ── S6 圖表形態（Required 4/5, Bonus 5/5）──
    # c4(整固)/c5(縮量) 檢查嘅係「突破前」嘅安靜狀態；一旦真係放量突破咗(broke_h1)，
    # 呢兩項自然會變假(唔再係整固、成交量都升返)——如果淨係死跟「4/5」，個股一突破反而會喺Watchlist度消失，
    # 變成獎勵緊「靜靜哋唔郁」，懲罰緊「做到你想佢做嘅事」。
    # 修正：c1/c2/c3(大趨勢/旗杆)永遠要過；c4/c5 就用「(c4 and c5) OR 已經放量突破」代替，
    # 即係「仲喺度靜靜整固」同「已經突破咗」兩種狀態，都算 ready，唔會突破一刻就跌出Watchlist。
    c4_raw = (perf1w is not None and -5 <= perf1w <= 0)
    c5_raw = (relvol is not None and relvol < 0.8)
    c = [
        close > e20,
        close > e50,
        (perf1m is not None and perf1m > 8),
        c4_raw or broke_h1,
        c5_raw or broke_h1,
    ]
    b = [
        (hiLo5 is not None and hiLo5 < 4.0),
        close > e200,
        (pct_from_high is not None and pct_from_high <= 15),
        (hiLo10 is not None and hiLo10 < 7.0),
        (perf3m is not None and perf3m > 15),
    ]
    c_vals = [
        round((close - e20) / e20 * 100, 2) if e20 else None,
        round((close - e50) / e50 * 100, 2) if e50 else None,
        round(perf1m, 1) if perf1m is not None else None,
        round(perf1w, 1) if perf1w is not None else None,
        round(relvol, 2) if relvol is not None else None,
    ]
    b_vals = [
        round(hiLo5, 2) if hiLo5 is not None else None,
        round((close - e200) / e200 * 100, 2) if e200 else None,
        round(pct_from_high, 1) if pct_from_high is not None else None,
        round(hiLo10, 2) if hiLo10 is not None else None,
        round(perf3m, 1) if perf3m is not None else None,
    ]
    res["S6"] = {"conds": c, "bonus": b, "score": sum(c), "bonusScore": sum(b),
                 # b6 veto：整固期間回調深度一旦超過 0.236（Patreon原文門檻），即刻唔算 ready，冇例外。
                 # 之前 valid_flag_depth 用緊 0.88 淨係一個好鬆嘅sanity check（防止回調到跌穿返旗杆起點），
                 # 唔係原文真正要求嘅0.236，兩者混埋一齊導致好多深回調嘅假旗形都當有效——而家分返開。
                 "ready": (sum(c) >= 4) and (flag_retrace is None or flag_retrace <= 0.236),
                 "condVals": c_vals, "bonusVals": b_vals,
                 "keyvals": {"1M%": round(perf1m, 1) if perf1m is not None else None, "1W%": round(perf1w, 1) if perf1w is not None else None}}
    res["S6"]["h1"] = round(h1, 2) if h1 else None
    res["S6"]["flagLow"] = round(flag_low, 2) if flag_low else None
    res["S6"]["pctToH1"] = round(pct_to_h1, 1) if pct_to_h1 is not None else None
    res["S6"]["brokeH1"] = broke_h1
    res["S6"]["flagRetrace"] = round(flag_retrace, 3) if flag_retrace is not None else None
    res["S6"]["daysSincePole"] = days_since_pole

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


# Sector 專屬定期數據（已查證嘅公開、規律性極強嘅發佈）。
# 逐個sector慢慢加，寧願少而準——未加入嘅sector，該股就唔會有呢類提示（同冇呢隻股一樣，唔會扮識）。
SECTOR_CALENDARS = {
    # EIA 石油庫存報告：逢星期三美東10:30am公布（幾十年慣例，遇假期先延遲去星期四）
    "Energy": {"name": "EIA石油庫存數據", "icon": "🛢️", "weekday": 2},
}


def get_sector_event(sector, today):
    """
    計返「今日」距離呢個sector最近相關數據事件幾多日（正=未來，負=已過，0=今日）。
    淨係支援 SECTOR_CALENDARS 入面已經查證咗嘅sector；其他sector回傳 None。
    """
    cal = SECTOR_CALENDARS.get(sector)
    if not cal:
        return None
    target_wd = cal["weekday"]
    cur_wd = today.weekday()
    if cur_wd == target_wd:
        days = 0
    else:
        days_ahead = (target_wd - cur_wd) % 7
        days_behind = (cur_wd - target_wd) % 7
        days = days_ahead if days_ahead <= days_behind else -days_behind
    return {"days": days, "name": cal["name"], "icon": cal["icon"]}


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
            strategies[s]["dipBreachEma50"] = today[s].get("dipBreachEma50", False)
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
        # S3 突破交易（Buildup）：pass-through 結構價位 + entry/stop/T1/T2
        if s == "S3":
            for k in ("buildupA", "buildupB", "buildupLow", "brokeToday", "rvolToday",
                      "methodAOk", "daysSinceB", "crossCount", "entry", "stop", "t1", "t2"):
                strategies[s][k] = today[s].get(k)
        # S4 假突破（淡倉）：pass-through 橫行區/假突破/entry/stop/T1-T3
        if s == "S4":
            for k in ("poleA", "rangeTop", "rangeLow", "entry", "stop", "t1", "t2", "t3",
                      "breakoutDuration", "daysSinceReturn", "polePct", "wickRatio", "brkRvol"):
                strategies[s][k] = today[s].get(k)
        # S5 支持阻力（Playbook confluence）：pass-through 結構價位 + entry/stop/T1/T2
        if s == "S5":
            for k in ("confluenceFound", "confA", "confB", "congesTop", "congesBottom",
                      "entry", "stop", "t1", "t2", "touched", "confFailed", "daysSinceB"):
                strategies[s][k] = today[s].get(k)
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


FOREX_PAIRS = ["EURUSD=X", "GBPUSD=X", "JPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X"]
FOREX_DISPLAY_NAMES = {"EURUSD=X": "EUR/USD", "GBPUSD=X": "GBP/USD", "JPY=X": "USD/JPY",
                        "AUDUSD=X": "AUD/USD", "USDCAD=X": "USD/CAD", "USDCHF=X": "USD/CHF",
                        "NZDUSD=X": "NZD/USD", "EURGBP=X": "EUR/GBP"}


def get_forex_s5_data():
    """
    S5(支持阻力被尊重)套用落8隻major貨幣對。呢個策略唔需要「持續單邊動能」——
    淨係需要一段直線向上嘅LTF升浪 + 之前有窄幅Congestion Area，現價回調到0.786同佢重疊。
    外匯係全世界機構參與最深嘅市場，「支持位受尊重」呢個邏輯（大戶以期望值思考，
    响高期望值進場點同時進場，形成支持）可能仲穩過股票。公式完全跟返detect_s5_confluence，
    冧使額外校準（因為呢個策略本身就係relative嘅位置關係，唔靠絕對百分比門檻）。
    """
    out = {}
    for fx in FOREX_PAIRS:
        try:
            fxh = fetch_history(fx)
            if not fxh or not fxh.get("close"):
                continue
            fc, fh, fl = fxh["close"], fxh["high"], fxh["low"]
            fidx = len(fc) - 1
            diag = {}
            conf = detect_s5_confluence(fidx, fh, fl, fc, diag_out=diag)
            out[fx] = {
                "display": FOREX_DISPLAY_NAMES.get(fx, fx),
                "close": round(fc[fidx], 5),
                "confluenceFound": conf is not None,
                "confA": conf["a"] if conf else None, "confB": conf["b"] if conf else None,
                "congesTop": conf["congesTop"] if conf else None, "congesBottom": conf["congesBottom"] if conf else None,
                "entry": conf["entry"] if conf else None, "stop": conf["stop"] if conf else None,
                "t1": conf["t1"] if conf else None, "t2": conf["t2"] if conf else None,
                "touched": conf["touched"] if conf else None, "confFailed": conf["failed"] if conf else None,
                "daysSinceB": conf["daysSinceB"] if conf else None,
                "diag": diag,
            }
        except Exception:
            continue
    return out


def get_forex_s1_data(forex_charts):
    """
    S1(順勢交易)套用落8隻major貨幣對。門檻已經用真實歷史分佈重新校準：
    3個月表現 >2%（原股票版 >5%，但外匯波幅天生細好多，2%約等於外匯自己嘅75百分位）
    1個月表現 <5%（原股票版 <25%，約等於外匯自己嘅90百分位，代表「未過熱」）
    其餘（EMA20/50/200、RSI 40-70）跟返股票版一樣嘅邏輯，因為呢啲係相對關係，唔受絕對波幅影響。
    forex_charts：傳入嘅dict，會寫低每對嘅圖表數據（最後120日），等前端可以開圖。
    """
    out = {}
    for fx in FOREX_PAIRS:
        try:
            fxh = fetch_history(fx)
            if not fxh or not fxh.get("close"):
                continue
            fc, fh, fl = fxh["close"], fxh["high"], fxh["low"]
            fo = fxh.get("open", fc)
            ft = fxh.get("time", [])
            fe20 = ema(fc, 20); fe50 = ema(fc, 50); fe200 = ema(fc, 200)
            fr = rsi(fc, 14); fa14 = atr(fh, fl, fc, 14)
            fidx = len(fc) - 1
            fclose = fc[fidx]
            fperf1m = (fc[fidx] - fc[fidx - 21]) / fc[fidx - 21] * 100 if fidx >= 21 else None
            fperf1w = (fc[fidx] - fc[fidx - 5]) / fc[fidx - 5] * 100 if fidx >= 5 else None
            fperf3m = (fc[fidx] - fc[fidx - 63]) / fc[fidx - 63] * 100 if fidx >= 63 else None
            f_e20v = fe20[fidx]; f_e50v = fe50[fidx]; f_e200v = fe200[fidx]; f_rv = fr[fidx]
            f_atr = fa14[fidx] if fa14[fidx] is not None else None
            c = [
                fclose > f_e20v if f_e20v else False,
                fclose > f_e50v if f_e50v else False,
                fclose > f_e200v if f_e200v else False,
                (40 <= f_rv <= 70) if f_rv is not None else False,
                (fperf3m is not None and fperf1m is not None and fperf3m > 2 and fperf1m < 5),
            ]
            b = [
                (f_e20v is not None and f_e50v is not None and f_e20v > f_e50v),
                (f_e50v is not None and f_e200v is not None and f_e50v > f_e200v),
                (fperf1w is not None and -1.5 <= fperf1w <= 0.5),
                (fperf3m is not None and fperf3m > 4),
            ]
            stop_ref = round(fclose - 1.5 * f_atr, 5) if f_atr else None

            # 跟返股票版S1同一套邏輯：track「幾多日前先跌穿返EMA20」——forex之前完全冇呢個dimension，
            # 淨係check緊「而家係咪健康」，令一個一個星期前已經返咗嚟嘅setup同今日先返嚟嘅冧分得開。
            f_pullback_touch = False
            f_pullback_days_ago = -1
            for back in range(0, 6):
                k = fidx - back
                if k < 0: break
                ek = fe20[k]
                if ek is not None and fc[k] < ek * 0.997:
                    f_pullback_touch = True
                    if f_pullback_days_ago < 0:
                        f_pullback_days_ago = back
            f_above_now = (f_e20v is not None and fclose >= f_e20v)

            out[fx] = {
                "display": FOREX_DISPLAY_NAMES.get(fx, fx),
                "close": round(fclose, 5), "score": sum(c), "bonusScore": sum(b),
                "ready": sum(c) == 5, "conds": c, "bonus": b,
                "perf1w": round(fperf1w, 2) if fperf1w is not None else None,
                "perf1m": round(fperf1m, 2) if fperf1m is not None else None,
                "perf3m": round(fperf3m, 2) if fperf3m is not None else None,
                "rsi": round(f_rv, 1) if f_rv is not None else None,
                "ema20": round(f_e20v, 5) if f_e20v else None,
                "atr14": round(f_atr, 5) if f_atr else None,
                "stopRef": stop_ref,
                "pullbackTouch": f_pullback_touch,
                "pullbackDaysAgo": f_pullback_days_ago,
                "aboveNow": f_above_now,
            }
            win = 120
            fstart = max(0, fidx - win)
            forex_charts[fx] = {
                "t": [ft[i] if i < len(ft) else 0 for i in range(fstart, fidx + 1)],
                "o": [round(fo[i], 5) for i in range(fstart, fidx + 1)],
                "h": [round(fh[i], 5) for i in range(fstart, fidx + 1)],
                "l": [round(fl[i], 5) for i in range(fstart, fidx + 1)],
                "c": [round(fc[i], 5) for i in range(fstart, fidx + 1)],
                "e20": [round(fe20[i], 5) if fe20[i] is not None else None for i in range(fstart, fidx + 1)],
                "e50": [round(fe50[i], 5) if fe50[i] is not None else None for i in range(fstart, fidx + 1)],
                "e200": [round(fe200[i], 5) if fe200[i] is not None else None for i in range(fstart, fidx + 1)],
            }
        except Exception:
            continue
    return out


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
                # 業績已經出咗（唔係未來）先計：由業績出咗嗰日之前收市，到今日收市，實際變咗幾多%
                # 呢個係已發生嘅事實，唔係預測「會升會跌」——單純話你知市場而家消化緊嘅方向同幅度
                rec["postEarningsReactionPct"] = None
                if edate and rec.get("daysToEarnings") is not None and rec["daysToEarnings"] < 0:
                    try:
                        times = hist.get("time", [])
                        e_ts = datetime.strptime(edate, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
                        # 揾返業績日（或之後）第一個交易日嘅 index，再退一日做「業績前收市」基準
                        idx_on_after = next((k for k, ts in enumerate(times) if ts >= e_ts), None)
                        if idx_on_after is not None and idx_on_after >= 1:
                            base_close = hist["close"][idx_on_after - 1]
                            cur_close = hist["close"][-1]
                            if base_close:
                                rec["postEarningsReactionPct"] = round((cur_close - base_close) / base_close * 100, 1)
                    except Exception:
                        pass
                rec["sectorEvent"] = get_sector_event(rec["sector"], datetime.now(timezone.utc).date())
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
