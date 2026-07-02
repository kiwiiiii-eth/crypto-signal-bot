"""實時 Demo 引擎：真實行情 + 模擬下單/停損/到時平倉，與重放共用策略與帳本。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

from .config import EXCLUDED_TOKENS, Settings
from .feed import BinanceFeed, binance_usdt_perps
from .ledger import Ledger
from .notifier import Notifier
from .strategies import StrategyA, StrategyB, StrategyC, StrategyD

TPE = timezone(timedelta(hours=8))
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "state.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class LiveEngine:
    def __init__(self):
        self.cfg = Settings()
        bitget = set(json.loads((DATA_DIR / "bitget_symbols.json").read_text())) \
            if (DATA_DIR / "bitget_symbols.json").exists() else set()
        universe = binance_usdt_perps() - EXCLUDED_TOKENS
        universe = {s for s in universe if not s.endswith("USDC")}
        if bitget:
            universe &= bitget
        self.feed = BinanceFeed(universe)
        self.ledger = Ledger(self.cfg.risk)
        if STATE_FILE.exists():
            self.ledger.restore(json.loads(STATE_FILE.read_text()))
            print(f"還原狀態: {len(self.ledger.open_positions)} 開倉 "
                  f"{len(self.ledger.closed)} 歷史", file=sys.stderr)
        self.notifier = Notifier(self.cfg.tg, dry_run=not self.cfg.tg.token)
        self.strat_a, self.strat_b = StrategyA(self.cfg.a), StrategyB(self.cfg.b)
        self.strat_c = StrategyC(self.cfg.c) if self.cfg.c.enabled else None
        self.strat_d = StrategyD(self.cfg.d) if self.cfg.d.enabled else None
        self.last_alert: dict[str, int] = {}  # A 的警報級冷卻（任何 OI 警報）
        self.day_pnl: dict = {}

    def _save(self):
        STATE_FILE.write_text(json.dumps(self.ledger.to_dict()))

    def _process_closes(self, minute: int):
        prices = {}
        for pos in self.ledger.open_positions:
            px = self.feed.price(pos.signal.symbol)
            if px is not None:
                prices[pos.signal.symbol] = px
        for pos in self.ledger.mark(minute, prices):
            day = datetime.fromtimestamp(pos.exit_minute * 60, tz=TPE).date().isoformat()
            self.day_pnl[day] = self.day_pnl.get(day, 0.0) + (pos.pnl_usdt or 0)
            self.notifier.close(pos, self.day_pnl[day])

    def _process_signals(self, minute: int):
        w = self.cfg.a.window_min
        self._btc_mom = self.feed.bench_momentum(self.cfg.regime.btc_window_min)
        # Bybit 否決濾網每 5 分鐘刷新一次即可（funding 變動慢）
        if minute - getattr(self, "_bybit_fr_at", -10) >= 5:
            fr_map = self.feed.fetch_bybit_funding()
            if fr_map:
                self._bybit_fr = fr_map
                self._bybit_fr_at = minute
        for sym in self.feed.universe:
            px, oi, fr = self.feed.arrays(sym)
            i = len(px) - 1
            if i < w:
                continue
            # A: 警報級冷卻（任何方向 |ΔOI|>=閾值 都重置）
            if not np.isnan(oi[i]) and not np.isnan(oi[i - w]) and oi[i - w] > 0:
                d_oi = abs(oi[i] / oi[i - w] - 1) * 100
                if d_oi >= abs(self.cfg.a.oi_drop_pct):
                    in_cd = minute - self.last_alert.get(sym, -10**9) < self.cfg.a.cooldown_min
                    if not in_cd:
                        self.last_alert[sym] = minute
                        sig = self.strat_a.check(sym, px, oi, fr, i)
                        if sig is not None:
                            self._try_open(sig, minute, sym, px, oi, fr, i,
                                           self.cfg.a.cooldown_min)
                        elif self.strat_d:  # A/D 條件互斥, 共用警報級冷卻
                            sig = self.strat_d.check(sym, px, oi, fr, i)
                            if sig is not None:
                                self._try_open(sig, minute, sym, px, oi, fr, i,
                                               self.cfg.d.cooldown_min)
            # B
            sig = self.strat_b.check(sym, px, oi, fr, i)
            if sig is not None:
                self._try_open(sig, minute, sym, px, oi, fr, i, self.cfg.b.cooldown_min)
            # C（預設關）
            if self.strat_c:
                sig = self.strat_c.check(sym, px, oi, fr, i)
                if sig is not None:
                    self._try_open(sig, minute, sym, px, oi, fr, i, self.cfg.c.cooldown_min)

    def _try_open(self, sig, minute: int, sym: str, px, oi, fr, i: int, cooldown: int):
        mom = getattr(self, "_btc_mom", None)
        oi_usd = float(oi[i] * px[i]) if not (np.isnan(oi[i]) or np.isnan(px[i])) else None
        size_mult = 1.0
        if sig.strategy == "E" and mom is not None and mom > 0:
            size_mult = self.cfg.regime.a_upsize_mult  # 逼空環境半倉
        if sig.strategy == "F":
            if oi_usd is None or oi_usd < self.cfg.regime.b_min_oi_usd:
                return  # B 只做大幣（流動性下限）
            # Bybit 否決: 兩所同時極端=知情擁擠會續漲 (fade -45bps); 僅 Binance 極端才 fade (+99bps)
            by_fr = getattr(self, "_bybit_fr", {}).get(sym)
            if by_fr is not None and by_fr >= self.cfg.b.fr_threshold:
                return
        if sig.strategy == "G":
            if self.cfg.d.require_btc_down and (mom is None or mom > 0):
                return  # D 只在 BTC 4h 跌勢接反彈
            if oi_usd is None or oi_usd < self.cfg.d.min_oi_usd:
                return
        sig = type(sig)(sig.strategy, sym, sig.side, minute, sig.price, sig.hold_min, sig.note)
        frv = float(fr[i]) if not np.isnan(fr[i]) else 0.0
        pos = self.ledger.try_open(sig, cooldown, fr=frv, size_mult=size_mult)
        if pos is not None:
            flagship = (sig.strategy == "E" and frv >= self.cfg.b.fr_threshold) or \
                       (sig.strategy == "G" and frv <= -self.cfg.b.fr_threshold)
            self.notifier.signal(sig, flagship=flagship)

    def run(self):
        print(f"Demo 模擬盤啟動: {len(self.feed.universe)} 幣 "
              f"單筆 {self.cfg.risk.margin_usdt:.0f}U×{self.cfg.risk.leverage:.0f}x "
              f"停損 -{self.cfg.risk.disaster_stop_bps/100:.0f}%", file=sys.stderr)
        if self.cfg.tg.token:
            from .commands import CommandServer
            CommandServer(self.cfg.tg.token, self.cfg.tg.chat_id, self).start()
            self.notifier.send("🤖 Demo 模擬盤啟動（真實行情、模擬下單）\n/help 看指令")
        import os
        if os.getenv("HL_WATCH_ENABLED", "true").lower() in {"1", "true", "yes"}:
            from .hl_watch import HLWatcher
            HLWatcher(self.notifier,
                      poll_min=int(os.getenv("HL_POLL_MIN", "5")),
                      top_n=int(os.getenv("HL_TOP_N", "60"))).start()
        while True:
            t0 = time.time()
            minute = int(t0 // 60)
            try:
                self.feed.tick(minute)
                self._process_closes(minute)
                self._process_signals(minute)
                self._save()
            except Exception as e:
                print(f"tick 錯誤: {e}", file=sys.stderr)
            time.sleep(max(5, 60 - (time.time() - t0)))
