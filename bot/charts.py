"""持倉走勢圖: Binance 1m/5m K 線 + 進場價/停損線/現價標註, 推播用 PNG。"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

TPE = timezone(timedelta(hours=8))
RED = "#EF5350"
GREEN = "#26A69A"
MA5_COLOR = "#FFD93D"
MA20_COLOR = "#5C9BD1"
MA60_COLOR = "#A78BFA"
MA120_COLOR = "#F472B6"
BG = "#1e1e1e"
PANEL_BG = "#070707"
GRID = "#3b3b3b"
PRICE_COLOR = "#00BFA5"
OI_COLOR = "#E9C46A"
RATE_COLOR = "#9CC9BE"
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


def trend_chart(symbol: str, out_dir: str = "/tmp") -> str:
    """4H/1D K線 + MA5/20/60/120 趨勢圖。"""
    import matplotlib.patches as patches
    from .trend import _sma, binance_klines

    fig, axes = plt.subplots(2, 1, figsize=(11, 8), dpi=140, sharex=False)
    fig.patch.set_facecolor(BG)
    for ax, interval in zip(axes, ("4h", "1d")):
        ks = binance_klines(symbol, interval, limit=180)
        ts = [datetime.fromtimestamp(k["time"] / 1000, tz=TPE) for k in ks]
        close = [k["close"] for k in ks]
        xs = list(range(len(ks)))
        width = 0.62
        for x, k in zip(xs, ks):
            o, h, l, c = k["open"], k["high"], k["low"], k["close"]
            col = RED if c >= o else GREEN
            ax.plot([x, x], [l, h], color=col, linewidth=0.8, zorder=2)
            body_bot = min(o, c)
            body_h = max(abs(c - o), c * 0.001)
            ax.add_patch(patches.Rectangle((x - width / 2, body_bot), width, body_h,
                                           linewidth=0, facecolor=col, zorder=3))
        for window, color in ((5, MA5_COLOR), (20, MA20_COLOR),
                              (60, MA60_COLOR), (120, MA120_COLOR)):
            ma = _sma(close, window)
            pts = [(i, v) for i, v in enumerate(ma) if v is not None]
            if pts:
                ax.plot([i for i, _ in pts], [v for _, v in pts],
                        color=color, lw=1.15, label=f"MA{window}", zorder=4)
        ax_vol = ax.twinx()
        vols = [k["volume"] for k in ks]
        max_vol = max(vols) if vols else 1
        for x, k in zip(xs, ks):
            col = RED if k["close"] >= k["open"] else GREEN
            ax_vol.bar(x, k["volume"], color=col, width=width, alpha=0.22, zorder=1)
        ax_vol.set_ylim(0, max_vol * 4)
        ax_vol.tick_params(axis="y", labelsize=7, colors="#666666")
        ax_vol.set_facecolor(BG)
        ax_vol.set_zorder(ax.get_zorder() - 1)
        ax.set_facecolor("none")
        ax.set_title(f"{symbol} {interval}", fontsize=12, pad=8)
        ax.legend(loc="best", fontsize=8, framealpha=0.25)
        ax.grid(True, alpha=0.15, color="gray", linewidth=0.5)
        ax.set_xlim(-0.7, len(xs) - 0.3)
        step = max(1, len(xs) // 8)
        ax.set_xticks(xs[::step])
        ax.set_xticklabels([ts[i].strftime("%m-%d") for i in xs[::step]],
                           fontsize=8, color="white", rotation=0)
    fig.text(0.98, 0.02, "crypto_hunter_kiwi", fontsize=16, color="white",
             alpha=0.25, ha="right", va="bottom", weight="bold")
    fig.tight_layout()
    out = f"{out_dir}/trend_{symbol}_{uuid.uuid4().hex[:6]}.png"
    fig.savefig(out)
    plt.close(fig)
    return out


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _duration_to_window(duration: str) -> str:
    d = duration.lower().strip()
    if d.endswith("h"):
        hours = int(d[:-1])
        return "1m" if hours <= 30 else "5m"
    if d.endswith("d"):
        days = int(d[:-1])
        return "5m" if days <= 3 else "15m"
    return "5m"


def _normalize_duration(duration: str) -> str:
    d = (duration or "24h").lower().strip()
    aliases = {"1d": "24h", "day": "24h", "24": "24h", "3day": "3d", "3days": "3d"}
    d = aliases.get(d, d)
    if not d.endswith(("h", "d")):
        raise ValueError("週期請用 24h 或 3d")
    n = int(d[:-1])
    if n <= 0:
        raise ValueError("週期必須大於 0")
    return d


def _query_market_frame(symbol: str, duration: str, influx: dict[str, Any], exchange: str):
    import pandas as pd
    from influxdb_client import InfluxDBClient

    token = influx.get("token") or ""
    if not token:
        raise ValueError("INFLUXDB_TOKEN 未設定")
    bucket = influx.get("bucket") or "exchange"
    org = influx.get("org") or "crypto"
    url = influx.get("url") or "http://localhost:8086"
    window = _duration_to_window(duration)
    flux = f'''
from(bucket: {_flux_string(bucket)})
  |> range(start: -{duration})
  |> filter(fn: (r) => r._measurement == "crypto_futures")
  |> filter(fn: (r) => r.exchange == {_flux_string(exchange)})
  |> filter(fn: (r) => r.symbol == {_flux_string(symbol)})
  |> filter(fn: (r) => r._field == "mark_price" or r._field == "last_price" or r._field == "price" or r._field == "open_interest" or r._field == "funding_rate")
  |> aggregateWindow(every: {window}, fn: last, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "mark_price", "last_price", "price", "open_interest", "funding_rate"])
'''
    with InfluxDBClient(url=url, token=token, org=org, timeout=120_000, enable_gzip=True) as client:
        df = client.query_api().query_data_frame(flux)
    if isinstance(df, list):
        df = pd.concat([x for x in df if not x.empty], ignore_index=True) if df else pd.DataFrame()
    if df.empty:
        return df
    for col in ("mark_price", "last_price", "price", "open_interest", "funding_rate"):
        if col not in df.columns:
            df[col] = pd.NA
    price_cols = [c for c in ("mark_price", "last_price", "price") if df[c].notna().any()]
    df["close"] = df[price_cols].bfill(axis=1).iloc[:, 0] if price_cols else pd.NA
    df = df.rename(columns={"_time": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(TPE)
    df = df[["time", "close", "open_interest", "funding_rate"]].dropna(subset=["time", "close"])
    df = df.sort_values("time").drop_duplicates(subset=["time"])
    df["open_interest"] = df["open_interest"].ffill()
    df["funding_rate"] = df["funding_rate"].ffill()
    return df.dropna(subset=["open_interest", "funding_rate"], how="all")


def market_structure_chart(symbol: str, duration: str = "24h", influx: dict[str, Any] | None = None,
                           exchange: str = "binance", out_dir: str = "/tmp") -> tuple[str, dict]:
    """InfluxDB Price/OI/Funding 結構圖，回傳 (PNG 路徑, 摘要)。"""
    import pandas as pd

    duration = _normalize_duration(duration)
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    df = _query_market_frame(symbol, duration, influx or {}, exchange.lower())
    if df.empty or len(df) < 5:
        raise ValueError(f"{symbol} {duration} 可用資料不足")

    df = df.copy()
    for window in (5, 20, 60):
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=max(2, window // 3)).mean()
    first_px, last_px = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    first_oi = float(df["open_interest"].dropna().iloc[0]) if df["open_interest"].notna().any() else None
    last_oi = float(df["open_interest"].dropna().iloc[-1]) if df["open_interest"].notna().any() else None
    last_fr = float(df["funding_rate"].dropna().iloc[-1]) if df["funding_rate"].notna().any() else None
    px_chg = (last_px / first_px - 1) * 100 if first_px else 0.0
    oi_chg = (last_oi / first_oi - 1) * 100 if first_oi and last_oi else None
    ma5, ma20, ma60 = (df["ma5"].iloc[-1], df["ma20"].iloc[-1], df["ma60"].iloc[-1])
    bull_ma = pd.notna(ma5) and pd.notna(ma20) and pd.notna(ma60) and last_px > ma5 > ma20 > ma60
    ma20_up = pd.notna(df["ma20"].iloc[-1]) and pd.notna(df["ma20"].iloc[-4]) and df["ma20"].iloc[-1] > df["ma20"].iloc[-4]
    oi_up = oi_chg is not None and oi_chg > 0
    funding_cool = last_fr is not None and last_fr <= 0
    state = "偏多堆疊" if bull_ma and ma20_up and oi_up and funding_cool else (
        "趨勢走強" if (last_px > ma20 if pd.notna(ma20) else False) and ma20_up and oi_up else "觀察中")

    fig, axes = plt.subplots(3, 1, figsize=(11.2, 7.3), dpi=150, sharex=True,
                             gridspec_kw={"height_ratios": [1, 1, 1], "hspace": 0.22})
    fig.patch.set_facecolor(BG)
    ax_price, ax_oi, ax_fr = axes
    for ax in axes:
        ax.set_facecolor(PANEL_BG)
        ax.grid(True, alpha=0.35, color=GRID, linewidth=0.5)
        ax.tick_params(colors="#cfcfcf", labelsize=7, length=3)
        for spine in ax.spines.values():
            spine.set_color("#9a9a9a")
            spine.set_linewidth(0.8)

    ax_price.plot(df["time"], df["close"], color=PRICE_COLOR, lw=1.45, label="Price", zorder=5)
    for name, color in (("ma5", MA5_COLOR), ("ma20", MA20_COLOR), ("ma60", MA60_COLOR)):
        ax_price.plot(df["time"], df[name], color=color, lw=1.05,
                      alpha=0.55, label=name.upper(), zorder=4)
    ax_price.set_title(f"{symbol} Price", fontsize=8, color="#dcdcdc", pad=4)
    ax_price.text(0.01, 0.88, f"{px_chg:+.2f}%", transform=ax_price.transAxes,
                  color=GREEN if px_chg >= 0 else RED, fontsize=7, weight="bold",
                  ha="left", va="top")

    if df["open_interest"].notna().any():
        ax_oi.plot(df["time"], df["open_interest"], color=OI_COLOR, lw=1.45)
    ax_oi.set_title("OI", fontsize=8, color="#dcdcdc", pad=4)
    if oi_chg is not None:
        ax_oi.text(0.01, 0.88, f"{oi_chg:+.2f}%", transform=ax_oi.transAxes,
                   color=GREEN if oi_chg >= 0 else RED, fontsize=7, weight="bold",
                   ha="left", va="top")

    if df["funding_rate"].notna().any():
        fr_pct = df["funding_rate"] * 100
        avg_fr_pct = float(fr_pct.dropna().mean())
        ax_fr.plot(df["time"], fr_pct, color=RATE_COLOR, lw=1.35)
        ax_fr.axhline(avg_fr_pct, color="#e8e8e8", lw=0.7, ls="--", alpha=0.45)
        ax_fr.text(0.01, 0.88, f"Avg: {avg_fr_pct:+.3f}%", transform=ax_fr.transAxes,
                   color="#d8d8d8", fontsize=7, ha="left", va="top")
    ax_fr.set_title("Rate", fontsize=8, color="#dcdcdc", pad=4)
    ax_fr.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.3f}%"))
    ax_fr.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if duration.endswith("h") else "%m-%d %H:%M", tz=TPE))
    ax_fr.set_xlabel("Time (UTC+8)", color="#bfbfbf", fontsize=7)

    fig.suptitle(f"{symbol} {duration.upper()}｜{state}", fontsize=9, color="#e5e5e5", y=0.985)
    fig.text(0.98, 0.02, "crypto_hunter_kiwi", fontsize=16, color="white",
             alpha=0.16, ha="right", va="bottom", weight="bold")
    fig.autofmt_xdate(rotation=35)
    fig.subplots_adjust(left=0.07, right=0.985, top=0.92, bottom=0.13, hspace=0.34)
    out = f"{out_dir}/market_{symbol}_{duration}_{uuid.uuid4().hex[:6]}.png"
    fig.savefig(out)
    plt.close(fig)
    summary = {"symbol": symbol, "duration": duration, "points": len(df), "state": state,
               "price_chg_pct": px_chg, "oi_chg_pct": oi_chg, "funding_bps": last_fr * 10000 if last_fr is not None else None}
    return out, summary


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
