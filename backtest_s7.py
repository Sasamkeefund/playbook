#!/usr/bin/env python3
"""
S7（52週新高動能）3年 backtest — 跑喺 GitHub Actions（連到 Yahoo）
邏輯同 scan.py 一致：import scan 用佢嘅 S7 + indicators
入場：signal 隔日開市
止損：entry - 1.5×ATR(14)
目標：1:2 R:R
"""
import sys, json, datetime
sys.path.insert(0, ".")
import scan

scan.HISTORY_RANGE = "3y"
ATR_MULT = 1.5
RR = 2.0
MAX_HOLD = 120

def backtest_ticker(ticker, hist):
    closes=hist["close"]; highs=hist["high"]; lows=hist["low"]
    volumes=hist["volume"]; opens=hist["open"]; times=hist.get("time",[])
    n=len(closes)
    if n<70: return []
    ema20a=scan.ema(closes,20); ema50a=scan.ema(closes,50); ema200a=scan.ema(closes,200)
    rsia=scan.rsi(closes,14); atr5a=scan.atr(highs,lows,closes,5); atr14a=scan.atr(highs,lows,closes,14)
    trades=[]; busy_until=-1
    for idx in range(63,n-1):
        if idx<=busy_until: continue
        ev=scan.eval_strategies(idx,closes,highs,lows,volumes,ema20a,ema50a,ema200a,rsia,atr5a,atr14a)
        if ev is None: continue
        s7=ev.get("S7")
        if not s7 or not s7["ready"]: continue
        ei=idx+1
        if ei>=n: break
        entry=opens[ei]; atr14=atr14a[idx]
        if atr14 is None or atr14<=0 or entry<=0: continue
        stop=entry-ATR_MULT*atr14
        target=entry+RR*(entry-stop)
        if stop<=0 or stop>=entry: continue
        outcome=None; xi=None; xpx=None
        for j in range(ei,min(ei+MAX_HOLD,n)):
            if lows[j]<=stop: outcome="loss"; xi=j; xpx=stop; break
            if highs[j]>=target: outcome="win"; xi=j; xpx=target; break
        if outcome is None:
            xi=min(ei+MAX_HOLD,n)-1; xpx=closes[xi]; outcome="timeout"
        r=(xpx-entry)/(entry-stop); pct=(xpx-entry)/entry*100
        yr=None
        if times and ei<len(times) and times[ei]:
            yr=datetime.datetime.utcfromtimestamp(times[ei]).year
        trades.append({"ticker":ticker,"entry_idx":ei,"exit_idx":xi,"year":yr,
            "entry":round(entry,2),"stop":round(stop,2),"target":round(target,2),
            "exit":round(xpx,2),"outcome":outcome,"r":round(r,2),"pct":round(pct,1),
            "hold_days":xi-ei,"bonus":s7["bonusScore"],"pctFromHigh":s7.get("pctFromHigh")})
        busy_until=xi
    return trades

def max_drawdown_R(trades_sorted):
    # 以時間排序逐單累計 R，計最大回撤（R 單位）
    eq=0; peak=0; mdd=0; curve=[]
    for t in trades_sorted:
        eq+=t["r"]; peak=max(peak,eq); mdd=min(mdd,eq-peak); curve.append(round(eq,2))
    return round(mdd,2), curve

