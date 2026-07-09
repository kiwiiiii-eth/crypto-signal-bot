"""OKX 活期借貸利率追蹤（第二組策略的利率佐證層）。

- 每分鐘 1 個公開請求（lending-rate-summary 一次回傳全幣即時預估利率）。
- 72h 小時級基線在背景執行緒啟動時回填（lending-rate-history, 公開分頁），
  之後由分鐘級觀測自行滾動維護 → z-score 不需要 InfluxDB。
- 偵測「脫離 1% 地板」事件（prev<=1.2% → >2%）: H 的加分項, 稀有事件推 TG。
研究依據: vault "Lending Rate Strategy Candidates 2026-07-10"。
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

SUMMARY_URL = "https://www.okx.com/api/v5/finance/savings/lending-rate-summary"
HISTORY_URL = "https://www.okx.com/api/v5/finance/savings/lending-rate-history"
FLOOR_RATE = 0.012   # 1% 地板容忍帶
LIFTOFF_RATE = 0.02  # 脫離地板認定
BASELINE_HOURS = 72


def _get(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class LendingTracker:
    def __init__(self, notify=None):
        self.rate: dict[str, float] = {}                 # ccy -> 最新預估年化
        self.prev: dict[str, float] = {}
        self.hourly: dict[str, deque] = {}               # ccy -> 72h 小時均值
        self.liftoff_at: dict[str, int] = {}             # ccy -> 脫離地板的分鐘
        self._hour_acc: dict[str, list[float]] = {}
        self._cur_hour: int | None = None
        self._notify = notify
        self._baseline_ready = False

    # ---------- 啟動回填 ----------

    def bootstrap_async(self, ccys: set[str]) -> None:
        threading.Thread(target=self._bootstrap, args=(ccys,), daemon=True,
                         name="lend-bootstrap").start()

    def _bootstrap(self, ccys: set[str]) -> None:
        start_ms = int((time.time() - BASELINE_HOURS * 3600) * 1000)
        done = 0
        for ccy in sorted(ccys):
            try:
                rows: list[tuple[int, float]] = []
                after = ""
                for _ in range(3):  # 72h / 100筆頁 = 最多 ~1 頁餘裕
                    q = urllib.parse.urlencode(
                        {"ccy": ccy, "limit": 100, **({"after": after} if after else {})})
                    r = _get(f"{HISTORY_URL}?{q}")
                    if r.get("code") != "0" or not r["data"]:
                        break
                    batch = [(int(x["ts"]), float(x["rate"])) for x in r["data"]]
                    rows.extend(batch)
                    oldest = min(b[0] for b in batch)
                    if oldest <= start_ms or len(batch) < 100:
                        break
                    after = str(oldest)
                    time.sleep(0.15)
                vals = [rate for ts, rate in rows if ts >= start_ms]
                if len(vals) >= 12:
                    self.hourly[ccy] = deque(vals[-BASELINE_HOURS:], maxlen=BASELINE_HOURS)
                    done += 1
                time.sleep(0.15)
            except Exception:
                continue
        self._baseline_ready = True
        print(f"lending 基線回填完成: {done}/{len(ccys)} ccy", file=sys.stderr)

    # ---------- 每分鐘更新 ----------

    def tick(self, minute: int) -> None:
        try:
            r = _get(SUMMARY_URL)
        except Exception as e:
            print(f"lending summary 失敗: {e}", file=sys.stderr)
            return
        if r.get("code") != "0":
            return
        hour = minute // 60
        if self._cur_hour is None:
            self._cur_hour = hour
        elif hour != self._cur_hour:  # 換小時: 上一小時均值滾入基線
            for ccy, acc in self._hour_acc.items():
                if acc:
                    self.hourly.setdefault(ccy, deque(maxlen=BASELINE_HOURS)).append(
                        statistics.fmean(acc))
            self._hour_acc.clear()
            self._cur_hour = hour
        for row in r.get("data", []):
            ccy = row.get("ccy")
            try:
                rate = float(row["estRate"])
            except (KeyError, TypeError, ValueError):
                continue
            prev = self.rate.get(ccy)
            if prev is not None:
                self.prev[ccy] = prev
            self.rate[ccy] = rate
            self._hour_acc.setdefault(ccy, []).append(rate)
            # 脫離地板偵測（稀有事件, 10天約7次, 推 TG 資訊訊息）
            if (prev is not None and prev <= FLOOR_RATE and rate > LIFTOFF_RATE
                    and minute - self.liftoff_at.get(ccy, -10**9) > 1440):
                self.liftoff_at[ccy] = minute
                if self._notify:
                    self._notify(f"💡 利率脫離地板 `{ccy}` "
                                 f"{prev*100:.1f}% → {rate*100:.1f}%（H 策略加分中）")

    # ---------- 查詢介面 ----------

    def z(self, ccy: str) -> float | None:
        vals = self.hourly.get(ccy)
        rate = self.rate.get(ccy)
        if not vals or len(vals) < 12 or rate is None:
            return None
        std = statistics.pstdev(vals)
        if std == 0:
            return None
        return (rate - statistics.fmean(vals)) / std

    def score(self, ccy: str, minute: int) -> tuple[int, str]:
        """H 的利率加分: 脫離地板(24h內)+2, z>2 +1。回傳 (分數, 說明)。"""
        pts, why = 0, []
        if minute - self.liftoff_at.get(ccy, -10**9) <= 1440:
            pts += 2
            why.append("脫離地板✓")
        zv = self.z(ccy)
        if zv is not None and zv > 2:
            pts += 1
            why.append(f"z={zv:.1f}")
        rate = self.rate.get(ccy)
        desc = f"{rate*100:.1f}%" if rate is not None else "無資料"
        if why:
            desc += " " + " ".join(why)
        return pts, desc
