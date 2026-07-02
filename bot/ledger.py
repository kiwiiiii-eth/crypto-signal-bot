"""Paper trading 帳本：倉位、固定時間出場、災難停損、費用與 funding carry。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import FEE_BPS, RiskCfg
from .strategies import Signal


@dataclass
class Position:
    signal: Signal
    entry_minute: int
    entry_price: float
    notional_usdt: float
    exit_due: int                 # entry + hold
    fr_at_entry: float = 0.0
    # 出場後填入
    exit_minute: int | None = None
    exit_price: float | None = None
    exit_reason: str = ""         # "time" / "stop"
    pnl_bps: float | None = None
    pnl_usdt: float | None = None


@dataclass
class Ledger:
    risk: RiskCfg
    open_positions: list[Position] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    cooldown_until: dict[tuple[str, str], int] = field(default_factory=dict)  # (strategy,sym) -> minute
    rejected: dict[str, int] = field(default_factory=dict)

    def _reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1

    def try_open(self, sig: Signal, cooldown_min: int, fr: float = 0.0) -> Position | None:
        key = (sig.strategy, sig.symbol)
        if sig.minute < self.cooldown_until.get(key, -1):
            self._reject("冷卻中")
            return None
        if len(self.open_positions) >= self.risk.max_positions:
            self._reject("倉位滿")
            return None
        if any(p.signal.symbol == sig.symbol for p in self.open_positions):
            self._reject("同幣持倉中")
            return None
        self.cooldown_until[key] = sig.minute + cooldown_min
        pos = Position(
            signal=sig,
            entry_minute=sig.minute,
            entry_price=sig.price,
            notional_usdt=self.risk.equity_usdt * self.risk.position_pct / 100,
            exit_due=sig.minute + sig.hold_min,
            fr_at_entry=fr,
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
            adverse_bps = -(px / pos.entry_price - 1) * 10000 * side
            if adverse_bps >= self.risk.disaster_stop_bps:
                self._close(pos, minute, px, "stop")
                done.append(pos)
            elif minute >= pos.exit_due:
                self._close(pos, minute, px, "time")
                done.append(pos)
        return done

    def _close(self, pos: Position, minute: int, px: float, reason: str) -> None:
        side = pos.signal.side
        gross = (px / pos.entry_price - 1) * 10000 * side
        held = minute - pos.entry_minute
        carry = -pos.fr_at_entry * 10000 * side * (held / 480)  # 每 8h 結算近似
        pos.exit_minute = minute
        pos.exit_price = px
        pos.exit_reason = reason
        pos.pnl_bps = gross + carry - FEE_BPS
        pos.pnl_usdt = pos.notional_usdt * pos.pnl_bps / 10000
        self.open_positions.remove(pos)
        self.closed.append(pos)

    @property
    def total_pnl_usdt(self) -> float:
        return sum(p.pnl_usdt or 0 for p in self.closed)
