#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex 獨立掃描 —— 得8隻major貨幣對，S1(順勢交易)邏輯，好快跑完。
同美股個scan.py分開，用獨立嘅output file(forex_data.json/forex_charts.json)，
避免兩個workflow同時寫同一個檔案撞車。
"""
import json
from datetime import datetime, timezone
import scan  # 借用返scan.py已經寫好、驗證過嘅 fetch_history/ema/rsi/atr/get_forex_s1_data 等機器

FOREX_DATA_FILE = "forex_data.json"
FOREX_CHARTS_FILE = "forex_charts.json"


def main():
    charts = {}
    s1 = scan.get_forex_s1_data(charts)
    s5 = scan.get_forex_s5_data()
    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "forex": {"S1": s1, "S5": s5},
    }
    with open(FOREX_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    with open(FOREX_CHARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False, separators=(",", ":"))
    print(f"✅ 寫好 {FOREX_DATA_FILE} + {FOREX_CHARTS_FILE}（S1:{len(s1)}隻 S5:{len(s5)}隻）")


if __name__ == "__main__":
    main()
