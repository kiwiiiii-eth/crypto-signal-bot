"""持倉走勢圖: Binance 1m/5m K 線 + 進場價/停損線/現價標註, 推播用 PNG。"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

TPE = timezone(timedelta(hours=8))
FONT = Path(__file__).resolve().parent.parent / "data" / "fonts" / "ArialUnicode.ttf"
if FONT.exists():
    fm.fontManager.addfont(str(FONT))
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.style.use("dark_background")


def _klines(symbol: str, start_min: int, interval: str = "1m") -> list:
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
           f"&interval={interval}&startTime={start_min * 60000}&limit=1500")
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def position_chart(pos, stop_bps: float, out_dir: str = "/tmp") -> str | None:
    """回傳 PNG 路徑; K 線抓不到回 None。pos 為 ledger.Position。"""
    sym = pos.signal.symbol
    held_min = int(time.time() // 60) - pos.entry_minute
    interval = "1m" if held_min <= 720 else "5m"
    try:
        ks = _klines(sym, pos.entry_minute - 60, interval)  # 進場前 1h 也畫進去
    except Exception:
        return None
    if not ks:
        return None
    ts = [datetime.fromtimestamp(k[0] / 1000, tz=TPE) for k in ks]
    close = [float(k[4]) for k in ks]

    side = pos.signal.side
    stop_px = pos.entry_price * (1 - side * stop_bps / 10000)
    cur = close[-1]
    upnl = pos.notional_usdt * (cur / pos.entry_price - 1) * side
    upnl_bps = (cur / pos.entry_price - 1) * 10000 * side
    dir_txt = "空" if side < 0 else "多"
    color = "#7CFC90" if upnl >= 0 else "#ff5566"

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    ax.plot(ts, close, color="#00d4ff", lw=1.5)
    entry_t = datetime.fromtimestamp(pos.entry_minute * 60, tz=TPE)
    ax.axvline(entry_t, color="white", lw=0.8, ls=":", alpha=0.6)
    ax.axhline(pos.entry_price, color="white", lw=1.0, ls="--", alpha=0.8,
               label=f"進場 {pos.entry_price:.6g}")
    ax.axhline(stop_px, color="#ff5566", lw=1.0, ls="--", alpha=0.8,
               label=f"停損 {stop_px:.6g} (-{stop_bps / 100:.0f}%)")
    ax.scatter([ts[-1]], [cur], color=color, s=45, zorder=5)
    ax.annotate(f"{cur:.6g}\n{upnl:+.2f}U ({upnl_bps:+.0f}bps)",
                (ts[-1], cur), textcoords="offset points", xytext=(8, 0),
                fontsize=10, color=color, weight="bold", va="center")
    exit_t = datetime.fromtimestamp(pos.exit_due * 60, tz=TPE)
    ax.set_title(f"{sym}｜{pos.signal.strategy} {dir_txt}｜{pos.notional_usdt:.0f}U 名目｜"
                 f"預定出場 {exit_t:%H:%M}", fontsize=12, pad=10)
    ax.legend(loc="best", fontsize=9, framealpha=0.3)
    ax.grid(alpha=0.15)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TPE))
    ax.margins(x=0.02)
    fig.text(0.98, 0.02, "crypto_hunter_kiwi", fontsize=16, color="white",
             alpha=0.25, ha="right", va="bottom", weight="bold")
    fig.tight_layout()
    out = f"{out_dir}/pos_{sym}_{uuid.uuid4().hex[:6]}.png"
    fig.savefig(out)
    plt.close(fig)
    return out


def send_photo(token: str, chat_id: str, path: str, caption: str = "",
               thread_id=None) -> None:
    boundary = uuid.uuid4().hex
    fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "Markdown"}
    if thread_id:
        fields["message_thread_id"] = str(thread_id)
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f"filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body += Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=30)
