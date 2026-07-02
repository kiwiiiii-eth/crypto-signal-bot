"""三條訊號策略。皆為逆勢做空，出場以固定持有時間為主（回測證明優於停利/緊停損）。

每條策略提供兩個介面：
- check(sym, px, oi, fr, i)：單點檢查（實時模式，i = 最新分鐘索引）
- trigger_mask(px, oi, fr)：向量化整段布林遮罩（重放模式），條件與 check 完全一致
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import StrategyACfg, StrategyBCfg, StrategyCCfg


@dataclass(frozen=True)
class Signal:
    strategy: str      # "A" / "B" / "C"
    symbol: str
    side: int          # -1 = 做空
    minute: int        # epoch 分鐘
    price: float
    hold_min: int
    note: str = ""


class StrategyA:
    """A｜3 分鐘 OI 減 ≥1.5% 且價漲 ≥1.5% → 做空 1h。"""

    name = "A"
    label = "OI減價漲"

    def __init__(self, cfg: StrategyACfg):
        self.cfg = cfg

    def trigger_mask(self, px: np.ndarray, oi: np.ndarray, fr: np.ndarray) -> np.ndarray:
        w = self.cfg.window_min
        d_oi = np.full_like(px, np.nan)
        d_px = np.full_like(px, np.nan)
        d_oi[w:] = (oi[w:] / oi[:-w] - 1) * 100
        d_px[w:] = (px[w:] / px[:-w] - 1) * 100
        with np.errstate(invalid="ignore"):
            return (d_oi <= self.cfg.oi_drop_pct) & (d_px >= self.cfg.px_up_pct)

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray, i: int) -> Signal | None:
        w = self.cfg.window_min
        if i < w or np.isnan(px[i]) or np.isnan(px[i - w]) or np.isnan(oi[i]) or np.isnan(oi[i - w]):
            return None
        d_oi = (oi[i] / oi[i - w] - 1) * 100
        d_px = (px[i] / px[i - w] - 1) * 100
        if d_oi <= self.cfg.oi_drop_pct and d_px >= self.cfg.px_up_pct:
            return Signal(self.name, sym, -1, i, float(px[i]), self.cfg.hold_min,
                          note=f"3m OI {d_oi:+.2f}% 價 {d_px:+.2f}%")
        return None


class StrategyB:
    """B｜資金費率 ≥ +0.1% → 做空 4h 收 carry。"""

    name = "B"
    label = "極端正費率"

    def __init__(self, cfg: StrategyBCfg):
        self.cfg = cfg

    def trigger_mask(self, px: np.ndarray, oi: np.ndarray, fr: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return fr >= self.cfg.fr_threshold

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray, i: int) -> Signal | None:
        if np.isnan(px[i]) or np.isnan(fr[i]):
            return None
        if fr[i] >= self.cfg.fr_threshold:
            return Signal(self.name, sym, -1, i, float(px[i]), self.cfg.hold_min,
                          note=f"fr {fr[i] * 100:+.4f}%")
        return None


class StrategyC:
    """C｜1h |ΔOI|≥5% 且 |Δ價|≥1% → 逆勢（與價格反向）1h。預設關閉。"""

    name = "C"
    label = "1h異動逆勢"

    def __init__(self, cfg: StrategyCCfg):
        self.cfg = cfg

    def trigger_mask(self, px: np.ndarray, oi: np.ndarray, fr: np.ndarray) -> np.ndarray:
        w = self.cfg.window_min
        d_oi = np.full_like(px, np.nan)
        d_px = np.full_like(px, np.nan)
        d_oi[w:] = (oi[w:] / oi[:-w] - 1) * 100
        d_px[w:] = (px[w:] / px[:-w] - 1) * 100
        with np.errstate(invalid="ignore"):
            return (np.abs(d_oi) >= self.cfg.oi_abs_pct) & (np.abs(d_px) >= self.cfg.px_abs_pct)

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray, i: int) -> Signal | None:
        w = self.cfg.window_min
        if i < w or np.isnan(px[i]) or np.isnan(px[i - w]) or np.isnan(oi[i]) or np.isnan(oi[i - w]):
            return None
        d_oi = (oi[i] / oi[i - w] - 1) * 100
        d_px = (px[i] / px[i - w] - 1) * 100
        if abs(d_oi) >= self.cfg.oi_abs_pct and abs(d_px) >= self.cfg.px_abs_pct:
            side = -1 if d_px > 0 else 1  # 逆勢
            return Signal(self.name, sym, side, i, float(px[i]), self.cfg.hold_min,
                          note=f"1h OI {d_oi:+.1f}% 價 {d_px:+.1f}% 逆勢")
        return None
