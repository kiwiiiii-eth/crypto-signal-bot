# 系統架構

## 一句話

Binance 行情做訊號 → 逆勢策略 → 本地帳本風控 → Bitget maker 執行 → Telegram 回報。
每 60 秒一個 tick,單進程單主迴圈,worker thread 只負責下單。

## 資料流

```
┌─────────────┐   60s tick    ┌──────────────┐   Signal    ┌──────────┐
│ BinanceFeed  │ ────────────▶ │  Strategies  │ ──────────▶ │  Engine  │
│ premiumIndex │  px/oi/fr     │ E / F / G(/C)│             │ 濾網+風控 │
│ openInterest │  滾動分鐘序列  └──────────────┘             └────┬─────┘
└─────────────┘                                                  │ try_open
      │ bench (BTC momentum, regime 濾網)                        ▼
      │                                              ┌──────────────────┐
      │                                              │      Ledger      │
      │                                              │ 倉位/冷卻/停損/   │
      │              mark() 每分鐘 ◀──────────────── │ 到期出場/PnL      │
      │                                              └───┬──────────┬───┘
      ▼                                                  │ state.json│
┌──────────────┐    open/close (worker thread)           ▼          │
│   Executor   │ ◀───────────────────────────── (mode=demo/live 才有)│
│ Bitget maker │ ──▶ _execq ──▶ 主迴圈記帳/對帳                      │
└──────────────┘                                                    ▼
                                                          ┌──────────────┐
                                                          │   Notifier    │
                                                          │ Telegram 推播 │
                                                          │ + CommandServer│
                                                          └──────────────┘
```

## 模組職責

| 模組 | 職責 | 邊界 |
|---|---|---|
| `bot/feed.py` `BinanceFeed` | 每 tick 拉全市場 premiumIndex + 逐檔 OI,維持記憶體分鐘序列;BTC momentum 供 regime 濾網 | 只讀 Binance,不碰 InfluxDB |
| `bot/strategies.py` | 純函數式訊號判定:`check()`(實時單點)與 `trigger_mask()`(重放向量化)條件一致 | 不知道倉位、不知道交易所 |
| `bot/engine.py` `LiveEngine` | 主迴圈:訊號後濾網(regime、OI 下限、Bybit 否決、Bitget funding)、safety 閘門、開平倉調度、對帳、日報 | 帳本只在主執行緒改;下單丟 worker thread |
| `bot/ledger.py` `Ledger` | 唯一事實帳本:開倉限制(冷卻/倉位上限/同幣/保證金 cap)、災難停損、到期出場、PnL;`to_dict`/`restore` 持久化 | 純模型,不發網路請求 |
| `bot/executor.py` `BitgetExecutor` | Bitget 下單:純 maker 追掛、盤口比價棄單、交易所端停損、平倉冪等、殘量閃電掃尾、結算回查 | 只被 engine 呼叫;paper 模式下不存在 |
| `bot/notifier.py` + `bot/commands.py` | Telegram 推播(訊號/平倉/日報)與指令(/status /positions /pnl /equity /pause /resume /kill /chart…) | CommandServer 持 engine 引用,long-poll 執行緒 |
| `bot/replay.py` | 歷史分鐘 CSV 重放,走同一套 strategies+ledger | 回測與實盤同邏輯的保證 |
| `bot/hl_watch.py` | Hyperliquid 大戶倉位監看,純情報附註 | 不影響下單 |
| `scripts/` | 周邊:ma_collector(寫 InfluxDB crypto_ma)、health_monitor(欄位級監控)、tradfi_monitor、回測 | 與 bot 主進程互不依賴 |
| `reporter/` | 結構化事件→通道(Discord/Telegram)投遞層 | health_monitor 已用;bot 主流程仍直連 Telegram(待遷移) |

## 執行模式(EXECUTION_MODE)

| mode | 行情 | 下單 | 用途 |
|---|---|---|---|
| `paper`(預設) | 真實 Binance | 無(帳本模擬) | 訊號驗證 |
| `demo` | 真實 Binance | Bitget 模擬盤(SUSDT-FUTURES,僅 BTC/ETH/XRP) | 驗證下單鏈 |
| `live` | 真實 Binance | Bitget 真錢 | 實盤(現行) |

三種模式共用 strategies/ledger/engine,差別只在 executor 存不存在與指向哪個環境。

## 策略命名對照(重要)

類別名是開發期編號,對外代號才是 Telegram/帳本/日報用的名字:

- `StrategyA`(cfg.a, `A_HOLD_MIN`)→ 對外 **E**|OI減價漲做空
- `StrategyB`(cfg.b, `B_HOLD_MIN`)→ 對外 **F**|極端正費率做空
- `StrategyD`(cfg.d, `D_HOLD_MIN`)→ 對外 **G**|OI增價跌做多
- `StrategyC`(cfg.c)→ 對外 **C**|1h 異動逆勢,預設關閉

E+F 同幣同時觸發(進場時 funding 同時極端)= 旗艦訊號 🏴。

## 風控層次(由外而內)

1. **safety 閘門**(engine):kill.flag > paused.flag > 日虧上限 > 日停損筆數 > 權益地板 → 停開新倉,不動既有倉位
2. **訊號濾網**(engine):regime(BTC 4h)、OI 美元下限、Bybit 費率否決、Bitget funding 濾網、盤口偏差 ≤50bps
3. **帳本限制**(ledger):per-symbol 冷卻、每策略 8 倉、同幣不加倉、總保證金 cap
4. **出場**(ledger+executor):固定持有時間;災難停損 -8%(交易所條件單先行,程式端冪等);逐倉強平模擬封頂

## 狀態與持久化

- `data/state.json` — 帳本全量(開倉/歷史/冷卻),每 tick 覆寫,重啟還原後與交易所對帳
- `data/paused.flag` / `data/kill.flag` — 檔案即狀態,內容為原因字串;這是外部(含 Hermes)控制 bot 的**唯一**入口
- `data/slippage.csv` — 訊號價 vs 成交價簽核
- `data/bitget_symbols.json` / `bitget_contracts.json` — 上架交集與合約規格(手動 curl 更新)

## 與 Hermes 生態的關係

```
Server A                                    Jetson
┌────────────────────────────┐             ┌─────────────────────┐
│ crypto-collector (Zeabur)  │──InfluxDB──▶│ hermes-financial-mvp │
│ crypto-signal-bot (本repo) │             │ FastAPI (唯讀分析)   │
│  └ 真錢實盤, systemd       │             │ Hermes Agent + LLM   │
│ scripts/ (cron)            │◀──flag 檔──│  (未來: 唯讀+單向煞車)│
└────────────────────────────┘             └─────────────────────┘
```

Hermes 對本 bot 的介面規格見 `docs/HERMES_SKILL_SPEC.md`:只有唯讀查詢
與單向煞車(pause/kill),沒有下單路徑。
