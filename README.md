# crypto-signal-bot

逆勢訊號交易 bot（Bitget USDT-M；EXECUTION_MODE=paper/demo/live）。規格見 SecondBrain《Trading Bot 架構藍圖》§8。
架構與模組職責見 `docs/ARCHITECTURE.md`；開發守則見 `CLAUDE.md`。

## 策略（E/F/G/C）
- **E｜OI減價漲做空**：3m ΔOI≤-1.5% 且 Δ價≥+1.5% → 做空 `A_HOLD_MIN`（預設 2h）
- **F｜極端正費率做空**：fr≥+0.1% → 做空 4h 收 carry，冷卻 8h（回測 淨+97bps、正日比88%）
- **G｜OI增價跌做多**：3m ΔOI≥+1.5% 且 Δ價≤-1.5% → 做多 4h（E 鏡像，僅 BTC 4h 跌勢時）
- **C｜1h異動逆勢**：預設關閉（`STRATEGY_C_ENABLED=true` 開啟）
- E+F 同幣同時觸發 = 旗艦訊號 🏴

> 命名注意：程式類別名 `StrategyA/B/D/C` 是開發期編號，對外代號依序為 E/F/G/C；
> 環境變數用類別編號（`A_HOLD_MIN`、`D_MIN_OI_USD`），推播與帳本用對外代號。

## 風控（回測定案，勿隨意加停利/緊停損）
- 出場 = 固定持有時間；只掛災難停損 -8%（重放驗證: -3% 太緊會砍掉會回來的單）
- 單筆本金 5%、最多 8 倉、同幣不加倉、per-symbol 冷卻

## 使用
```bash
# 重放驗證（歷史分鐘 CSV）
python main.py replay minute_last.csv funding_minute.csv trades_out.csv
# 實時模式：等新 InfluxDB 上線後實作 InfluxFeed
```

訊號源 Binance、下單所 Bitget；`data/bitget_symbols.json` 為上架交集過濾（curl Bitget /api/v2/mix/market/contracts 更新）。
Telegram 推播格式模仿 zeabur-fastapi-binanceTG（Markdown、分 topic），env: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TG_THREAD_*`。

## Demo 實盤模擬（live 模式）
```bash
export TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx   # 不設則只在 console 記錄
python main.py live
```
- 真實 Binance 行情（premiumIndex 全市場 + OI 逐檔掃描，每 60s 一 tick）
- 模擬下單/到時平倉/災難停損，state.json 持久化（重啟不掉倉位）
- 單筆 `MARGIN_USDT`(50) × `LEVERAGE`(5x) = 250U 名目，可用環境變數調
- TG 指令: `/status` `/positions` `/pnl` `/equity` `/help`

## 定義備註
- **OI 下限 = Binance 未平倉合約美元名目（張數×mark price, USD）**，非幣數、非 Bitget 數據。E 無下限、F ≥5M、G ≥1M。
- 災難停損 -8%＝持倉最深逆行上限（MAE 掃描定案：更緊的水位會誤殺會回來的贏單，E 的贏單 P90 逆行達 7.2%）。

## 策略分組（2026-07-10）

| 組 | 策略 | 資金池 |
|---|---|---|
| 第一組 G1 | E/F/G/C（原有） | 獨立 1000U、5x、單筆保證金 100U（名目 500U） |
| 第二組 G2 | H 擠多頂空、L 強平反抽（利率共鳴新策略） | 獨立 1000U、同上 |

- 兩組保證金上限各自計算（`TOTAL_MARGIN_CAP` 各套一份），互不排擠。
- G2 專屬倉位模型：組內共用 25 倉上限（`G2_MAX_POSITIONS`）；名目固定 500U，保證金按「剩餘池子÷剩餘槽位」浮動（滿載約 36U/倉 ≈ 13.9x），槓桿上限 20x；強平以交易所口徑模擬（1/槓桿 − 維持保證金率 0.5%），強平價比 -8% 停損近時以強平價出場、該倉保證金歸零（`exit_reason="liq"`）。
- H/L 為兩階段觸發（狀態達標 armed → 分鐘級扳機進場），全過程含條件實際值記錄於 `data/hl_triggers.jsonl`（armed/entered/expired/vetoed），供參數優化。
- H 需要 24h 行情緩衝，重啟後約 24h 才會開始觸發（feed keep_min=1500）。
- OKX 活期利率佐證層（`bot/lending.py`）：每分鐘 1 個公開請求，脫離 1% 地板 → H 加分並推 TG；L 在利率 z>2（空頭仍在借幣）時否決進場。
- 日報與 `/performance` 皆分組對比（各組單數/勝率/PnL/虛擬權益）。
- MFE/MAE（持有期最大浮盈/浮虧 bps）記錄於每筆 Position。
- 研究依據：vault `10 Projects/Crypto/Lending Rate Strategy Candidates 2026-07-10.md`。
