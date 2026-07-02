# crypto-signal-bot

逆勢做空訊號 bot（paper trading）。規格見 SecondBrain《Trading Bot 架構藍圖》§8。

## 策略
- **A｜OI減價漲做空**：3m ΔOI≤-1.5% 且 Δ價≥+1.5% → 做空 1h（回測 淨+68bps、勝率66%）
- **B｜極端正費率做空**：fr≥+0.1% → 做空 4h 收 carry，冷卻 8h（回測 淨+97bps、正日比88%）
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
