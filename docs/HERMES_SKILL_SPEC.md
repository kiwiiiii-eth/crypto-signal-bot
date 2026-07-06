# HERMES_SKILL_SPEC — Hermes 如何與 crypto-signal-bot 協作

狀態:**規格定案,尚未實作**。本文件是未來 `signal-bot-skill` 的合約。

## 安全原則(不可協商)

1. **能力白名單**:查狀態、查持倉、查訊號/績效、暫停、恢復、kill-switch。
   就這六件事。沒有開倉、沒有平倉、沒有改參數、沒有改模式。
2. **單向煞車**:pause/kill 只會讓 bot 更保守,所以 Hermes 可自主觸發
   (例如巡檢發現資料鏈斷裂);**resume 必須由人確認**——Hermes 可以建議
   恢復,但執行恢復需要使用者在 Discord/Telegram 明確回覆同意。
3. **LLM 不碰執行路徑**:Hermes 的 LLM 只解析意圖與組織回覆;實際動作是
   確定性的 API 呼叫,參數可枚舉、可審計。
4. **審計**:每次控制類呼叫(pause/resume/kill)記 log:時間、觸發原因、
   來源(user 指令 or Hermes 自主),並經 reporter 推播。

## 介面形式

bot 與 Hermes 不同機(Server A vs Jetson),介面走 **hermes-financial-mvp
FastAPI 的擴充路由**(Server A 本機讀 bot 的 state/flag 檔,Jetson 經 HTTP 呼叫):

```
GET  /bot/status      → 執行狀態
GET  /bot/positions   → 持倉
GET  /bot/performance → 績效(當日/累計,分策略)
POST /bot/pause       {"reason": "..."}      → 建 data/paused.flag
POST /bot/resume      {"confirmed_by": "..."} → 刪 paused.flag(需人名)
POST /bot/kill        {"reason": "..."}      → 建 data/kill.flag
```

實作基礎:bot 的 `data/state.json`(帳本全量)與 `paused.flag`/`kill.flag`
(檔案即狀態)已經是穩定介面,API 層只是讀檔/寫 flag,**不需要改 bot 主程式**。
FastAPI 服務與 bot 同機(Server A 8010),天然能存取這些檔案。

控制類路由需要 `X-Hermes-Token` header(獨立密鑰,與 Influx/Bitget 分權);
查詢類唯讀免token。kill 之後 bot 端行為:停開新倉、既有倉照規則出場——
與現行 `/kill` Telegram 指令完全一致,API 不引入新行為。

## 各端點規格

### GET /bot/status
```json
{
  "mode": "live",
  "running": true,            // systemd active + state.json 60s 內有更新
  "paused": false, "killed": false,
  "safety_reason": "",        // 非空 = 風控閘門觸發中
  "open_positions": 3,
  "equity_usdt": 102.4,       // 上次快取值, null = 未知
  "today_realized_usdt": -1.2,
  "today_stops": 0,
  "state_age_seconds": 12
}
```
`running` 的判定用 state.json 的 mtime,不 exec 進程查詢。

### GET /bot/positions
```json
{"positions": [{
  "strategy": "F", "symbol": "XUSDT", "side": -1,
  "entry_price": 0.0123, "notional_usdt": 250.0,
  "entry_time": "2026-07-06T03:15:00Z",
  "exit_due": "2026-07-06T07:15:00Z",
  "note": "fr +0.1200%"
}]}
```
直接映射 state.json 的 open;時間由 epoch 分鐘轉 ISO UTC。

### GET /bot/performance?scope=today|all
```json
{"scope": "today", "closed": 5, "net_usdt": 2.31,
 "by_strategy": {"E": {"n": 2, "wins": 1, "pnl_usdt": 1.1},
                 "F": {"n": 3, "wins": 3, "pnl_usdt": 1.21}},
 "pending_reconcile": 0}
```

### POST /bot/pause | /bot/kill
body `{"reason": "influx 資料斷流 12 分鐘"}` → 寫入 flag 檔內容。
回 `{"ok": true, "flag": "data/paused.flag"}`。冪等:已存在則覆寫原因。
每次呼叫經 reporter 發 `bot.control` 事件(severity=crit for kill)。

### POST /bot/resume
body 必帶 `{"confirmed_by": "<使用者名>"}`,缺了回 403。
只刪 paused.flag;**kill.flag 不提供 API 解除**,必須人上主機刪
(kill 是最後防線,解除成本就該高)。

## Hermes 端 skill 行為

- 問「bot 現在怎樣/持倉/今天賺多少」→ 對應 GET,LLM 把 JSON 組織成中文回覆
- 巡檢(health_monitor 級別的資料鏈斷裂、或 anomaly 訊號)判定危險 →
  自主 POST /bot/pause 並推播原因
- 使用者說「恢復」→ Hermes 先 GET /bot/status 確認 safety_reason 已消失,
  再帶 confirmed_by 呼叫 resume;若風控閘門仍在觸發,拒絕並說明
- 永遠不代使用者決定 kill 的解除

## 實作順序建議

1. FastAPI 加三個 GET(純讀檔,零風險)
2. Jetson Hermes skill 接查詢,Discord 能問答
3. 控制類 POST + token + 審計 log + reporter 事件
4. Hermes 巡檢自主 pause(最後,觀察期後才開)
