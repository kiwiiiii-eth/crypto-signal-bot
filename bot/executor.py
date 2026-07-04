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
        try:
            self._req("POST", "/api/v2/mix/account/set-position-mode",
                      {"productType": self.product, "posMode": "one_way_mode"})
        except ExecError as e:
            # 40920: 有持倉/掛單（含手動單）時不能切換; 模式先前已是 one_way, 照常啟動
            if "40920" not in str(e):
                raise
            print(f"set-position-mode 略過: {e}", file=sys.stderr)

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

    def funding_rate(self, sym_raw: str) -> float | None:
        """Bitget 當期資金費率（實際收付發生在這裡, 不是訊號源 Binance）。"""
        sym = self.map_symbol(sym_raw)
        url = (f"{BASE}/api/v2/mix/market/current-fund-rate"
               f"?symbol={sym}&productType={self.product}")
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read()).get("data") or []
        return float(d[0]["fundingRate"]) if d else None

    def ticker(self, sym: str) -> dict:
        """Bitget 盤口: last/bid/ask（公開接口, 無需簽名）。"""
        url = f"{BASE}/api/v2/mix/market/ticker?symbol={sym}&productType={self.product}"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())["data"][0]
        return {"last": float(d["lastPr"]),
                "bid": float(d.get("bidPr") or 0), "ask": float(d.get("askPr") or 0)}

    # ---- 下單核心 ----
    def _place(self, sym: str, side: str, size: float, order_type: str,
               price: float | None = None, post_only: bool = False,
               reduce: bool = False, stop_px: float | None = None,
               client_oid: str | None = None) -> str:
        body = {
            "symbol": sym, "productType": self.product, "marginMode": "isolated",
            "marginCoin": self.margin_coin, "size": str(size),
            "side": side, "orderType": order_type,
        }
        if price is not None:
            body["price"] = self._round_price(sym, price)
        if post_only:
            body["force"] = "post_only"
        if reduce:
            body["reduceOnly"] = "YES"
        if stop_px is not None:
            body["presetStopLossPrice"] = self._round_price(sym, stop_px)
        if client_oid:
            body["clientOid"] = client_oid
        out = self._req("POST", "/api/v2/mix/order/place-order", body)
        return out["data"]["orderId"]

    def _detail(self, sym: str, order_id: str) -> dict:
        out = self._req("GET", "/api/v2/mix/order/detail",
                        query=f"symbol={sym}&productType={self.product}&orderId={order_id}")
        return out.get("data") or {}

    def _cancel(self, sym: str, order_id: str) -> None:
        try:
            self._req("POST", "/api/v2/mix/order/cancel-order",
                      {"symbol": sym, "productType": self.product, "orderId": order_id})
        except ExecError:
            pass  # 撤單瞬間剛好成交 → 後面 _detail 會看到 filled

    def _floor_size(self, sym: str, size: float) -> float:
        c = self.contracts[sym]
        step = float(c["sizeMultiplier"])
        return round(size // step * step, int(c["volumePlace"]))

    def _execute(self, sym: str, side: str, size: float, ref_px: float,
                 chase: int, market_fallback: bool,
                 reduce: bool = False, stop_px: float | None = None,
                 client_oid: str | None = None) -> dict:
        """純 maker 執行: post-only 貼盤口, 逾時撤單重新貼價, 追掛 chase 輪。
        market_fallback=True 時(僅平倉)用盡輪數才市價兜底。
        回傳聚合成交 {orderId, price, size, fee}。"""
        fills: list[tuple[float, float, float]] = []  # (px, sz, fee)
        remaining, main_oid = size, ""
        min_num = float(self.contracts[sym]["minTradeNum"])

        def _collect(oid: str) -> float:
            d = self._detail(sym, oid)
            fz = float(d.get("baseVolume") or 0)
            if fz > 0:
                fills.append((float(d.get("priceAvg") or ref_px), fz,
                              abs(float(d.get("fee") or 0))))
            return fz

        rounds = chase if self.cfg.maker_first else 0
        for rd in range(rounds):
            if remaining < min_num:
                break
            try:
                tk = self.ticker(sym)
                join = (tk["bid"] if side == "buy" else tk["ask"]) or tk["last"]
                oid = self._place(sym, side, remaining, "limit", price=join,
                                  post_only=True, reduce=reduce, stop_px=stop_px,
                                  client_oid=f"{client_oid}-{rd}" if client_oid else None)
            except ExecError as e:
                # post-only 會穿價被拒 → 盤口動了, 下一輪重新貼價
                print(f"maker 掛單被拒({rd + 1}/{rounds}) {sym}: {e}", file=sys.stderr)
                time.sleep(2)
                continue
            main_oid = main_oid or oid
            deadline = time.time() + self.cfg.maker_wait_sec
            state = ""
            while time.time() < deadline:
                time.sleep(2)
                state = self._detail(sym, oid).get("state", "")
                if state in {"filled", "canceled", "cancelled"}:
                    break
            if state != "filled":
                self._cancel(sym, oid)
                time.sleep(1)
            remaining = self._floor_size(sym, max(0.0, remaining - _collect(oid)))

        if remaining >= min_num:
            if not market_fallback:
                if not fills:
                    raise ExecError(f"{sym} maker {rounds} 輪未成交, 棄單")
                print(f"{sym} maker 部分成交 {size - remaining}/{size}, "
                      f"殘量放棄", file=sys.stderr)
            else:
                oid = self._place(sym, side, remaining, "market", reduce=reduce,
                                  stop_px=stop_px,
                                  client_oid=f"{client_oid}-mkt" if client_oid else None)
                main_oid = main_oid or oid
                for _ in range(5):
                    time.sleep(1)
                    if _collect(oid):
                        break
        elif not fills:
            raise ExecError(f"{sym} 無任何成交")

        tot = sum(sz for _, sz, _ in fills)
        avg = sum(px * sz for px, sz, _ in fills) / tot if tot else ref_px
        return {"orderId": main_oid, "price": avg, "size": tot,
                "fee": sum(f for _, _, f in fills)}

    # ---- 開/平倉 ----
    def open(self, pos: Position) -> dict:
        sym = self.map_symbol(pos.signal.symbol)
        if sym not in self.contracts:
            raise ExecError(f"{sym} 不在 Bitget 合約表")
        # 盤口比價: Binance 訊號價 vs Bitget 現價, 偏差過大=已被追走或兩所脫鉤 → 棄單
        tk = self.ticker(sym)
        # 實際成交參考: 買看 ask、賣看 bid
        exec_px = (tk["ask"] if pos.signal.side > 0 else tk["bid"]) or tk["last"]
        dev_bps = (exec_px / pos.entry_price - 1) * 10000
        # 不利方向 = 買得更貴 / 賣(空)得更便宜
        adverse = dev_bps * pos.signal.side
        if adverse > self.cfg.max_dev_bps:
            raise ExecError(f"{sym} Bitget 盤口偏差 {dev_bps:+.0f}bps "
                            f"(Binance {pos.entry_price:.6g} → Bitget {exec_px:.6g}) 棄單")
        self._prep_symbol(sym)
        # 數量與停損以 Bitget 實際盤口為基準, 而非 Binance 訊號價
        size = self._round_size(sym, pos.notional_usdt, exec_px)
        stop_px = exec_px * (1 - pos.signal.side * self.risk.disaster_stop_bps / 10000)
        # 開倉: 純 maker, 追掛 entry_chase 輪掛不到就棄單, 絕不吃市價
        return self._execute(sym, "buy" if pos.signal.side > 0 else "sell", size,
                             exec_px, chase=self.cfg.entry_chase, market_fallback=False,
                             stop_px=stop_px,
                             client_oid=f"esig-{pos.signal.strategy}-{pos.entry_minute}")

    def _flash_close(self, sym: str) -> None:
        """市價閃電全平（不受 minTradeNum 限制）, 用來掃掉 maker 殘量灰塵倉。"""
        try:
            self._req("POST", "/api/v2/mix/order/close-positions",
                      {"symbol": sym, "productType": self.product})
        except ExecError as e:
            if "22002" not in str(e) and "No position" not in str(e):
                raise

    def close(self, pos: Position) -> str | None:
        """全平（maker 優先→市價, reduceOnly, 冪等: 停損已先平則吞 no-position）。"""
        sym = self.map_symbol(pos.signal.symbol)
        held = self.positions().get(sym)
        if not held:  # 已被交易所停損單平掉
            return None
        size = self._floor_size(sym, abs(held))
        oid = None
        if size > 0:
            try:
                # 平倉: maker 追掛 close_chase 輪, 用盡才市價兜底(倉位不能裸奔)
                res = self._execute(sym, "sell" if pos.signal.side > 0 else "buy",
                                    size, pos.exit_price or pos.entry_price,
                                    chase=self.cfg.close_chase, market_fallback=True,
                                    reduce=True)
                oid = res["orderId"]
            except ExecError as e:
                if "22002" not in str(e) and "No position" not in str(e):
                    raise
                return None
        # 殘量掃尾: 部分成交剩 < minTradeNum、或 floor 捨去的尾數, 一律閃電平掉
        leftover = self.positions().get(sym)
        if leftover:
            print(f"{sym} 平倉後殘量 {leftover}, 閃電平倉掃尾", file=sys.stderr)
            self._flash_close(sym)
        return oid

    # ---- 真實成交回查 ----
    def fill_info(self, sym_raw: str, order_id: str, tries: int = 5) -> dict | None:
        """查訂單實際成交: 均價/數量/手續費。未成交回 None。"""
        sym = self.map_symbol(sym_raw)
        for i in range(tries):
            out = self._req("GET", "/api/v2/mix/order/detail",
                            query=f"symbol={sym}&productType={self.product}&orderId={order_id}")
            d = out.get("data") or {}
            if d.get("state") == "filled" and float(d.get("priceAvg") or 0) > 0:
                return {"price": float(d["priceAvg"]),
                        "size": float(d.get("baseVolume") or 0),
                        "fee": abs(float(d.get("fee") or 0))}
            if i < tries - 1:
                time.sleep(1)
        return None

    def last_closed(self, sym_raw: str) -> dict | None:
        """該幣最近一筆已平倉位的交易所結算數字（含 funding 與開平手續費）。"""
        sym = self.map_symbol(sym_raw)
        out = self._req("GET", "/api/v2/mix/position/history-position",
                        query=f"symbol={sym}&productType={self.product}&limit=1")
        lst = (out.get("data") or {}).get("list") or []
        if not lst:
            return None
        r = lst[0]
        return {"close_price": float(r.get("closeAvgPrice") or 0),
                "open_price": float(r.get("openAvgPrice") or 0),
                "net": float(r.get("netProfit") or 0),        # 含 funding+費用的淨損益
                "funding": float(r.get("totalFunding") or 0),
                "fee": abs(float(r.get("openFee") or 0)) + abs(float(r.get("closeFee") or 0)),
                "utime": int(r.get("utime") or 0)}

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
