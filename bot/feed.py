"""Binance 期貨實時數據源（不依賴 InfluxDB）。

每分鐘一個 tick：
- mark_price + funding_rate：/fapi/v1/premiumIndex 一次全市場
- open_interest：/fapi/v1/openInterest 逐檔並發掃描（約 20 req/s）

在記憶體維持每幣的分鐘級滾動序列，長度足夠餵最長窗口的策略。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

BASE = "https://fapi.binance.com"


def _get(path: str, timeout: float = 10) -> dict | list:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "crypto-signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def binance_usdt_perps() -> set[str]:
    info = _get("/fapi/v1/exchangeInfo", timeout=20)
    return {s["symbol"] for s in info["symbols"]
            if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"}


class BinanceFeed:
    BENCH = "BTCUSDT"  # 大盤基準, 不在訊號 universe 也要追蹤(regime 濾網用)

    def __init__(self, universe: set[str], keep_min: int = 300, oi_workers: int = 20):
        self.universe = sorted(universe)
        self.keep = keep_min
        self.pool = ThreadPoolExecutor(max_workers=oi_workers)
        self.px: dict[str, deque] = {s: deque(maxlen=keep_min) for s in self.universe}
        self.oi: dict[str, deque] = {s: deque(maxlen=keep_min) for s in self.universe}
        self.fr: dict[str, float] = {}
        self.bench_px: deque = deque(maxlen=keep_min)
        self.last_minute: int | None = None

    def _fetch_oi(self, sym: str) -> float:
        try:
            return float(_get(f"/fapi/v1/openInterest?symbol={sym}")["openInterest"])
        except Exception:
            return float("nan")

    def tick(self, minute: int) -> None:
        """抓一輪快照並 append。缺漏補 NaN 讓索引對齊。"""
        try:
            prem = {d["symbol"]: d for d in _get("/fapi/v1/premiumIndex", timeout=20)}
        except Exception as e:
            print(f"premiumIndex 失敗: {e}", file=sys.stderr)
            prem = {}
        ois = dict(zip(self.universe, self.pool.map(self._fetch_oi, self.universe)))
        gap = 1 if self.last_minute is None else max(1, minute - self.last_minute)
        b = prem.get(self.BENCH)
        for _ in range(gap - 1):
            self.bench_px.append(float("nan"))
        self.bench_px.append(float(b["markPrice"]) if b else float("nan"))
        for sym in self.universe:
            p = prem.get(sym)
            px = float(p["markPrice"]) if p else float("nan")
            if p:
                self.fr[sym] = float(p["lastFundingRate"])
            for _ in range(gap - 1):  # 掉拍補 NaN
                self.px[sym].append(float("nan"))
                self.oi[sym].append(float("nan"))
            self.px[sym].append(px)
            self.oi[sym].append(ois.get(sym, float("nan")))
        self.last_minute = minute

    def arrays(self, sym: str):
        """回傳 (px, oi, fr) numpy 序列，最後一格 = 最新 tick。"""
        px = np.asarray(self.px[sym], dtype=float)
        oi = np.asarray(self.oi[sym], dtype=float)
        fr = np.full(len(px), self.fr.get(sym, float("nan")))
        return px, oi, fr

    def bench_momentum(self, window_min: int) -> float | None:
        """基準(BTC) 過去 window 分鐘報酬; 歷史不足回傳 None。"""
        import math
        if len(self.bench_px) <= window_min:
            return None
        now, ago = self.bench_px[-1], self.bench_px[-1 - window_min]
        if math.isnan(now) or math.isnan(ago) or ago <= 0:
            return None
        return now / ago - 1

    def price(self, sym: str) -> float | None:
        if self.px[sym] and not np.isnan(self.px[sym][-1]):
            return float(self.px[sym][-1])
        return None
