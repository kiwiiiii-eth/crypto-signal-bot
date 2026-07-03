"""Bitget 執行鏈冒煙測試（demo 環境, SBTCSUSDT 最小單）:
簽名 → 單向持倉 → 權益 → 逐倉+槓桿 → 市價開空(帶-8%停損) → 查倉 → reduceOnly 平倉。
用法: 在專案根目錄  set -a && source .env && set +a && venv/bin/python scripts/smoke_exec.py
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import ExecCfg, RiskCfg  # noqa: E402
from bot.executor import BitgetExecutor  # noqa: E402
from bot.ledger import Position  # noqa: E402
from bot.strategies import Signal  # noqa: E402

cfg = ExecCfg()
assert cfg.mode == "demo", f"EXECUTION_MODE={cfg.mode}, 冒煙測試僅限 demo"
ex = BitgetExecutor(cfg, RiskCfg())
print("1. 合約表:", list(ex.contracts))

ex.setup_account()
print("2. 單向持倉 OK")
print("3. 模擬盤權益:", ex.equity(), ex.margin_coin)

with urllib.request.urlopen(
        "https://api.bitget.com/api/v2/mix/market/ticker?productType=SUSDT-FUTURES&symbol=SBTCSUSDT",
        timeout=10) as r:
    px = float(json.loads(r.read())["data"][0]["lastPr"])
print("4. SBTC 現價:", px)

sig = Signal("E", "BTCUSDT", -1, int(time.time() // 60), px, 180, "smoke")
pos = Position(sig, sig.minute, px, notional_usdt=250.0, exit_due=sig.minute + 180)
oid = ex.open(pos)
print("5. 開空成功 orderId:", oid)

time.sleep(3)
p = ex.positions()
print("6. 交易所持倉:", p)

pos.exit_price = px
cid = ex.close(pos)
print("7. 平倉 orderId:", cid)
time.sleep(3)
print("8. 平倉後持倉:", ex.positions())
print("9. 期末權益:", ex.equity())
print("\n✅ 冒煙測試全通")
