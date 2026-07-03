"""Bitget USDT-M 合約實盤執行器。

- EXECUTION_MODE=paper（預設, 不下真單）/ demo（Demo API key + paptrading:1）/ live
- 開單: 逐倉 + 5x（各幣上限取 min）+ 市價單 + presetStopLossPrice 掛交易所端災難停損
- 帳戶級: 啟動時切單向持倉（one_way_mode）
- 出場: 到期由程式市價平倉（reduceOnly）; 停損由交易所條件單先行, 程式平倉冪等
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.request
from pathlib import Path

from .config import ExecCfg, RiskCfg
from .ledger import Position

BASE = "https://api.bitget.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PRODUCT = "USDT-FUTURES"


class ExecError(Exception):
    pass


class BitgetExecutor:
    """mode=demo 走 Bitget 模擬盤環境: productType=SUSDT-FUTURES、幣對 S<base>SUSDT、
    保證金幣 SUSDT（僅 BTC/ETH/XRP 三幣對, 只夠驗證下單鏈, 跑不了全策略）。"""

    def __init__(self, cfg: ExecCfg, risk: RiskCfg):
        self.cfg = cfg
        self.risk = risk
        self._prepped: set[str] = set()  # 已設定逐倉+槓桿的幣
        if cfg.mode == "demo":
            self.product, self.margin_coin = "SUSDT-FUTURES", "SUSDT"
            with urllib.request.urlopen(
                    f"{BASE}/api/v2/mix/market/contracts?productType={self.product}",
                    timeout=10) as r:
                raw = json.loads(r.read())["data"]
        else:
            self.product, self.margin_coin = PRODUCT, "USDT"
            raw = json.loads((DATA_DIR / "bitget_contracts.json").read_text())["data"]
        self.contracts = {r["symbol"]: r for r in raw}

    def map_symbol(self, sym: str) -> str:
        """引擎用正式盤符號 (TLMUSDT); demo 環境轉 S 前綴 (STLMSUSDT)。"""
        if self.cfg.mode != "demo":
            return sym
        return f"S{sym[:-4]}SUSDT" if sym.endswith("USDT") else sym

    # ---- 簽名與請求 ----
    def _req(self, method: str, path: str, body: dict | None = None,
             query: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        payload = json.dumps(body) if body else ""
        pre = ts + method + path + (f"?{query}" if query else "") + payload
        sign = base64.b64encode(
            hmac.new(self.cfg.api_secret.encode(), pre.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "ACCESS-KEY": self.cfg.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-PASSPHRASE": self.cfg.passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }
        if self.cfg.mode == "demo":
            headers["paptrading"] = "1"
        url = BASE + path + (f"?{query}" if query else "")
        req = urllib.request.Request(url, data=payload.encode() if payload else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            out = json.loads(e.read())
        if out.get("code") != "00000":
            raise ExecError(f"{path}: {out.get('code')} {out.get('msg')}")
        return out

    # ---- 帳戶/幣種前置 ----
    def setup_account(self) -> None:
        self._req("POST", "/api/v2/mix/account/set-position-mode",
                  {"productType": self.product, "posMode": "one_way_mode"})

    def _prep_symbol(self, sym: str) -> None:
        if sym in self._prepped:
            return
        c = self.contracts.get(sym)
        lev = int(min(self.risk.leverage, float(c["maxLever"]))) if c else int(self.risk.leverage)
        try:
            self._req("POST", "/api/v2/mix/account/set-margin-mode",
                      {"symbol": sym, "productType": self.product,
                       "marginCoin": self.margin_coin, "marginMode": "isolated"})
        except ExecError as e:  # 有持倉時不能改, 模式本來就對則無妨
            print(f"set-margin-mode {sym}: {e}", file=sys.stderr)
        self._req("POST", "/api/v2/mix/account/set-leverage",
                  {"symbol": sym, "productType": self.product,
                   "marginCoin": self.margin_coin, "leverage": str(lev)})
        self._prepped.add(sym)

    # ---- 數量/價格精度 ----
    def _round_size(self, sym: str, notional: float, price: float) -> float:
        c = self.contracts[sym]
        step = float(c["sizeMultiplier"])
        vp = int(c["volumePlace"])
        size = max(float(c["minTradeNum"]), (notional / price) // step * step)
        size = round(size, vp)
        if size * price < float(c.get("minTradeUSDT", 5)):
            raise ExecError(f"{sym} 名目 {size * price:.2f}U 低於下限")
        return size

    def _round_price(self, sym: str, px: float) -> str:
        pp = int(self.contracts[sym]["pricePlace"])
        return f"{px:.{pp}f}"

    # ---- 開/平倉 ----
    def open(self, pos: Position) -> str:
        sym = self.map_symbol(pos.signal.symbol)
        if sym not in self.contracts:
            raise ExecError(f"{sym} 不在 Bitget 合約表")
        self._prep_symbol(sym)
        size = self._round_size(sym, pos.notional_usdt, pos.entry_price)
        stop_px = pos.entry_price * (1 - pos.signal.side * self.risk.disaster_stop_bps / 10000)
        body = {
            "symbol": sym, "productType": self.product, "marginMode": "isolated",
            "marginCoin": self.margin_coin, "size": str(size),
            "side": "buy" if pos.signal.side > 0 else "sell",
            "orderType": "market",
            "presetStopLossPrice": self._round_price(sym, stop_px),
            "clientOid": f"esig-{pos.signal.strategy}-{pos.entry_minute}",
        }
        out = self._req("POST", "/api/v2/mix/order/place-order", body)
        return out["data"]["orderId"]

    def close(self, pos: Position) -> str | None:
        """市價全平（reduceOnly, 冪等: 交易所停損已先平則吞掉 no-position 錯誤）。"""
        sym = self.map_symbol(pos.signal.symbol)
        c = self.contracts[sym]
        size = self._round_size(sym, pos.notional_usdt, pos.exit_price or pos.entry_price)
        body = {
            "symbol": sym, "productType": self.product, "marginMode": "isolated",
            "marginCoin": self.margin_coin, "size": str(size),
            "side": "sell" if pos.signal.side > 0 else "buy",
            "orderType": "market", "reduceOnly": "YES",
        }
        try:
            out = self._req("POST", "/api/v2/mix/order/place-order", body)
            return out["data"]["orderId"]
        except ExecError as e:
            if "22002" in str(e) or "No position" in str(e):  # 已被停損單平掉
                return None
            raise

    # ---- 對帳 ----
    def positions(self) -> dict[str, float]:
        """交易所實際持倉 sym -> 帶方向 size, 供與本地帳本對帳。"""
        out = self._req("GET", "/api/v2/mix/position/all-position",
                        query=f"productType={self.product}&marginCoin={self.margin_coin}")
        res = {}
        for p in out.get("data", []):
            sz = float(p.get("total", 0))
            if sz:
                res[p["symbol"]] = sz * (1 if p.get("holdSide") == "long" else -1)
        return res

    def equity(self) -> float:
        out = self._req("GET", "/api/v2/mix/account/accounts",
                        query=f"productType={self.product}")
        for a in out.get("data", []):
            if a.get("marginCoin") == self.margin_coin:
                return float(a.get("accountEquity", 0))
        return 0.0