def summarize(trades):
    out=[]
    def p(s): out.append(s); print(s,flush=True)
    if not trades:
        p("冇 signal"); return "\n".join(out)
    n=len(trades)
    wins=[t for t in trades if t["outcome"]=="win"]
    losses=[t for t in trades if t["outcome"]=="loss"]
    tos=[t for t in trades if t["outcome"]=="timeout"]
    total_r=sum(t["r"] for t in trades); avg_r=total_r/n
    wr=len(wins)/n*100; avg_hold=sum(t["hold_days"] for t in trades)/n
    ts=sorted(trades,key=lambda x:(x["year"] or 0,x["entry_idx"]))
    mdd,curve=max_drawdown_R(ts)
    p("="*56)
    p("📊 S7 Backtest 結果（過去3年，S&P500，1:2 R:R）")
    p("="*56)
    p(f"  總交易：{n} 單")
    p(f"  勝率：{wr:.1f}%（贏 {len(wins)} / 輸 {len(losses)} / 未觸 {len(tos)}）")
    p(f"  每單期望值：{avg_r:+.2f}R  {'✅ 正期望' if avg_r>0 else '❌ 負期望'}")
    p(f"  3年累計：{total_r:+.1f}R")
    p(f"  最大回撤：{mdd:.1f}R")
    p(f"  平均持倉：{avg_hold:.0f} 交易日")
    p("\n  按年份：")
    for y in sorted(set(t["year"] for t in trades if t["year"])):
        g=[t for t in trades if t["year"]==y]
        gwr=len([t for t in g if t["outcome"]=="win"])/len(g)*100
        gr=sum(t["r"] for t in g)
        p(f"    {y}：{len(g)} 單，勝率 {gwr:.0f}%，{gr:+.1f}R")
    p("\n  按 Bonus 質素：")
    for b in range(6):
        g=[t for t in trades if t["bonus"]==b]
        if g:
            gwr=len([t for t in g if t["outcome"]=="win"])/len(g)*100
            gr=sum(t["r"] for t in g)/len(g)
            p(f"    Bonus {b}/5：{len(g)} 單，勝率 {gwr:.0f}%，期望 {gr:+.2f}R")
    return "\n".join(out)

def main():
    tickers=scan.get_sp500_tickers()
    print(f"S7 Backtest — {len(tickers)} 隻股，3年，1:2 R:R\n",flush=True)
    all_t=[]; done=0
    for t in tickers:
        try:
            h=scan.fetch_history(t)
            if h: all_t.extend(backtest_ticker(t,h))
        except Exception: pass
        done+=1
        if done%50==0: print(f"  ...{done}/{len(tickers)}，累計 {len(all_t)} 單",flush=True)
    json.dump(all_t,open("bt_s7_trades.json","w"))
    report=summarize(all_t)
    open("bt_s7_report.txt","w").write(report)
    # 資金曲線數據
    ts=sorted(all_t,key=lambda x:(x["year"] or 0,x["entry_idx"]))
    mdd,curve=max_drawdown_R(ts)
    # app 用嘅精簡統計 JSON
    n=len(all_t)
    wins=[t for t in all_t if t["outcome"]=="win"]
    losses=[t for t in all_t if t["outcome"]=="loss"]
    tos=[t for t in all_t if t["outcome"]=="timeout"]
    total_r=sum(t["r"] for t in all_t)
    years={}
    for y in sorted(set(t["year"] for t in all_t if t["year"])):
        g=[t for t in all_t if t["year"]==y]
        years[str(y)]={"n":len(g),"wr":round(len([t for t in g if t["outcome"]=="win"])/len(g)*100,1),"r":round(sum(t["r"] for t in g),1)}
    bonus={}
    for b in range(6):
        g=[t for t in all_t if t["bonus"]==b]
        if g:
            bonus[str(b)]={"n":len(g),"wr":round(len([t for t in g if t["outcome"]=="win"])/len(g)*100,1),"er":round(sum(t["r"] for t in g)/len(g),2)}
    # 圖太多點，sample 落 ~400 點
    step=max(1,len(curve)//400)
    curve_s=curve[::step]
    app={
        "strategy":"S7","name":"52週新高動能","rr":"1:2","period":"3年",
        "total":n,"wins":len(wins),"losses":len(losses),"timeouts":len(tos),
        "winRate":round(len(wins)/n*100,1) if n else 0,
        "expR":round(total_r/n,3) if n else 0,
        "totalR":round(total_r,1),"maxDD":mdd,
        "avgHold":round(sum(t["hold_days"] for t in all_t)/n) if n else 0,
        "years":years,"bonus":bonus,"curve":curve_s,
        "updated":datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    json.dump(app,open("bt_s7_app.json","w"))
    json.dump({"curve":curve,"report":report},open("bt_s7_summary.json","w"))

if __name__=="__main__":
    main()
