import os, json, time, urllib.request
from datetime import datetime, timezone

# Full S&P 500
SP500 = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB",
    "AKAM","ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN",
    "AMCR","AEE","AAL","AEP","AXP","AIG","AMT","AWK","AMP","AME","AMGN",
    "APH","ADI","ANSS","AON","APA","AAPL","AMAT","APTV","ACGL","ADM","ANET",
    "AJG","AIZ","T","ATO","ADSK","ADP","AZO","AVB","AVY","AXON","BKR","BALL",
    "BAC","BK","BBWI","BAX","BDX","WRB","BBY","BIO","TECH","BIIB","BLK","BX",
    "BA","BKNG","BWA","BSX","BMY","AVGO","BR","BRO","BF.B","BLDR","BG","CDNS",
    "CZR","CPT","CPB","COF","CAH","KMX","CCL","CARR","CTLT","CAT","CBOE","CBRE",
    "CDW","CE","COR","CNC","CNX","CDAY","CF","CRL","SCHW","CHTR","CVX","CMG",
    "CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX","CME","CMS","KO",
    "CTSH","CL","CMCSA","CMA","CAG","COP","ED","STZ","CEG","COO","CPRT","GLW",
    "CTVA","CSGP","COST","CTRA","CCI","CSX","CMI","CVS","DHI","DHR","DRI",
    "DVA","DAY","DECK","DE","DAL","DVN","DXCM","FANG","DLR","DFS","DG","DLTR",
    "D","DPZ","DOV","DOW","DHI","DTE","DUK","DD","EMN","ETN","EBAY","ECL",
    "EIX","EW","EA","ELV","LLY","EMR","ENPH","ETR","EOG","EPAM","EQT","EFX",
    "EQIX","EQR","ESS","EL","ETSY","EG","EVRG","ES","EXC","EXPE","EXPD","EXR",
    "XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB","FSLR","FE",
    "FI","FMC","F","FTNT","FTV","FOXA","FOX","BEN","FCX","GRMN","IT","GE",
    "GEHC","GEV","GEN","GNRC","GD","GIS","GM","GPC","GILD","GPN","GL","GS",
    "HAL","HIG","HAS","HCA","DOC","HSIC","HSY","HES","HPE","HLT","HOLX","HD",
    "HON","HRL","HST","HWM","HPQ","HUBB","HUM","HBAN","HII","IBM","IEX","IDXX",
    "ITW","INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU","ISRG","IVZ",
    "INVH","IQV","IRM","JBAL","JKHY","J","JBL","JNPR","JCI","JPM","JNPR",
    "K","KVUE","KDP","KEY","KEYS","KMB","KIM","KMI","KLAC","KHC","KR","LHX",
    "LH","LRCX","LW","LVS","LDOS","LEN","LIN","LYV","LKQ","LMT","L","LOW",
    "LULU","LYB","MTB","MRO","MPC","MKTX","MAR","MMC","MLM","MAS","MA","MTCH",
    "MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM","MCHP","MU","MSFT",
    "MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR","MNST","MCO","MS","MOS","MSI",
    "MSCI","NDAQ","NTAP","NOV","NFLX","NEM","NWSA","NWS","NEE","NKE","NI",
    "NDSN","NSC","NTRS","NOC","NCLH","NRG","NUE","NVDA","NVR","NXPI","ORLY",
    "OXY","ODFL","OMC","ON","OKE","ORCL","OTIS","PCAR","PKG","PLTR","PH",
    "PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG","PM","PSX","PNW","PXD","PNC",
    "POOL","PPG","PPL","PFG","PG","PGR","PRU","PEG","PTC","PSA","PHM","QRVO",
    "PWR","QCOM","DGX","RL","RJF","RTX","O","REG","REGN","RF","RSG","RMD",
    "RVTY","ROK","ROL","ROP","ROST","RCL","SPGI","CRM","SBAC","SLB","STX",
    "SRE","NOW","SHW","SPG","SWKS","SJM","SNA","SOLV","SO","LUV","SWK","SBUX",
    "STT","STLD","STE","SYK","SMCI","SYF","SNPS","SYY","TMUS","TROW","TTWO",
    "TPR","TRGP","TGT","TEL","TDY","TFX","TER","TSLA","TXN","TXT","TMO","TJX",
    "TSCO","TT","TDG","TRV","TRMB","TFC","TYL","TSN","USB","UBER","UDR","ULTA",
    "UNP","UAL","UPS","URI","UNH","UHS","VLO","VTR","VLTO","VRSN","VRSK","VZ",
    "VRTX","VTRS","VICI","V","VST","VFC","VTRS","WRB","GWW","WAB","WBA","WMT",
    "DIS","WBD","WM","WAT","WEC","WFC","WELL","WST","WDC","WY","WHR","WMB",
    "WTW","GWW","WYNN","XEL","XYL","YUM","ZBRA","ZBH","ZTS"
]

# Remove duplicates
SP500 = list(dict.fromkeys(SP500))

def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2y"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        volumes = [v for v in result["indicators"]["quote"][0]["volume"] if v is not None]
        if len(closes) < 60:
            return None
        return {"closes": closes, "volumes": volumes}
    except:
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
    data = fetch_yahoo(symbol)
    if not data:
        return None
    closes = data["closes"]
    volumes = data["volumes"]
    price = closes[-1]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    ema200_10ago = calc_ema(closes[:-10], 200) if len(closes) > 210 else None
    rsi = calc_rsi(closes)
    if not all([ema20, ema50, ema200, rsi]):
        return None
    perf3m = (price - closes[-63]) / closes[-63] * 100 if len(closes) >= 63 else 0
    perf1m = (price - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0
    perf1w = (price - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
    rel_vol = volumes[-1] / avg_vol if avg_vol > 0 else 0
    ema200_slope = (ema200 - ema200_10ago) if ema200_10ago else 0
    c1 = price > ema20
    c2 = price > ema50
    c3 = price > ema200 and ema200_slope > 0
    c4 = 40 <= rsi <= 70
    c5 = perf3m > 5 and perf1m < 25
    req_score = sum([c1, c2, c3, c4, c5])
    b1 = ema20 > ema50
    b2 = ema50 > ema200
    b3 = rel_vol < 0.9
    b4 = -8 <= perf1w <= 0
    high52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    pct_from_high = (price - high52w) / high52w * 100
    b5 = pct_from_high > -15
    bon_score = sum([b1, b2, b3, b4, b5])
    if req_score < 5 or bon_score <= 2:
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
        print(f"PASS {symbol} Req:{result['reqScore']}/5 Bon:{result['bonScore']}/5")
    else:
        print(f"skip {symbol}")
    time.sleep(0.2)

output = {
    "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "count": len(results),
    "stocks": sorted(results, key=lambda x: x["bonScore"], reverse=True)
}

with open("data.json", "w") as f:
    json.dump(output, f, indent=2)

print(f"Done! {len(results)} stocks passed S1 conditions")
