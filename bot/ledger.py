"""Paper trading 帳本：倉位、固定時間出場、災難停損、費用與 funding carry。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import FEE_BPS, RiskCfg, strategy_group
from .strategies import Signal


@dataclass
class Position:
    signal: Signal
    entry_minute: int
    entry_price: float
    notional_usdt: float
    exit_due: int                 # entry + hold
    fr_at_entry: float = 0.0
    # 每倉獨立保證金/槓桿（G2 浮動: 名目固定, 池子越滿保證金越小槓桿越高）
    margin_usdt: float = 0.0
    leverage: float = 0.0
    # 出場後填入
    exit_minute: int | None = None
    exit_price: float | None = None
    exit_reason: str = ""         # "time" / "stop"
    pnl_bps: float | None = None
    pnl_usdt: float | None = None
    # 實盤對帳（交易所真實數字, pnl_source="exchange" 時 pnl_usdt 為 Bitget 結算淨利）
    # 持有期間最大浮盈/浮虧（bps, 分鐘級標記; 供停損/停利參數優化）
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
    order_id: str = ""
    fee_usdt: float | None = None
    funding_usdt: float | None = None
    pnl_source: str = "model"


@dataclass
class Ledger:
    risk: RiskCfg
    open_positions: list[Position] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    cooldown_until: dict[tuple[str, str], int] = field(default_factory=dict)  # (strategy,sym) -> minute
    rejected: dict[str, int] = field(default_factory=dict)

    def _reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1

    def try_open(self, sig: Signal, cooldown_min: int, fr: float = 0.0,
                 size_mult: float = 1.0) -> Position | None:
        key = (sig.strategy, sig.symbol)
        if sig.minute < self.cooldown_until.get(key, -1):
            self._reject("冷卻中")
            return None
        grp = strategy_group(sig.strategy)
        grp_open = [p for p in self.open_positions
                    if strategy_group(p.signal.strategy) == grp]
        if grp == "G2":
            # 第二組: 組內共用 25 倉上限（H/L 開倉條件不同, 不設每策略上限）
            if len(grp_open) >= self.risk.g2_max_positions:
                self._reject("倉位滿(G2)")
                return None
        else:
            # 上限每策略獨立: B 持倉 4h 會長期佔位，不能擠掉 A 的密集訊號
            #（重放驗證被擠掉的單均賺 +214bps）
            if sum(1 for p in self.open_positions
                   if p.signal.strategy == sig.strategy) >= self.risk.max_positions:
                self._reject("倉位滿")
                return None
        if any(p.signal.symbol == sig.symbol for p in self.open_positions):
            self._reject("同幣持倉中")
            return None
        # 保證金上限按組獨立計算（G1=原有策略, G2=利率共鳴策略, 各 1 份組權益）
        used = sum(p.margin_usdt for p in grp_open)
        remaining = self.risk.margin_cap_usdt - used
        base_margin = self.risk.margin_usdt * size_mult
        notional = self.risk.margin_usdt * self.risk.leverage * size_mult  # 名目固定
        if grp == "G2":
            # 浮動保證金: 剩餘池子均攤到剩餘槽位(名目固定 → 槓桿反向浮動),
            # 25 槽滿載約 36U/倉 ≈ 13.9x; 槓桿上限 20x → 保證金下限 25U, 低於即拒單
            slots_left = max(1, self.risk.g2_max_positions - len(grp_open))
            margin = min(base_margin, remaining / slots_left)
            min_margin = notional / self.risk.g2_max_leverage
            if margin < min_margin:
                self._reject(f"保證金上限({grp})")
                return None
        else:
            margin = base_margin
            if margin > remaining:
                self._reject(f"保證金上限({grp})")
                return None
        leverage = notional / margin
        self.cooldown_until[key] = sig.minute + cooldown_min
        pos = Position(
            signal=sig,
            entry_minute=sig.minute,
            entry_price=sig.price,
            notional_usdt=notional,
            exit_due=sig.minute + sig.hold_min,
            fr_at_entry=fr,
            margin_usdt=margin,
            leverage=leverage,
        )
        self.open_positions.append(pos)
        return pos

    def mark(self, minute: int, prices: dict[str, float]) -> list[Position]:
        """每分鐘呼叫：檢查災難停損與到期出場，回傳本次平掉的倉位。"""
        done: list[Position] = []
        for pos in list(self.open_positions):
            px = prices.get(pos.signal.symbol)
            if px is None:
                continue
            side = pos.signal.side
            run_bps = (px / pos.entry_price - 1) * 10000 * side
            pos.mfe_bps = max(pos.mfe_bps, run_bps)
            pos.mae_bps = min(pos.mae_bps, run_bps)
            adverse_bps = -run_bps
            # 強平檢查優先: 強平距離 = 保證金率 - 維持保證金率（逐倉, 交易所口徑）。
            # 高槓桿(G2 浮動後)強平價可能比 -8% 停損更近, 此時遵守交易所價格被強平。
            liq_move_bps = self._liq_move_bps(pos)
            if adverse_bps >= liq_move_bps:
                liq_px = pos.entry_price * (1 - side * liq_move_bps / 10000)
                self._close(pos, minute, liq_px, "liq")
                done.append(pos)
            elif adverse_bps >= self.risk.disaster_stop_bps:
                self._close(pos, minute, px, "stop")
                done.append(pos)
            elif minute >= pos.exit_due:
                self._close(pos, minute, px, "time")
                done.append(pos)
        return done

    def _liq_move_bps(self, pos: Position) -> float:
        """強平所需價格反向變動(bps): 1/槓桿 - 維持保證金率。"""
        lev = pos.leverage or self.risk.leverage
        return (1 / lev - self.risk.maint_margin_rate) * 10000

    def _close(self, pos: Position, minute: int, px: float, reason: str) -> None:
        side = pos.signal.side
        gross = (px / pos.entry_price - 1) * 10000 * side
        held = minute - pos.entry_minute
        carry = -pos.fr_at_entry * 10000 * side * (held / 480)  # 每 8h 結算近似
        pos.exit_minute = minute
        pos.exit_price = px
        pos.exit_reason = reason
        pos.pnl_bps = gross + carry - FEE_BPS
        # 逐倉強平: 虧損上限 = 該倉保證金歸零（以實際槓桿計; 跳空穿越時也不會虧超過保證金）
        lev = pos.leverage or self.risk.leverage
        liq_bps = -10000 / lev
        if reason == "liq" or pos.pnl_bps < liq_bps:
            pos.pnl_bps = liq_bps
            pos.exit_reason = "liq"
        pos.pnl_usdt = pos.notional_usdt * pos.pnl_bps / 10000
        self.open_positions.remove(pos)
        self.closed.append(pos)

    @property
    def total_pnl_usdt(self) -> float:
        return sum(p.pnl_usdt or 0 for p in self.closed)

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for pos in self.open_positions:
            px = prices.get(pos.signal.symbol)
            if px is None:
                continue
            total += pos.notional_usdt * (px / pos.entry_price - 1) * pos.signal.side
        return total

    def to_dict(self) -> dict:
        def _pos(p: Position) -> dict:
            return {
                "strategy": p.signal.strategy, "symbol": p.signal.symbol,
                "side": p.signal.side, "note": p.signal.note,
                "hold_min": p.signal.hold_min,
                "entry_minute": p.entry_minute, "entry_price": p.entry_price,
                "notional_usdt": p.notional_usdt, "exit_due": p.exit_due,
                "fr_at_entry": p.fr_at_entry, "exit_minute": p.exit_minute,
                "margin_usdt": p.margin_usdt, "leverage": p.leverage,
                "exit_price": p.exit_price, "exit_reason": p.exit_reason,
                "pnl_bps": p.pnl_bps, "pnl_usdt": p.pnl_usdt,
                "mfe_bps": p.mfe_bps, "mae_bps": p.mae_bps,
                "order_id": p.order_id, "fee_usdt": p.fee_usdt,
                "funding_usdt": p.funding_usdt, "pnl_source": p.pnl_source,
            }
        return {"open": [_pos(p) for p in self.open_positions],
                "closed": [_pos(p) for p in self.closed],
                "cooldown": {f"{k[0]}|{k[1]}": v for k, v in self.cooldown_until.items()}}

    def restore(self, d: dict) -> None:
        from .strategies import Signal

        def _pos(r: dict) -> Position:
            sig = Signal(r["strategy"], r["symbol"], r["side"], r["entry_minute"],
                         r["entry_price"], r["hold_min"], r.get("note", ""))
            p = Position(sig, r["entry_minute"], r["entry_price"], r["notional_usdt"],
                         r["exit_due"], r.get("fr_at_entry", 0.0))
            # 舊版 state 沒有這兩欄: 以全域槓桿回推, 避免組保證金占用被低估
            p.margin_usdt = r.get("margin_usdt") or r["notional_usdt"] / self.risk.leverage
            p.leverage = r.get("leverage") or self.risk.leverage
            p.exit_minute = r.get("exit_minute")
            p.exit_price = r.get("exit_price")
            p.exit_reason = r.get("exit_reason", "")
            p.pnl_bps = r.get("pnl_bps")
            p.pnl_usdt = r.get("pnl_usdt")
            p.mfe_bps = r.get("mfe_bps", 0.0)
            p.mae_bps = r.get("mae_bps", 0.0)
            p.order_id = r.get("order_id", "")
            p.fee_usdt = r.get("fee_usdt")
            p.funding_usdt = r.get("funding_usdt")
            p.pnl_source = r.get("pnl_source", "model")
            return p
        self.open_positions = [_pos(r) for r in d.get("open", [])]
        self.closed = [_pos(r) for r in d.get("closed", [])]
        self.cooldown_until = {tuple(k.split("|", 1)): v for k, v in d.get("cooldown", {}).items()}
