"""重放引擎：把歷史分鐘資料餵進與實時模式相同的策略/帳本/推播程式碼路徑。

輸入:
- minute_last.csv: sym,minute,field,value（Binance 山寨永續 mark_price / open_interest）
- funding_minute.csv: sym,minute,fr
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .config import EXCLUDED_TOKENS, Settings
from .ledger import Ledger
from .notifier import Notifier
from .strategies import StrategyA, StrategyB, StrategyC

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_universe() -> set[str]:
    p = DATA_DIR / "bitget_symbols.json"
    if not p.exists():
        print("警告: 無 bitget_symbols.json，跳過上架過濾", file=sys.stderr)
        return set()
    return set(json.loads(p.read_text()))


def load_series(minute_csv: str, funding_csv: str, bitget: set[str]):
    df = pd.read_csv(minute_csv, header=None, names=["sym", "minute", "field", "val"])
    df = df[~df["sym"].isin(EXCLUDED_TOKENS) & ~df["sym"].str.endswith("USDC")]
    if bitget:
        df = df[df["sym"].isin(bitget)]
    piv = df.pivot_table(index=["sym", "minute"], columns="field", values="val", aggfunc="last")
    del df
    fr_df = pd.read_csv(funding_csv, header=None, names=["sym", "minute", "fr"])

    series = {}
    fr_g = dict(list(fr_df.groupby("sym")))
    for sym, g in piv.groupby(level=0):
        g = g.droplevel(0)
        full = pd.RangeIndex(g.index.min(), g.index.max() + 1)
        px = g["mark_price"].reindex(full).ffill(limit=10)
        oi = g["open_interest"].reindex(full).ffill(limit=15)
        if sym in fr_g:
            fr = fr_g[sym].set_index("minute")["fr"].sort_index().reindex(full).ffill(limit=120)
        else:
            fr = pd.Series(np.nan, index=full)
        series[sym] = (int(full[0]), px.to_numpy(), oi.to_numpy(), fr.to_numpy())
    return series


def run(minute_csv: str, funding_csv: str, out_csv: str = "") -> Ledger:
    cfg = Settings()
    bitget = load_universe()
    series = load_series(minute_csv, funding_csv, bitget)
    print(f"幣數(過 Bitget 上架+排除名單): {len(series)}", file=sys.stderr)

    strat_a, strat_b = StrategyA(cfg.a), StrategyB(cfg.b)
    strat_c = StrategyC(cfg.c) if cfg.c.enabled else None
    strategies = [(strat_a, cfg.a.cooldown_min), (strat_b, cfg.b.cooldown_min)] + (
        [(strat_c, cfg.c.cooldown_min)] if strat_c else [])

    # 向量化預篩出候選分鐘，再逐事件走與實時相同的 check() 路徑
    events: list[tuple[int, str, object, int]] = []  # (minute_abs, sym, strategy, cooldown)
    b_mask_by_sym = {}
    for sym, (t0, px, oi, fr) in series.items():
        for strat, cd in strategies:
            mask = strat.trigger_mask(px, oi, fr)
            if strat.name == "F":
                b_mask_by_sym[sym] = mask
            if strat.name == "E":
                # A 的冷卻掛在「任何 OI 警報」上（同 TripleMonitor 行為）:
                # 30 分內出過警報（含 OI增）的再觸發一律略過
                alerts = strat.alert_mask(px, oi)
                cd_a = strat.cfg.cooldown_min
                last = -10**9
                for i in np.flatnonzero(alerts):
                    if i - last < cd_a:
                        continue
                    last = int(i)
                    if mask[i]:
                        events.append((t0 + int(i), sym, strat, cd))
                continue
            for i in np.flatnonzero(mask):
                events.append((t0 + int(i), sym, strat, cd))
    events.sort(key=lambda e: e[0])
    print(f"候選事件: {len(events)}", file=sys.stderr)

    ledger = Ledger(cfg.risk)
    notifier = Notifier(cfg.tg, dry_run=True)
    day_pnl: dict = {}

    t_min = min(e[0] for e in events)
    t_max = max(t0 + len(px) - 1 for t0, px, _, _ in
                ((t0, px, oi, fr) for (t0, px, oi, fr) in series.values()))
    ev_idx = 0
    for t in range(t_min, t_max + 1):
        # 1) 先結算既有倉位（停損/到期）
        if ledger.open_positions:
            prices = {}
            for pos in ledger.open_positions:
                t0, px, _, _ = series[pos.signal.symbol]
                j = t - t0
                if 0 <= j < len(px) and not np.isnan(px[j]):
                    prices[pos.signal.symbol] = float(px[j])
            for pos in ledger.mark(t, prices):
                day = pos.exit_minute * 60 // 86400
                day_pnl[day] = day_pnl.get(day, 0.0) + pos.pnl_usdt
                notifier.close(pos, day_pnl[day])
        # 2) 再處理本分鐘的候選訊號
        while ev_idx < len(events) and events[ev_idx][0] == t:
            _, sym, strat, cd = events[ev_idx]
            ev_idx += 1
            t0, px, oi, fr = series[sym]
            i = t - t0
            sig = strat.check(sym, px, oi, fr, i)
            if sig is None:
                continue
            sig = type(sig)(sig.strategy, sym, sig.side, t, sig.price, sig.hold_min, sig.note)
            frv = fr[i] if not np.isnan(fr[i]) else 0.0
            pos = ledger.try_open(sig, cd, fr=float(frv))
            if pos is not None:
                flagship = (sig.strategy == "E" and bool(b_mask_by_sym.get(sym, np.zeros(1))[i])
                            if i < len(b_mask_by_sym.get(sym, [])) else False)
                notifier.signal(sig, flagship=flagship)

    if out_csv:
        rows = [{
            "strategy": p.signal.strategy, "sym": p.signal.symbol, "side": p.signal.side,
            "entry_minute": p.entry_minute, "exit_minute": p.exit_minute,
            "entry": p.entry_price, "exit": p.exit_price, "reason": p.exit_reason,
            "pnl_bps": p.pnl_bps, "pnl_usdt": p.pnl_usdt, "note": p.signal.note,
        } for p in ledger.closed]
        pd.DataFrame(rows).to_csv(out_csv, index=False)

    return ledger, notifier
