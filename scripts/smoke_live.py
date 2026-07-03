"""正式環境最小單冒煙測試: XRP 空單 ~6U 名目, 開完即平。
用法: cd ~/crypto-signal-bot && set -a && source .env && set +a && EXECUTION_MODE=live venv/bin/python scripts/smoke_live.py
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import ExecCfg, RiskCfg  # noqa: E402
from bot.executor import BitgetExecutor, ExecError  # noqa: E402
from bot.ledger import Position  # noqa: E402
from bot.strategies import Signal  # noqa: E402

ex = BitgetExecutor(ExecCfg(), RiskCfg())
assert ex.cfg.mode == "live"
try:
    ex.setup_account()
    print("1. 單向持倉 OK")
except ExecError as e:
    print("1. 單向持倉設定失敗(帳戶有持倉時無法切換, 若本來就是單向可忽略):", e)

with urllib.request.urlopen(
        "https://api.bitget.com/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol=XRPUSDT",
        timeout=10) as r:
    px = float(json.loads(r.read())["data"][0]["lastPr"])
print("2. XRP 現價:", px)

sig = Signal("E", "XRPUSDT", -1, int(time.time() // 60), px, 180, "smoke")
pos = Position(sig, sig.minute, px, notional_usdt=6.0, exit_due=sig.minute + 180)
oid = ex.open(pos)
print("3. 開空成功 orderId:", oid)
time.sleep(3)
print("4. 交易所持倉:", ex.positions())
pos.exit_price = px
cid = ex.close(pos)
print("5. 平倉 orderId:", cid)
time.sleep(3)
print("6. 平倉後持倉:", ex.positions())
print("7. 期末權益:", ex.equity())
print("\n✅ 實盤下單鏈全通")
