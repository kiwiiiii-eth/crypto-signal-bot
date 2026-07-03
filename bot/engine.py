"""實時 Demo 引擎：真實行情 + 模擬下單/停損/到時平倉，與重放共用策略與帳本。"""
from __future__ import annotations

import csv
import json
import queue
import sys
import threading
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
        self.executor = None
        self._execq: queue.Queue = queue.Queue()  # 執行緒 → 主迴圈 的成交回報
        self.hl = None
        if self.cfg.exec_.mode in {"demo", "live"}:
            from .executor import BitgetExecutor
            self.executor = BitgetExecutor(self.cfg.exec_, self.cfg.risk)
            self.executor.setup_account()
            print(f"Bitget 執行器啟用: mode={self.cfg.exec_.mode} "
                  f"權益={self.executor.equity():.2f}U", file=sys.stderr)
            self._reconcile_startup()
        self.strat_a, self.strat_b = StrategyA(self.cfg.a), StrategyB(self.cfg.b)
        self.strat_c = StrategyC(self.cfg.c) if self.cfg.c.enabled else None
        self.strat_d = StrategyD(self.cfg.d) if self.cfg.d.enabled else None
        self.last_alert: dict[str, int] = {}  # A 的警報級冷卻（任何 OI 警報）
        self.day_pnl: dict = {}

    def _save(self):
        STATE_FILE.write_text(json.dumps(self.ledger.to_dict()))

    def _reconcile_startup(self):
        """重啟後與交易所對帳: 停機期間被停損的倉補結算; 非帳本倉位告警(不動它)。"""
        try:
            exch = self.executor.positions()
        except Exception as e:
            print(f"啟動對帳失敗: {e}", file=sys.stderr)
            return
        for pos in list(self.ledger.open_positions):
            if not exch.get(self.executor.map_symbol(pos.signal.symbol)):
                self.ledger.open_positions.remove(pos)
                pos.exit_minute = int(time.time() // 60)
                pos.exit_price = pos.entry_price  # 佔位, 下行用交易所結算回填
                pos.exit_reason = "stop"
                self.ledger.closed.append(pos)
                self._reconcile_close(pos)
                pnl = f"{pos.pnl_usdt:+.2f}U" if pos.pnl_usdt is not None else "對帳失敗"
                self.notifier.send(f"⚠️ 停機期間 `{pos.signal.symbol}` 已被交易所平掉, "
                                   f"補結算: {pnl}")
        ledger_syms = {self.executor.map_symbol(p.signal.symbol)
                       for p in self.ledger.open_positions}
        extra = [s for s in exch if s not in ledger_syms]
        if extra:
            self.notifier.send(f"ℹ️ 交易所有非帳本倉位(bot 不會動它): `{'`, `'.join(extra)}`")

    def _book_close(self, pos):
        day = datetime.fromtimestamp(pos.exit_minute * 60, tz=TPE).date().isoformat()
        self.day_pnl[day] = self.day_pnl.get(day, 0.0) + (pos.pnl_usdt or 0)
        self.notifier.close(pos, self.day_pnl[day])

    def _process_closes(self, minute: int):
        prices = {}
        for pos in self.ledger.open_positions:
            px = self.feed.price(pos.signal.symbol)
            if px is not None:
                prices[pos.signal.symbol] = px
        for pos in self.ledger.mark(minute, prices):
            if self.executor:
                # 平倉走執行緒: maker 追掛最長數分鐘, 不能卡住其他幣的停損/出場檢查
                def job(p=pos):
                    try:
                        self.executor.close(p)
                        self._reconcile_close(p)
                        self._execq.put(("close_done", p, None))
                    except Exception as ex:
                        self._execq.put(("close_fail", p, ex))
                threading.Thread(target=job, daemon=True, name=f"close-{pos.signal.symbol}").start()
            else:
                self._book_close(pos)

    def _log_slippage(self, pos):
        """簽核滑價: Binance 訊號價 vs Bitget 實際成交價（正值=不利）。"""
        f = DATA_DIR / "slippage.csv"
        new = not f.exists()
        sig_px = pos.signal.price
        slip = (pos.entry_price / sig_px - 1) * 10000 * pos.signal.side
        with f.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["ts", "symbol", "strategy", "side",
                            "signal_px", "fill_px", "slip_bps", "fee_usdt"])
            w.writerow([int(time.time()), pos.signal.symbol, pos.signal.strategy,
                        pos.signal.side, sig_px, pos.entry_price,
                        round(slip, 2), pos.fee_usdt])

    def _drain_exec(self):
        """主迴圈開頭消化執行緒回報, 帳本只在主執行緒改。"""
        while True:
            try:
                kind, pos, extra = self._execq.get_nowait()
            except queue.Empty:
                return
            if kind == "open_ok":
                res, flagship = extra
                pos.order_id = res["orderId"]
                if res["size"] > 0:  # 真實成交均價/名目/開倉手續費(可能部分成交)
                    pos.entry_price = res["price"]
                    pos.notional_usdt = res["price"] * res["size"]
                pos.fee_usdt = res["fee"]
                self._log_slippage(pos)
                self.notifier.signal(pos.signal, flagship=flagship,
                                     extra=self._hl_note(pos.signal.symbol))
            elif kind == "open_fail":
                if pos in self.ledger.open_positions:
                    self.ledger.open_positions.remove(pos)
                self.notifier.send(f"⚠️ 實盤開倉失敗 `{pos.signal.symbol}`: {extra}")
            elif kind == "close_done":
                self._book_close(pos)
            elif kind == "close_fail":
                self.notifier.send(f"🚨 實盤平倉失敗 `{pos.signal.symbol}`: {extra}\n"
                                   f"請手動檢查交易所倉位")
                self._book_close(pos)

    def _hl_note(self, sym: str) -> str:
        """HL 聰明錢在該幣的即時淨倉向, 附註在開倉訊息（純情報, 不影響下單）。"""
        if not self.hl or not self.hl.prev:
            return ""
        coin = sym[:-4] if sym.endswith("USDT") else sym
        net, longs, shorts = 0.0, 0, 0
        for poss in self.hl.prev.values():
            n = poss.get(coin)
            if n:
                net += n
                longs, shorts = longs + (n > 0), shorts + (n < 0)
        if not (longs or shorts):
            return ""
        return (f"🐳 HL大戶: {longs}多/{shorts}空 "
                f"淨{'多' if net > 0 else '空'} `{abs(net) / 1e6:.2f}M`")

    def _reconcile_close(self, pos):
        """平倉後用 Bitget 結算數字覆蓋帳本（真實出場價/手續費/funding/淨損益）。"""
        for _ in range(5):
            time.sleep(1)
            try:
                r = self.executor.last_closed(pos.signal.symbol)
            except Exception:
                continue
            # utime 需晚於進場, 才確定是這一筆而非更早的歷史倉位
            if r and r["utime"] // 60000 >= pos.entry_minute:
                if r["close_price"] > 0:
                    pos.exit_price = r["close_price"]
                if r["open_price"] > 0:
                    pos.entry_price = r["open_price"]
                pos.fee_usdt = r["fee"]
                pos.funding_usdt = r["funding"]
                pos.pnl_usdt = r["net"]
                if pos.notional_usdt:
                    pos.pnl_bps = r["net"] / pos.notional_usdt * 10000
                pos.pnl_source = "exchange"
                return
        print(f"對帳失敗(沿用模擬值): {pos.signal.symbol}", file=sys.stderr)

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
        # Bitget funding 濾網: funding 實收在 Bitget, Binance 費率只是訊號
        if self.executor:
            try:
                bfr = self.executor.funding_rate(sym)
            except Exception:
                bfr = None
            if bfr is not None:
                if sig.strategy == "F" and bfr <= 0:
                    print(f"F 棄單 {sym}: Bitget fr={bfr:+.4%} 收不到 carry", file=sys.stderr)
                    return
                # E/G: 持有期跨結算, funding 嚴重倒貼(>=0.1%/期)就不開
                if sig.strategy in {"E", "G"} and bfr * sig.side >= self.cfg.b.fr_threshold:
                    print(f"{sig.strategy} 棄單 {sym}: Bitget fr={bfr:+.4%} 倒貼", file=sys.stderr)
                    return
        sig = type(sig)(sig.strategy, sym, sig.side, minute, sig.price, sig.hold_min, sig.note)
        frv = float(fr[i]) if not np.isnan(fr[i]) else 0.0
        pos = self.ledger.try_open(sig, cooldown, fr=frv, size_mult=size_mult)
        if pos is not None:
            flagship = (sig.strategy == "E" and frv >= self.cfg.b.fr_threshold) or \
                       (sig.strategy == "G" and frv <= -self.cfg.b.fr_threshold)
            if self.executor:
                # 開倉走執行緒(maker 追掛最長 ~90s); 成交/失敗經 _execq 回主迴圈記帳
                def job(p=pos, fl=flagship):
                    try:
                        res = self.executor.open(p)
                        self._execq.put(("open_ok", p, (res, fl)))
                    except Exception as ex:
                        self._execq.put(("open_fail", p, ex))
                threading.Thread(target=job, daemon=True, name=f"open-{sym}").start()
            else:
                self.notifier.signal(sig, flagship=flagship,
                                     extra=self._hl_note(sym))

    def run(self):
        print(f"啟動({self.cfg.exec_.mode}): {len(self.feed.universe)} 幣 "
              f"單筆 {self.cfg.risk.margin_usdt:.0f}U×{self.cfg.risk.leverage:.0f}x "
              f"停損 -{self.cfg.risk.disaster_stop_bps/100:.0f}%", file=sys.stderr)
        if self.cfg.tg.token:
            from .commands import CommandServer
            CommandServer(self.cfg.tg.token, self.cfg.tg.chat_id, self).start()
            mode_txt = {"live": "🔴 實盤（Bitget 真單）", "demo": "🟡 Bitget 模擬盤",
                        "paper": "🟢 純模擬"}.get(self.cfg.exec_.mode, self.cfg.exec_.mode)
            self.notifier.send(f"🤖 啟動｜{mode_txt}\n/help 看指令")
        import os
        if os.getenv("HL_WATCH_ENABLED", "true").lower() in {"1", "true", "yes"}:
            from .hl_watch import HLWatcher
            self.hl = HLWatcher(self.notifier,
                                poll_min=int(os.getenv("HL_POLL_MIN", "5")),
                                top_n=int(os.getenv("HL_TOP_N", "60")))
            self.hl.start()
        self._report_day = datetime.now(TPE).date().isoformat()
        while True:
            t0 = time.time()
            minute = int(t0 // 60)
            try:
                self._drain_exec()
                self.feed.tick(minute)
                self._process_closes(minute)
                self._process_signals(minute)
                self._save()
                self._maybe_daily_report()
            except Exception as e:
                print(f"tick 錯誤: {e}", file=sys.stderr)
            time.sleep(max(5, 60 - (time.time() - t0)))

    def _maybe_daily_report(self):
        """跨日後第一個 tick 推送前一日結算報告。"""
        today = datetime.now(TPE).date().isoformat()
        if today == self._report_day:
            return
        day, self._report_day = self._report_day, today
        closed = [p for p in self.ledger.closed if p.exit_minute and
                  datetime.fromtimestamp(p.exit_minute * 60, tz=TPE).date().isoformat() == day]
        pnl = sum(p.pnl_usdt or 0 for p in closed)
        wins = sum(1 for p in closed if (p.pnl_bps or 0) > 0)
        fees = sum(p.fee_usdt or 0 for p in closed)
        fund = sum(p.funding_usdt or 0 for p in closed)
        msg = (f"📅 *{day} 日報*\n\n"
               f"平倉: `{len(closed)}` 單（勝 {wins}）\n"
               f"淨損益: `{pnl:+.2f} USDT`\n"
               f"手續費: `-{fees:.4f}`｜funding: `{fund:+.4f}`\n"
               f"目前持倉: `{len(self.ledger.open_positions)}`")
        if self.executor:
            try:
                msg += f"\n交易所權益: `{self.executor.equity():.2f} USDT`"
            except Exception:
                pass
        self.notifier.send(msg, self.cfg.tg.thread_daily)
