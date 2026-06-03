import os, json, time, urllib.request
from datetime import datetime, timezone

FINNHUB_KEY = os.environ["FINNHUB_KEY"]

# S&P 500 stocks (major ones)
SP500 = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK.B","AVGO","JPM",
    "LLY","UNH","V","XOM","MA","COST","HD","PG","WMT","ABBV","BAC","MRK",
    "CVX","CRM","NFLX","AMD","ACN","TMO","PEP","ADBE","LIN","MCD","ABT",
    "TXN","DHR","NEE","PM","ORCL","DIS","QCOM","VZ","CMCSA","WFC","CAT",
    "INTU","IBM","AMGN","RTX","HON","SPGI","GS","ISRG","UNP","LOW","BKNG",
    "ELV","SYK","AMAT","T","MDT","GE","PLD","AXP","GILD","CVS","TJX","C",
    "VRTX","PGR","CI","REGN","MMC","AON","BDX","ZTS","CB","CME","LRCX",
    "ETN","BSX","MO","SO","DUK","ITW","SHW","CL","NOC","GD","EMR","FDX",
    "MCO","APD","FCX","NSC","PSA","WM","ECL","HCA","EW","MPC","EOG","PXD",
    "OXY","KMB","ROP","MSI","KLAC","SNPS","CDNS","MCHP","FTNT","PANW",
    "CRWD","DDOG","ZS","OKTA","NET","MDB","SNOW","WDAY","NOW","TEAM",
    "UBER","ABNB","DASH","LYFT","PINS","SNAP","RBLX","COIN","SQ","PYPL",
    "NKE","LULU","SBUX","CMG","YUM","QSR","DPZ","WING","MTN","MAR","HLT",
    "MGM","WYNN","LVS","CZR","PENN","DKNG","RSG","WCN","CPRT","VRSK",
    "IDXX","IQV","DGX","LH","MTD","WAT","A","KEYS","TRMB","TER","ENPH",
    "SEDG","FSLR","RUN","ARRY","NOVA","BE","PLUG","BLNK","CHPT","EVN"
]

def get_candles(symbol, days=400):
    """Get daily candles from Finnhub"""
    import time as t
    to_ts = int(t.time())
    from_ts = to_ts - days * 86400
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=D&from={from_ts}&to={to_ts}&token={FINNHUB_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("s") == "ok" and len(data.get("c", [])) >= 50:
            return data
    except:
        pass
    return None

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = 0, 0
    for i in range(-period, 0):
        diff = prices[i] - prices[i-1]
        if diff > 0: gains += diff
        else: losses += abs(diff)
    avg_g, avg_l = gains/period, losses/period
    if avg_l == 0: return 100
    return 100 - (100 / (1 + avg_g/avg_l))

def check_s1(symbol):
    data = get_candles(symbol)
    if not data:
        return None
    
    closes = data["c"]
    volumes = data["v"]
    
    price = closes[-1]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    ema200_10ago = calc_ema(closes[:-10], 200) if len(closes) > 210 else None
    rsi = calc_rsi(closes)
    
    if not all([ema20, ema50, ema200, rsi]):
        return None
    
    # Performance
    perf3m = (price - closes[-63]) / closes[-63] * 100 if len(closes) >= 63 else 0
    perf1m = (price - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0
    perf1w = (price - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    
    # RelVol
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
    rel_vol = volumes[-1] / avg_vol if avg_vol > 0 else 0
    
    # EMA slope
    ema200_slope = (ema200 - ema200_10ago) if ema200_10ago else 0
    
    # Required conditions
    c1 = price > ema20
    c2 = price > ema50
    c3 = price > ema200 and ema200_slope > 0
    c4 = 40 <= rsi <= 70
    c5 = perf3m > 5 and perf1m < 25
    
    req_score = sum([c1, c2, c3, c4, c5])
    
    # Bonus conditions
    b1 = ema20 > ema50
    b2 = ema50 > ema200
    b3 = rel_vol < 0.9
    b4 = -8 <= perf1w <= 0
    
    high52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    pct_from_high = (price - high52w) / high52w * 100
    b5 = pct_from_high > -15
    
    bon_score = sum([b1, b2, b3, b4, b5])
    
    if req_score < 5:
        return None
    if bon_score <= 2:
        return None
        
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema200": round(ema200, 2),
        "rsi": round(rsi, 1),
        "relVol": round(rel_vol, 2),
        "perf3m": round(perf3m, 1),
        "perf1m": round(perf1m, 1),
        "perf1w": round(perf1w, 1),
        "pctFromHigh": round(pct_from_high, 1),
        "reqScore": req_score,
        "bonScore": bon_score,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5,
        "b1": b1, "b2": b2, "b3": b3, "b4": b4, "b5": b5,
        "quality": "high" if bon_score >= 4 else "mid"
    }

print(f"Scanning {len(SP500)} stocks...")
results = []
for i, symbol in enumerate(SP500):
    result = check_s1(symbol)
    if result:
        results.append(result)
        print(f"✅ {symbol} - Req:{result['reqScore']}/5 Bon:{result['bonScore']}/5")
    else:
        print(f"   {symbol} - skip")
    time.sleep(0.5)  # Rate limit: 60 calls/min free tier

output = {
    "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "count": len(results),
    "stocks": sorted(results, key=lambda x: x["bonScore"], reverse=True)
}

with open("data.json", "w") as f:
    json.dump(output, f, indent=2)

print(f"\n✅ Done! {len(results)} stocks passed S1 conditions")
