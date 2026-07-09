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
class StrategyHCfg:  # 擠多頂做空（第二組）: 24h漲+OI增+正費率 → OI回落扳機 → 空24h
    # 依據: 2026-06-20→30 小時級+分鐘級重放, 淨+1.9%/24h 勝率64-67%, 詳見
    # vault "Lending Rate Strategy Candidates 2026-07-10"
    ret24_pct: float = _f("H_RET24_PCT", 8.0)        # 24h 漲幅門檻
    doi24_pct: float = _f("H_DOI24_PCT", 10.0)       # OI 24h 增幅門檻
    fr_threshold: float = _f("H_FR_THRESHOLD", 0.0003)  # 預估費率 0.03%
    oi_pullback_pct: float = _f("H_OI_PULLBACK_PCT", 2.0)  # 扳機: OI 自近2h高點回落
    oi_high_window_min: int = 120
    armed_ttl_min: int = 720                          # armed 超過 12h 未觸發即撤銷
    hold_min: int = _i("H_HOLD_MIN", 1440)
    cooldown_min: int = 720
    enabled: bool = os.getenv("STRATEGY_H_ENABLED", "true").lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class StrategyLCfg:  # 強平反抽做多（第二組）: 4h急殺+OI急降 → 30分未創低扳機 → 多8h
    # 依據: 分鐘級重放 淨+0.6~1.0%/4-8h 勝率60-62% n=233; 做多必須等企穩,
    # 但等超過60分會錯過反抽
    ret4_pct: float = _f("L_RET4_PCT", -8.0)          # 4h 跌幅門檻
    doi4_pct: float = _f("L_DOI4_PCT", -5.0)          # OI 4h 降幅門檻
    stab_min: int = _i("L_STAB_MIN", 30)              # 企穩: N 分鐘未再創低
    armed_ttl_min: int = 360                          # armed 超過 6h 未企穩即撤銷
    lend_z_veto: float = _f("L_LEND_Z_VETO", 2.0)     # 利率飆升中(空頭仍在借幣)不接
    hold_min: int = _i("L_HOLD_MIN", 480)
    cooldown_min: int = 720
    enabled: bool = os.getenv("STRATEGY_L_ENABLED", "true").lower() in {"1", "true", "yes"}


# 策略分組: 第一組=原有策略, 第二組=利率共鳴新策略。兩組各自獨立資金池
# (各 1000U 權益、5x、單筆保證金 100U → 名目 500U), 報表分組比較。
STRATEGY_GROUPS: dict[str, str] = {"E": "G1", "F": "G1", "G": "G1", "C": "G1",
                                   "H": "G2", "L": "G2"}


def strategy_group(strategy: str) -> str:
    return STRATEGY_GROUPS.get(strategy, "G1")


@dataclass(frozen=True)
class RegimeCfg:
    # A 空單: BTC 4h≤0 全倉(+171bps/正日比100%), >0 仍有+103 → 砍半不砍單
    btc_window_min: int = 240
    a_upsize_mult: float = _f("A_UP_REGIME_MULT", 0.5)
    # B: regime 濾網無效(上漲時反而+127), 不套用; 只加流動性下限(大幣 B 期望值 3 倍)
    b_min_oi_usd: float = _f("B_MIN_OI_USD", 5_000_000)


@dataclass(frozen=True)
class RiskCfg:
    equity_usdt: float = _f("EQUITY_USDT", 1000.0)  # 每組獨立權益(G1/G2 各一份)
    margin_usdt: float = _f("MARGIN_USDT", 100.0)   # 單筆保證金
    leverage: float = _f("LEVERAGE", 5.0)           # 名目 = 保證金 × 槓桿 (100U×5=500U)
    max_positions: int = _i("MAX_POSITIONS", 8)     # 每策略獨立上限
    # 重放掃描: -3% 會砍掉 25% 的單且多數會回來; -8% 觸發率 8%、EV 幾乎不損, 尾部保護仍在
    disaster_stop_bps: float = _f("DISASTER_STOP_BPS", 800.0)
    # 每組保證金上限 (G1/G2 各自獨立計算; 預設 = 組權益 9 成)
    margin_cap_usdt: float = _f("TOTAL_MARGIN_CAP", _f("EQUITY_USDT", 1000.0) * 0.9)
    # live 風控閘門: 達標後停止開新倉, 既有倉位仍照規則出場
    daily_loss_limit_usdt: float = _f("DAILY_LOSS_LIMIT_USDT", 5.0)
    daily_stop_limit: int = _i("DAILY_STOP_LIMIT", 2)
    equity_floor_usdt: float = _f("EQUITY_FLOOR_USDT", _f("EQUITY_USDT", 1000.0) * 0.92)


@dataclass(frozen=True)
class ExecCfg:
    # paper: 純模擬 / demo: Bitget Demo API key (paptrading:1) / live: 真錢
    mode: str = os.getenv("EXECUTION_MODE", "paper")
    api_key: str = os.getenv("BITGET_API_KEY", "")
    api_secret: str = os.getenv("BITGET_API_SECRET", "")
    passphrase: str = os.getenv("BITGET_PASSPHRASE", "")
    # 開倉前 Bitget 盤口 vs Binance 訊號價偏差上限, 超過即棄單（避免追價/兩所脫鉤）
    max_dev_bps: float = _f("MAX_PRICE_DEV_BPS", 50.0)
    # maker 優先: post-only 貼盤口掛單, 逾時撤單重新貼價再掛（maker 2bps vs taker 6bps）
    maker_first: bool = os.getenv("MAKER_FIRST", "true").lower() in {"1", "true", "yes"}
    maker_wait_sec: float = _f("MAKER_WAIT_SEC", 30.0)
    entry_chase: int = _i("ENTRY_CHASE", 3)    # 開倉追掛輪數, 掛不到=棄單(不吃市價)
    close_chase: int = _i("CLOSE_CHASE", 10)   # 平倉追掛輪數, 用盡才市價兜底


@dataclass(frozen=True)
class TelegramCfg:
    token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    thread_signals: str = os.getenv("TG_THREAD_SIGNALS", "")
    thread_fills: str = os.getenv("TG_THREAD_FILLS", "")
    thread_daily: str = os.getenv("TG_THREAD_DAILY", "")


@dataclass(frozen=True)
class CoinGlassCfg:
    api_key: str = os.getenv("COINGLASS_API_KEY", "")
    exchange: str = os.getenv("COINGLASS_EXCHANGE", "Binance")


@dataclass(frozen=True)
class Settings:
    a: StrategyACfg = field(default_factory=StrategyACfg)
    b: StrategyBCfg = field(default_factory=StrategyBCfg)
    c: StrategyCCfg = field(default_factory=StrategyCCfg)
    d: StrategyDCfg = field(default_factory=StrategyDCfg)
    h: StrategyHCfg = field(default_factory=StrategyHCfg)
    l: StrategyLCfg = field(default_factory=StrategyLCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    exec_: ExecCfg = field(default_factory=ExecCfg)
    tg: TelegramCfg = field(default_factory=TelegramCfg)
    coinglass: CoinGlassCfg = field(default_factory=CoinGlassCfg)
    influx_url: str = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    influx_token: str = os.getenv("INFLUXDB_TOKEN", "")
    influx_org: str = os.getenv("INFLUXDB_ORG", "crypto")
    influx_bucket: str = os.getenv("INFLUXDB_BUCKET", "exchange")
