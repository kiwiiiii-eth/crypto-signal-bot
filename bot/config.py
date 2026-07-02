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
class StrategyACfg:  # OI減+價漲 → 做空（回測: 1h 淨+68bps 勝率66%）
    window_min: int = 3
    oi_drop_pct: float = -1.5
    px_up_pct: float = 1.5
    hold_min: int = 60
    cooldown_min: int = 30


@dataclass(frozen=True)
class StrategyBCfg:  # 極端正費率 → 做空收 carry（回測: 4h 淨+97bps 正日比88%）
    fr_threshold: float = 0.001  # +0.1%
    hold_min: int = 240
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
class RiskCfg:
    equity_usdt: float = _f("EQUITY_USDT", 1000.0)
    position_pct: float = _f("POSITION_PCT", 5.0)   # 單筆 = 本金 5%
    max_positions: int = _i("MAX_POSITIONS", 8)
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
    risk: RiskCfg = field(default_factory=RiskCfg)
    tg: TelegramCfg = field(default_factory=TelegramCfg)
    influx_url: str = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    influx_token: str = os.getenv("INFLUXDB_TOKEN", "")
    influx_org: str = os.getenv("INFLUXDB_ORG", "crypto")
    influx_bucket: str = os.getenv("INFLUXDB_BUCKET", "exchange")
