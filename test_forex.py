import sys
sys.path.insert(0, '/home/claude/playbook')
import scan

pairs = ["EURUSD=X", "GBPUSD=X", "JPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X"]
for p in pairs:
    h = scan.fetch_history(p)
    if h and h.get('close'):
        print(f"✅ {p}: {len(h['close'])}日, 最新close={h['close'][-1]:.5f}")
    else:
        print(f"❌ {p}: 攞唔到")
