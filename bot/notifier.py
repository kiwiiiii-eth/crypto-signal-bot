"""Telegram 推播。格式模仿 zeabur-fastapi-binanceTG：Markdown、反引號可複製、K/M/B 縮寫、分 topic。
Paper/重放模式下 dry_run=True，訊息只回傳字串不真的送出。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import urllib.parse
import urllib.request

from .config import TelegramCfg
from .ledger import Position
from .strategies import Signal

TPE = timezone(timedelta(hours=8))

STRAT_LABEL = {"E": "OI減價漲", "F": "極端正費率", "C": "1h異動逆勢", "G": "OI增價跌",
               "H": "擠多頂空", "L": "強平反抽"}


def _fmt_ts(minute: int) -> str:
    return datetime.fromtimestamp(minute * 60, tz=TPE).strftime("%Y-%m-%d %H:%M")


def format_signal(sig: Signal, flagship: bool = False, extra: str = "") -> str:
    side = "做空" if sig.side < 0 else "做多"
    emoji = "🔻" if sig.side < 0 else "🔺"
    head = f"{emoji} *{sig.strategy}｜{STRAT_LABEL[sig.strategy]} {side}*"
    if flagship:
        head = "🏴 *旗艦訊號（A+B 同時觸發）*\n" + head
    body = (
        f"{head}\n\n"
        f"交易對: `{sig.symbol}`\n"
        f"進場價: `{sig.price:.6g}`\n"
        f"預定出場: {sig.hold_min} 分鐘後（固定時間平倉）\n"
        f"觸發: {sig.note}\n"
    )
    if extra:
        body += f"{extra}\n"
    return body + f"🕒 {_fmt_ts(sig.minute)} (Asia/Taipei)"


def format_close(pos: Position, day_pnl_usdt: float) -> str:
    emoji = "✅" if (pos.pnl_bps or 0) > 0 else "❌"
    reason = {"time": "到時平倉", "stop": "災難停損",
              "liq": "強平(交易所價)"}.get(pos.exit_reason, pos.exit_reason)
    return (
        f"{emoji} *平倉回報｜{pos.signal.strategy}*\n\n"
        f"交易對: `{pos.signal.symbol}`\n"
        f"進場: `{pos.entry_price:.6g}` → 出場: `{pos.exit_price:.6g}`（{reason}）\n"
        f"淨損益: `{pos.pnl_bps:+.1f} bps` / `{pos.pnl_usdt:+.2f} USDT`\n"
        f"當日累計: `{day_pnl_usdt:+.2f} USDT`\n"
        f"🕒 {_fmt_ts(pos.exit_minute)} (Asia/Taipei)"
    )


class Notifier:
    def __init__(self, cfg: TelegramCfg, dry_run: bool = True):
        self.cfg = cfg
        self.dry_run = dry_run
        self.sent: list[str] = []

    def send(self, text: str, thread_id: str = "") -> None:
        self.sent.append(text)
        if self.dry_run or not self.cfg.token:
            return
        payload = {"chat_id": self.cfg.chat_id, "text": text, "parse_mode": "Markdown"}
        if thread_id:
            payload["message_thread_id"] = thread_id
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.cfg.token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10)

    def signal(self, sig: Signal, flagship: bool = False, extra: str = "") -> None:
        self.send(format_signal(sig, flagship, extra), self.cfg.thread_signals)

    def close(self, pos: Position, day_pnl_usdt: float) -> None:
        self.send(format_close(pos, day_pnl_usdt), self.cfg.thread_fills)
