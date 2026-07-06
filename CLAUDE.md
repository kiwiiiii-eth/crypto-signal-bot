# CLAUDE.md — crypto-signal-bot

## 這個 repo 是什麼

Bitget USDT-M 合約的逆勢訊號交易 bot。**目前以真錢實盤運行**(Server A,
systemd,EXECUTION_MODE=live,小額本金)。同時是 Hermes Financial Agent
生態的「執行層」:Hermes(Jetson 上的 LLM agent)只做唯讀分析與問答,
本 repo 才有下單能力,兩者**嚴格隔離**。

策略與參數全部經歷史重放回測定案(config.py 內每個數字旁的註解就是回測依據)。

## 絕對禁止(除非使用者明確逐項要求)

1. **不改任何策略參數**——config.py 的閾值、持有時間、冷卻、停損水位、
   槓桿、倉位上限。註解裡的回測結論就是這些數字的存在理由。
2. **不把 EXECUTION_MODE 改成 live**,也不在文件/腳本/範例中預設 live。
   模式只由 Server A 的 .env 控制,由使用者本人設定。
3. **不加停利、不收緊停損**。回測已證明固定持有時間 + 寬災難停損(-8%)
   優於緊停損(-3% 會砍掉會回來的單,E 贏單 P90 逆行達 7.2%)。
4. **不提交任何密鑰**。BITGET_API_KEY/SECRET/PASSPHRASE、INFLUXDB_TOKEN、
   TELEGRAM_BOT_TOKEN、DISCORD_WEBHOOK_URL 一律走 .env(已在 .gitignore)。
   push 前掃一次 diff。此 repo 曾有 config.py 外洩 token 的前科。
5. **不給 Hermes 任何下單/改參數的介面**。Hermes 對本 bot 只有:查狀態、
   查持倉、查訊號、暫停、恢復、kill-switch(見 docs/HERMES_SKILL_SPEC.md)。
   暫停/kill 是「只能讓 bot 更保守」的單向控制,永遠允許;反向(開倉、
   加倉、調參)永遠不允許。
6. **Agent 不直接 SSH/exec 進實盤主機**。需要在 Server A 執行的指令,
   輸出給使用者用 `! <command>` 自行執行。

## 實盤安全規則(程式現況,修改前必讀)

- 三種模式:`paper`(純模擬,預設)/ `demo`(Bitget 模擬盤,僅 3 幣對,
  只能驗證下單鏈)/ `live`(真錢)。
- 風控閘門 `safety_reason()`:日虧上限、日停損筆數、權益地板、
  `data/paused.flag`、`data/kill.flag`。觸發後**停開新倉,既有倉位仍照規則出場**。
- 開倉純 maker(post-only 追掛,掛不到就棄單,絕不吃市價);平倉 maker
  優先、用盡輪數才市價兜底;交易所端掛 presetStopLossPrice 災難停損,
  程式端平倉冪等。
- 重啟必對帳(`_reconcile_startup`):停機期間被交易所平掉的倉補結算;
  交易所有非帳本倉位只告警、不動它。
- 帳本只在主執行緒修改;下單走 worker thread,結果經 `_execq` 回主迴圈。
  改 engine.py 時必須維持這個約束。
- `data/state.json` 是唯一持久化狀態,格式改動必須向後相容
  (restore() 對缺欄位要有預設值)。

## 命名對照(歷史包袱,先讀再改)

類別名(開發期編號)與對外策略代號**不同**:

| 類別/設定 | 對外代號 `Signal.strategy` | 內容 |
|---|---|---|
| `StrategyA` / `StrategyACfg` / `cfg.a` | **E** | OI減+價漲→做空 |
| `StrategyB` / `StrategyBCfg` / `cfg.b` | **F** | 極端正費率→做空收 carry |
| `StrategyD` / `StrategyDCfg` / `cfg.d` | **G** | OI增+價跌→做多(E 鏡像) |
| `StrategyC` / `StrategyCCfg` / `cfg.c` | **C** | 1h 異動逆勢(預設關) |

engine.py 裡 `sig.strategy == "E"` 之類的判斷用的是**對外代號**;
環境變數(`A_HOLD_MIN`、`D_MIN_OI_USD`)用的是**類別編號**。改碼時別混。
Telegram、state.json、日報、回測輸出全部用對外代號。

## Claude 開發流程

1. 動手前先讀 README、本檔、docs/ARCHITECTURE.md;動 engine/executor
   前把該檔全文讀完。
2. 文件與註解優先;程式修改以「不改變交易行為」為預設邊界。
3. 任何修改跑 `uv run --with pytest --no-project -- pytest tests/ -q`
   (repo 本身無 venv;Server A 上才有 `venv/`)。
4. 部署流程:本機改 → 測試 → commit → push(master 分支)→ 使用者在
   Server A `git pull` + 重啟 systemd。Server A 是唯讀 deploy key,
   不會從那邊 push。
5. 涉及實盤行為的變更,在 commit message 與回覆中明確標注
   「不影響交易行為」或列出影響面,交使用者判斷何時重啟。

## 相關文件

- `docs/ARCHITECTURE.md` — 系統流程與模組職責
- `docs/HERMES_SKILL_SPEC.md` — Hermes 呼叫本 bot 的介面規格
- `reporter/` — 結構化事件投遞層(health_monitor 已接,bot 主流程尚未)
