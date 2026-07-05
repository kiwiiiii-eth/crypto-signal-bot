#!/usr/bin/env python3
"""Write low-frequency MA regime points to InfluxDB.

Measurement: crypto_ma
Tags: exchange, symbol, interval
Fields: OHLCV, MA5/10/20/60/120, slopes, trend flags

This intentionally writes only the latest closed candle for each interval. Re-running
within the same candle overwrites the same timestamp instead of growing data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BINANCE = "https://fapi.binance.com"
BITGET = "https://api.bitget.com"
WINDOWS = (5, 10, 20, 60, 120)
DEFAULT_INTERVALS = ("5m", "15m", "1h", "4h", "1d")
BITGET_GRANULARITY = {"1h": "1H", "4h": "4H", "1d": "1D"}
SYMBOLS_CACHE = DATA / "ma_symbols_common.json"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_json(url: str, timeout: int = 20, retries: int = 2):
    """GET with backoff on 429 so one rate-limit blip doesn't drop the symbol."""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-signal-bot"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise


def sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= window:
            acc -= values[i - window]
        out.append(acc / window if i >= window - 1 else None)
    return out


def bitget_symbols() -> set[str]:
    p = DATA / "bitget_symbols.json"
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def binance_usdt_perps() -> set[str]:
    info = get_json(f"{BINANCE}/fapi/v1/exchangeInfo", timeout=30)
    return {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"}


def universe(limit: int = 0, exchange: str = "bitget") -> list[str]:
    """Only write Binance ∩ Bitget USDT perpetual symbols.

    Signal source and execution venue must both have the symbol. If Binance is
    temporarily blocked, reuse the last common-symbol cache instead of expanding
    to Bitget-only names.
    """
    bg = bitget_symbols()
    try:
        syms = binance_usdt_perps()
        if bg:
            syms &= bg
        syms = {s for s in syms if not s.endswith("USDC")}
        out = sorted(syms)
        if out:
            SYMBOLS_CACHE.write_text(json.dumps(out))
    except Exception as e:
        if SYMBOLS_CACHE.exists():
            print(f"warn: Binance symbols unavailable ({e}); using cached common symbols",
                  file=sys.stderr)
            out = json.loads(SYMBOLS_CACHE.read_text())
        else:
            raise
    return out[:limit] if limit > 0 else out


def binance_klines(symbol: str, interval: str, limit: int = 180) -> list[dict]:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = get_json(f"{BINANCE}/fapi/v1/klines?{q}")
    now_ms = int(time.time() * 1000)
    rows = []
    for k in raw:
        close_time = int(k[6])
        if close_time > now_ms:
            continue
        rows.append({
            "open_time": int(k[0]), "close_time": close_time,
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "quote_volume": float(k[7]), "trades": int(k[8]),
        })
    return rows


def bitget_klines(symbol: str, interval: str, limit: int = 180) -> list[dict]:
    granularity = BITGET_GRANULARITY.get(interval, interval)
    q = urllib.parse.urlencode({
        "symbol": symbol, "productType": "USDT-FUTURES",
        "granularity": granularity, "limit": limit,
    })
    raw = get_json(f"{BITGET}/api/v2/mix/market/candles?{q}")
    now_ms = int(time.time() * 1000)
    rows = []
    for k in raw.get("data", []):
        open_time = int(k[0])
        close_time = open_time + interval_ms(interval) - 1
        if close_time > now_ms:
            continue
        rows.append({
            "open_time": open_time,
            "close_time": close_time,
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "quote_volume": float(k[6]) if len(k) > 6 else 0.0,
            "trades": 0,
        })
    rows.sort(key=lambda r: r["open_time"])
    return rows


def interval_ms(interval: str) -> int:
    table = {"5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000,
             "4h": 4 * 60 * 60_000, "1d": 24 * 60 * 60_000}
    return table[interval]


def klines(symbol: str, interval: str, exchange: str, limit: int = 180) -> list[dict]:
    if exchange == "bitget":
        return bitget_klines(symbol, interval, limit)
    return binance_klines(symbol, interval, limit)


def trend_fields(rows: list[dict]) -> dict:
    closes = [r["close"] for r in rows]
    if len(closes) < 60:
        raise ValueError("not enough candles")
    last = rows[-1]
    fields = {
        "open": last["open"], "high": last["high"], "low": last["low"],
        "close": last["close"], "volume": last["volume"],
        "quote_volume": last["quote_volume"], "trades": last["trades"],
    }
    mas: dict[int, list[float | None]] = {}
    for w in WINDOWS:
        m = sma(closes, w)
        mas[w] = m
        fields[f"ma{w}"] = float(m[-1]) if m[-1] is not None else 0.0
        fields[f"slope_ma{w}_bps"] = (
            float((m[-1] / m[-4] - 1) * 10000)
            if m[-1] is not None and m[-4] not in {None, 0} else 0.0
        )
    c = last["close"]
    ma5, ma20, ma60, ma120 = fields["ma5"], fields["ma20"], fields["ma60"], fields["ma120"]
    fields["short_bull"] = c > ma5 > ma20 and fields["slope_ma5_bps"] > 0
    fields["mid_bull"] = c > ma5 > ma20 > ma60 and fields["slope_ma20_bps"] >= 0
    fields["full_bull"] = ma120 > 0 and c > ma5 > ma20 > ma60 > ma120 and fields["slope_ma60_bps"] >= 0
    fields["boxed_up"] = c > ma20 and ma5 > ma20 and c > ma60 and fields["slope_ma20_bps"] >= 0
    fields["trend_score"] = int(fields["short_bull"]) + int(fields["mid_bull"]) + int(fields["full_bull"])
    return fields


def build_point(symbol: str, interval: str, exchange: str = "bitget"):
    from influxdb_client import Point, WritePrecision

    rows = klines(symbol, interval, exchange)
    fields = trend_fields(rows)
    p = Point("crypto_ma").tag("exchange", exchange).tag("symbol", symbol).tag("interval", interval)
    for k, v in fields.items():
        p.field(k, v)
    return p.time(datetime.fromtimestamp(rows[-1]["close_time"] / 1000, tz=timezone.utc),
                  WritePrecision.MS)


def write_points(points, dry_run: bool) -> None:
    if dry_run:
        print(f"dry-run points={len(points)}")
        for p in points[:5]:
            print(p.to_line_protocol())
        return
    token = os.getenv("INFLUXDB_TOKEN", "")
    if not token:
        raise SystemExit("INFLUXDB_TOKEN is required unless --dry-run")
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    url = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    org = os.getenv("INFLUXDB_ORG", "crypto")
    bucket = os.getenv("INFLUXDB_BUCKET", "exchange")
    with InfluxDBClient(url=url, token=token, org=org) as client:
        client.write_api(write_options=SYNCHRONOUS).write(bucket=bucket, org=org, record=points)


def due_intervals(candidates: list[str], now: datetime | None = None) -> list[str]:
    """Keep only intervals whose candle could have closed since the last 5-min run.

    Recomputing 4h/1d MAs every 5 minutes rewrites identical points and burns
    ~70% of the request budget for nothing. UTC-aligned, stateless.
    """
    now = now or datetime.now(timezone.utc)
    due = {
        "5m": True,
        "15m": now.minute % 15 < 5,
        "1h": now.minute < 5,
        "4h": now.hour % 4 == 0 and now.minute < 5,
        "1d": now.hour == 0 and now.minute < 5,
    }
    return [itv for itv in candidates if due.get(itv, True)]


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    ap.add_argument("--all-intervals", action="store_true",
                    help="ignore due-time gating and fetch every interval (backfill)")
    ap.add_argument("--exchange", default=os.getenv("MA_SOURCE_EXCHANGE", "bitget"),
                    choices=("bitget", "binance"))
    ap.add_argument("--symbols", default="", help="comma-separated symbols; default=Binance/Bitget USDT perp intersection")
    ap.add_argument("--limit", type=int, default=int(os.getenv("MA_SYMBOL_LIMIT", "0")))
    ap.add_argument("--workers", type=int, default=int(os.getenv("MA_WORKERS", "2")))
    # 0.05 meant 20 submissions/s — right at Bitget's public rate limit,
    # so any jitter produced 429 storms. 0.12 keeps peak around 8 req/s.
    ap.add_argument("--request-sleep", type=float, default=float(os.getenv("MA_REQUEST_SLEEP", "0.12")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]
    if not args.all_intervals:
        intervals = due_intervals(intervals)
    if not intervals:
        print("crypto_ma nothing due this run")
        return
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or universe(args.limit, args.exchange)
    points, errors = [], 0
    error_kinds: dict[str, int] = {}
    jobs = [(s, itv) for s in symbols for itv in intervals]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {}
        for s, itv in jobs:
            futs[pool.submit(build_point, s, itv, args.exchange)] = (s, itv)
            if args.request_sleep > 0:
                time.sleep(args.request_sleep)
        for fut in as_completed(futs):
            s, itv = futs[fut]
            try:
                points.append(fut.result())
            except Exception as e:
                errors += 1
                kind = f"{type(e).__name__}: {e}"[:60]
                error_kinds[kind] = error_kinds.get(kind, 0) + 1
                if errors <= 12:
                    print(f"skip {s} {itv}: {e}", file=sys.stderr)
    write_points(points, args.dry_run)
    print(f"crypto_ma exchange={args.exchange} wrote={len(points)} errors={errors} "
          f"symbols={len(symbols)} intervals={','.join(intervals)}")
    for kind, count in sorted(error_kinds.items(), key=lambda kv: -kv[1]):
        print(f"  error x{count}: {kind}", file=sys.stderr)


if __name__ == "__main__":
    main()
