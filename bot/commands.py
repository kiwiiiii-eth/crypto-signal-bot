"""Telegram 指令查詢（getUpdates 長輪詢，跑在背景 thread）。

指令:
  /status    — 運行狀態、universe、槓桿設定
  /positions — 目前倉位與未實現損益
  /pnl       — 已實現損益（今日/累計）
  /equity    — 總權益 = 初始 + 已實現 + 未實現
  /help
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))


class CommandServer:
    def __init__(self, token: str, chat_id: str, engine):
        self.token = token
        self.chat_id = str(chat_id)
        self.engine = engine  # LiveEngine，讀取 ledger/feed/cfg
        self.offset = 0
        self.started = time.time()

    def _api(self, method: str, **params):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read())

    def _reply(self, text: str, thread_id=None):
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        if thread_id:
            params["message_thread_id"] = thread_id
        try:
            self._api("sendMessage", **params)
        except Exception:
            pass

    # ---- 指令實作 ----
    def cmd_status(self) -> str:
        e = self.engine
        up = (time.time() - self.started) / 3600
        return (f"🤖 *Demo 模擬盤運行中*\n\n"
                f"運行時間: `{up:.1f} h`\n"
                f"監控幣數: `{len(e.feed.universe)}`\n"
                f"最後 tick: {datetime.fromtimestamp((e.feed.last_minute or 0)*60, tz=TPE):%H:%M}\n"
                f"單筆: `{e.cfg.risk.margin_usdt:.0f}U × {e.cfg.risk.leverage:.0f}x = "
                f"{e.cfg.risk.margin_usdt*e.cfg.risk.leverage:.0f}U 名目`\n"
                f"停損: `-{e.cfg.risk.disaster_stop_bps/100:.0f}%`｜出場: 固定時間 (A 1h / B 4h)\n"
                f"倉位: `{len(e.ledger.open_positions)}` 開放｜累計平倉 `{len(e.ledger.closed)}`")

    def cmd_positions(self) -> str:
        e = self.engine
        if not e.ledger.open_positions:
            return "📭 目前無持倉"
        prices = {p.signal.symbol: e.feed.price(p.signal.symbol) for p in e.ledger.open_positions}
        lines = ["📊 *目前倉位*\n"]
        now_min = int(time.time() // 60)
        for p in e.ledger.open_positions:
            px = prices.get(p.signal.symbol)
            upnl = p.notional_usdt * (px / p.entry_price - 1) * p.signal.side if px else float("nan")
            side = "空" if p.signal.side < 0 else "多"
            left = max(0, p.exit_due - now_min)
            lines.append(f"`{p.signal.symbol}` {p.signal.strategy}/{side} "
                         f"進場 `{p.entry_price:.6g}` 現價 `{(px or 0):.6g}` "
                         f"未實現 `{upnl:+.2f}U` 剩 `{left}m`")
        total = e.ledger.unrealized({k: v for k, v in prices.items() if v})
        lines.append(f"\n合計未實現: `{total:+.2f} USDT`")
        return "\n".join(lines)

    def cmd_pnl(self) -> str:
        e = self.engine
        today = datetime.now(TPE).date()
        realized_today = sum(p.pnl_usdt or 0 for p in e.ledger.closed
                             if p.exit_minute and
                             datetime.fromtimestamp(p.exit_minute * 60, tz=TPE).date() == today)
        n_today = sum(1 for p in e.ledger.closed if p.exit_minute and
                      datetime.fromtimestamp(p.exit_minute * 60, tz=TPE).date() == today)
        wins = sum(1 for p in e.ledger.closed if (p.pnl_bps or 0) > 0)
        n = len(e.ledger.closed)
        return (f"💰 *已實現損益*\n\n"
                f"今日: `{realized_today:+.2f} USDT`（{n_today} 單）\n"
                f"累計: `{e.ledger.total_pnl_usdt:+.2f} USDT`（{n} 單，勝率 "
                f"{wins/n*100 if n else 0:.0f}%）")

    def cmd_equity(self) -> str:
        e = self.engine
        prices = {p.signal.symbol: e.feed.price(p.signal.symbol)
                  for p in e.ledger.open_positions}
        upnl = e.ledger.unrealized({k: v for k, v in prices.items() if v})
        eq = e.cfg.risk.equity_usdt + e.ledger.total_pnl_usdt + upnl
        margin_used = len(e.ledger.open_positions) * e.cfg.risk.margin_usdt
        return (f"🏦 *總權益*\n\n"
                f"初始本金: `{e.cfg.risk.equity_usdt:.2f}`\n"
                f"已實現: `{e.ledger.total_pnl_usdt:+.2f}`\n"
                f"未實現: `{upnl:+.2f}`\n"
                f"*權益: `{eq:.2f} USDT`（{(eq/e.cfg.risk.equity_usdt-1)*100:+.2f}%）*\n"
                f"保證金占用: `{margin_used:.0f}` / 可用 `{eq-margin_used:.2f}`")

    HELP = ("📖 指令:\n/status 運行狀態\n/positions 目前倉位\n"
            "/pnl 已實現損益\n/equity 總權益\n/help 本說明")

    def handle(self, text: str) -> str | None:
        cmd = text.split("@")[0].split()[0].lower() if text else ""
        return {
            "/status": self.cmd_status, "/positions": self.cmd_positions,
            "/pnl": self.cmd_pnl, "/equity": self.cmd_equity,
            "/help": lambda: self.HELP, "/start": lambda: self.HELP,
        }.get(cmd, lambda: None)()

    def _loop(self):
        while True:
            try:
                upd = self._api("getUpdates", offset=self.offset, timeout=30)
                for u in upd.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    reply = self.handle(msg.get("text", ""))
                    if reply:
                        self._reply(reply, msg.get("message_thread_id"))
            except Exception:
                time.sleep(5)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="tg-commands").start()
