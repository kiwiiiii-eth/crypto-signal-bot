# crypto-signal-bot

逆勢做空訊號 bot（paper trading）。規格見 SecondBrain《Trading Bot 架構藍圖》§8。

## 策略（E/F/G）
- **E｜OI減價漲做空**：3m ΔOI≤-1.5% 且 Δ價≥+1.5% → 做空 1h（回測 淨+68bps、勝率66%）
- **F｜極端正費率做空**：fr≥+0.1% → 做空 4h 收 carry，冷卻 8h（回測 淨+97bps、正日比88%）
- **C｜1h異動逆勢**：預設關閉（`STRATEGY_C_ENABLED=true` 開啟）
- A+B 同幣同時觸發 = 旗艦訊號 🏴

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
