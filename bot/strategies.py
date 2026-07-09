"""三條訊號策略。皆為逆勢做空，出場以固定持有時間為主（回測證明優於停利/緊停損）。

每條策略提供兩個介面：
- check(sym, px, oi, fr, i)：單點檢查（實時模式，i = 最新分鐘索引）
- trigger_mask(px, oi, fr)：向量化整段布林遮罩（重放模式），條件與 check 完全一致
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import (StrategyACfg, StrategyBCfg, StrategyCCfg, StrategyDCfg,
                     StrategyHCfg, StrategyLCfg)


@dataclass(frozen=True)
class Signal:
    # 對外代號（Telegram/帳本/state.json 用這個）: E=StrategyA, F=StrategyB,
    # G=StrategyD, C=StrategyC。類別名 A/B/C/D 是開發期編號, 詳見 docs/ARCHITECTURE.md
    strategy: str      # "E" / "F" / "G" / "C"
    symbol: str
    side: int          # -1 = 做空
    minute: int        # epoch 分鐘
    price: float
    hold_min: int
    note: str = ""


class StrategyA:
    """E｜3 分鐘 OI 減 ≥1.5% 且價漲 ≥1.5% → 做空 A_HOLD_MIN 分鐘（預設 120 = 2h）。"""

    name = "E"
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

    def alert_mask(self, px: np.ndarray, oi: np.ndarray) -> np.ndarray:
        """任何 |ΔOI|≥閾值 的警報（含 OI增）。冷卻鍵用這個：
        剛震盪完（任何方向警報）30 分內的再觸發是 whipsaw，重放驗證均虧 -198bps。"""
        w = self.cfg.window_min
        d_oi = np.full_like(px, np.nan)
        d_oi[w:] = (oi[w:] / oi[:-w] - 1) * 100
        with np.errstate(invalid="ignore"):
            return np.abs(d_oi) >= abs(self.cfg.oi_drop_pct)

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

    name = "F"
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


class StrategyD:
    """D｜3 分鐘 OI 增 ≥1.5% 且價跌 ≥1.5% → 做多 4h（A 的鏡像：空頭擁擠 fade）。"""

    name = "G"
    label = "OI增價跌"

    def __init__(self, cfg: StrategyDCfg):
        self.cfg = cfg

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray, i: int) -> Signal | None:
        w = self.cfg.window_min
        if i < w or np.isnan(px[i]) or np.isnan(px[i - w]) or np.isnan(oi[i]) or np.isnan(oi[i - w]):
            return None
        d_oi = (oi[i] / oi[i - w] - 1) * 100
        d_px = (px[i] / px[i - w] - 1) * 100
        if d_oi >= self.cfg.oi_up_pct and d_px <= self.cfg.px_down_pct:
            return Signal(self.name, sym, +1, i, float(px[i]), self.cfg.hold_min,
                          note=f"3m OI {d_oi:+.2f}% 價 {d_px:+.2f}%")
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


class StrategyH:
    """H｜第二組·擠多頂做空: 24h漲≥8% + OI 24h增≥10% + 費率≥0.03% 進入 armed,
    OI 自近 2h 高點回落 ≥2%（多頭撤退）觸發進場。空 24h。

    兩階段有狀態: armed/低點追蹤存在實例內（僅實時模式用; 重放用 trigger_mask 近似,
    只含狀態條件不含扳機）。依據: 分鐘級重放 +2.0%/24h 勝率64-67% (n=39)。
    """

    name = "H"
    label = "擠多頂空"
    window_min = 1440

    def __init__(self, cfg: StrategyHCfg):
        self.cfg = cfg
        self.armed: dict[str, int] = {}  # sym -> armed 分鐘

    def trigger_mask(self, px: np.ndarray, oi: np.ndarray, fr: np.ndarray) -> np.ndarray:
        w = self.window_min
        d_oi = np.full_like(px, np.nan)
        d_px = np.full_like(px, np.nan)
        d_oi[w:] = (oi[w:] / oi[:-w] - 1) * 100
        d_px[w:] = (px[w:] / px[:-w] - 1) * 100
        with np.errstate(invalid="ignore"):
            return (d_px >= self.cfg.ret24_pct) & (d_oi >= self.cfg.doi24_pct) & \
                   (fr >= self.cfg.fr_threshold)

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray,
              i: int, minute: int) -> tuple[Signal | None, str]:
        """回傳 (Signal|None, phase)。phase: '' / 'armed' / 'expired' 供觸發記錄用。"""
        w = self.window_min
        if i < w or np.isnan(px[i]) or np.isnan(px[i - w]) or \
                np.isnan(oi[i]) or np.isnan(oi[i - w]) or oi[i - w] <= 0:
            return None, ""
        ret24 = (px[i] / px[i - w] - 1) * 100
        doi24 = (oi[i] / oi[i - w] - 1) * 100
        frv = fr[i] if not np.isnan(fr[i]) else 0.0
        state_ok = (ret24 >= self.cfg.ret24_pct and doi24 >= self.cfg.doi24_pct
                    and frv >= self.cfg.fr_threshold)
        phase = ""
        if state_ok and sym not in self.armed:
            self.armed[sym] = minute
            phase = "armed"
        elif sym in self.armed and not state_ok and \
                minute - self.armed[sym] > self.cfg.armed_ttl_min:
            del self.armed[sym]
            return None, "expired"
        if sym not in self.armed:
            return None, phase
        # 扳機: OI 自近 2h 高點回落
        lo = max(0, i - self.cfg.oi_high_window_min)
        window = oi[lo:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < 10:
            return None, phase
        oi_high = float(valid.max())
        if oi[i] <= oi_high * (1 - self.cfg.oi_pullback_pct / 100):
            del self.armed[sym]
            note = f"24h {ret24:+.1f}% OI24h {doi24:+.1f}% fr {frv*100:+.3f}% OI回落"
            return Signal(self.name, sym, -1, minute, float(px[i]),
                          self.cfg.hold_min, note=note), "entered"
        return None, phase


class StrategyL:
    """L｜第二組·強平反抽做多: 4h跌≥8% + OI 4h降≥5% 進入 armed 並追蹤低點,
    30 分鐘未再創低（企穩）觸發進場。多 8h。利率 z>2（空頭仍在借幣）由引擎否決。

    依據: 分鐘級重放 淨+0.6~1.0%/4-8h 勝率60-62% (n=233); 接飛刀本質,
    4h 內最大浮虧中位 -2.6%, 停損 -8% 必須在。
    """

    name = "L"
    label = "強平反抽"
    window_min = 240

    def __init__(self, cfg: StrategyLCfg):
        self.cfg = cfg
        self.armed: dict[str, int] = {}       # sym -> armed 分鐘
        self.low: dict[str, tuple[float, int]] = {}  # sym -> (低點價, 低點分鐘)

    def trigger_mask(self, px: np.ndarray, oi: np.ndarray, fr: np.ndarray) -> np.ndarray:
        w = self.window_min
        d_oi = np.full_like(px, np.nan)
        d_px = np.full_like(px, np.nan)
        d_oi[w:] = (oi[w:] / oi[:-w] - 1) * 100
        d_px[w:] = (px[w:] / px[:-w] - 1) * 100
        with np.errstate(invalid="ignore"):
            return (d_px <= self.cfg.ret4_pct) & (d_oi <= self.cfg.doi4_pct)

    def check(self, sym: str, px: np.ndarray, oi: np.ndarray, fr: np.ndarray,
              i: int, minute: int) -> tuple[Signal | None, str]:
        w = self.window_min
        if i < w or np.isnan(px[i]) or np.isnan(px[i - w]) or \
                np.isnan(oi[i]) or np.isnan(oi[i - w]) or oi[i - w] <= 0:
            return None, ""
        ret4 = (px[i] / px[i - w] - 1) * 100
        doi4 = (oi[i] / oi[i - w] - 1) * 100
        state_ok = ret4 <= self.cfg.ret4_pct and doi4 <= self.cfg.doi4_pct
        phase = ""
        if state_ok:
            if sym not in self.armed:
                self.armed[sym] = minute
                self.low[sym] = (float(px[i]), minute)
                phase = "armed"
            elif px[i] < self.low[sym][0]:
                self.low[sym] = (float(px[i]), minute)
        if sym not in self.armed:
            return None, phase
        if minute - self.armed[sym] > self.cfg.armed_ttl_min:
            del self.armed[sym]
            self.low.pop(sym, None)
            return None, "expired"
        low_px, low_min = self.low[sym]
        if minute - low_min >= self.cfg.stab_min:  # 企穩: N 分未創低
            del self.armed[sym]
            self.low.pop(sym, None)
            note = (f"4h {ret4:+.1f}% OI4h {doi4:+.1f}% "
                    f"{self.cfg.stab_min}分未創低(低點 {low_px:.6g})")
            return Signal(self.name, sym, +1, minute, float(px[i]),
                          self.cfg.hold_min, note=note), "entered"
        return None, phase
