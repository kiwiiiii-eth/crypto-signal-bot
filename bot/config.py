"""集中設定。全部可用環境變數覆蓋，預設值 = 藍圖 §8 定案規格。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


# 訊號源是 Binance、下單在 Bitget，兩邊都排除主流幣與 USDC 對
EXCLUDED_TOKENS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "TRXUSDT"}

FEE_BPS = 8.0  # Bitget maker 進 maker 出來回


@dataclass(frozen=True)
class StrategyACfg:  # OI減+價漲 → 做空
    # 持有掃描(扣市場漂移的真alpha): 1h +65 / 2h +145(勝率71%,尾部最淺) / 8h +271(總量最高但佔槽+尾肥)
    window_min: int = 3
    oi_drop_pct: float = -1.5
    px_up_pct: float = 1.5
    hold_min: int = _i("A_HOLD_MIN", 120)
    cooldown_min: int = 30


@dataclass(frozen=True)
class StrategyBCfg:  # 極端正費率 → 做空收 carry（回測: 4h 淨+97bps 正日比88%）
    fr_threshold: float = 0.001  # +0.1%
    hold_min: int = _i("B_HOLD_MIN", 240)
    cooldown_min: int = 480  # 8h = 一個結算週期


@dataclass(frozen=True)
class StrategyCCfg:  # 1h 異動逆勢（回測: 淨+17.9bps，量大單薄，預設關）
    window_min: int = 60
    oi_abs_pct: float = 5.0
    px_abs_pct: float = 1.0
    hold_min: int = 60
    cooldown_min: int = 60
    enabled: bool = os.getenv("STRATEGY_C_ENABLED", "false").lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class StrategyDCfg:  # OI增+價跌 → 做多（A 的鏡像: 空頭擁擠過度→反彈; 回測 4h 淨+47bps 勝率64%）
    window_min: int = 3
    oi_up_pct: float = 1.5
    px_down_pct: float = -1.5
    hold_min: int = _i("D_HOLD_MIN", 240)
    cooldown_min: int = 30
    enabled: bool = os.getenv("STRATEGY_D_ENABLED", "true").lower() in {"1", "true", "yes"}
    # 濾網回測: D 在 BTC 4h 下跌時 +144bps/勝率69%, 上漲時 -52bps → 只在跌勢接反彈
    require_btc_down: bool = True
    min_oi_usd: float = _f("D_MIN_OI_USD", 1_000_000)


@dataclass(frozen=True)
class RegimeCfg:
    # A 空單: BTC 4h≤0 全倉(+171bps/正日比100%), >0 仍有+103 → 砍半不砍單
    btc_window_min: int = 240
    a_upsize_mult: float = _f("A_UP_REGIME_MULT", 0.5)
    # B: regime 濾網無效(上漲時反而+127), 不套用; 只加流動性下限(大幣 B 期望值 3 倍)
    b_min_oi_usd: float = _f("B_MIN_OI_USD", 5_000_000)


@dataclass(frozen=True)
class RiskCfg:
    equity_usdt: float = _f("EQUITY_USDT", 1000.0)
    margin_usdt: float = _f("MARGIN_USDT", 50.0)    # 單筆保證金
    leverage: float = _f("LEVERAGE", 5.0)           # 名目 = 保證金 × 槓桿
    max_positions: int = _i("MAX_POSITIONS", 8)     # 每策略獨立上限
    # 重放掃描: -3% 會砍掉 25% 的單且多數會回來; -8% 觸發率 8%、EV 幾乎不損, 尾部保護仍在
    disaster_stop_bps: float = _f("DISASTER_STOP_BPS", 800.0)


@dataclass(frozen=True)
class TelegramCfg:
    token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    thread_signals: str = os.getenv("TG_THREAD_SIGNALS", "")
    thread_fills: str = os.getenv("TG_THREAD_FILLS", "")
    thread_daily: str = os.getenv("TG_THREAD_DAILY", "")


@dataclass(frozen=True)
class Settings:
    a: StrategyACfg = field(default_factory=StrategyACfg)
    b: StrategyBCfg = field(default_factory=StrategyBCfg)
    c: StrategyCCfg = field(default_factory=StrategyCCfg)
    d: StrategyDCfg = field(default_factory=StrategyDCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    tg: TelegramCfg = field(default_factory=TelegramCfg)
    influx_url: str = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    influx_token: str = os.getenv("INFLUXDB_TOKEN", "")
    influx_org: str = os.getenv("INFLUXDB_ORG", "crypto")
    influx_bucket: str = os.getenv("INFLUXDB_BUCKET", "exchange")
