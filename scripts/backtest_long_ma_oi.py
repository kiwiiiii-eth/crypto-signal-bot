#!/usr/bin/env python3
"""Backtest long continuation setups from Influx crypto_futures data.

Signal idea:
- open interest is rising
- funding is negative
- moving averages are becoming stronger / stacked upward
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WINDOWS = (5, 20, 60)
DEFAULT_INTERVALS = ("5m", "15m", "1h", "4h")
DEFAULT_SETUPS = {
    "short_bull": "close > ma5 > ma20 and ma5 slope > 0",
    "mid_bull": "close > ma5 > ma20 > ma60 and ma20 slope >= 0",
    "strengthening": "ma5 slope > 0 and ma20 slope > 0 and close > ma20",
}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def common_symbols(limit: int = 0) -> list[str]:
    p = DATA / "ma_symbols_common.json"
    if p.exists():
        syms = json.loads(p.read_text())
    else:
        syms = ["BTCUSDT", "ETHUSDT", "TLMUSDT"]
    syms = sorted(set(syms))
    return syms[:limit] if limit else syms


def symbol_filter(symbols: list[str]) -> str:
    return " or ".join(f'r.symbol == "{s}"' for s in symbols)


def query_interval(symbols: list[str], interval: str, start: str, chunk_size: int) -> pd.DataFrame:
    from influxdb_client import InfluxDBClient

    url = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    org = os.getenv("INFLUXDB_ORG", "crypto")
    bucket = os.getenv("INFLUXDB_BUCKET", "exchange")
    token = os.getenv("INFLUXDB_TOKEN", "")
    if not token:
        raise SystemExit("INFLUXDB_TOKEN is required")

    frames: list[pd.DataFrame] = []
    with InfluxDBClient(url=url, token=token, org=org, timeout=120_000, enable_gzip=True) as client:
        q = client.query_api()
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "crypto_futures")
  |> filter(fn: (r) => r.exchange == "binance")
  |> filter(fn: (r) => r._field == "mark_price" or r._field == "open_interest" or r._field == "funding_rate")
  |> filter(fn: (r) => {symbol_filter(chunk)})
  |> aggregateWindow(every: {interval}, fn: last, createEmpty: false)
  |> pivot(rowKey:["_time", "symbol"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "symbol", "mark_price", "open_interest", "funding_rate"])
'''
            df = q.query_data_frame(flux, org=org)
            if isinstance(df, list):
                df = pd.concat([x for x in df if not x.empty], ignore_index=True) if df else pd.DataFrame()
            if df is not None and not df.empty:
                frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    keep = [c for c in ["_time", "symbol", "mark_price", "open_interest", "funding_rate"] if c in df.columns]
    return df[keep].dropna(subset=["_time", "symbol", "mark_price", "open_interest", "funding_rate"])


def enrich(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"_time": "time", "mark_price": "close"}).copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out = out.sort_values(["symbol", "time"])
    for col in ["close", "open_interest", "funding_rate"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    g = out.groupby("symbol", group_keys=False)
    for w in WINDOWS:
        out[f"ma{w}"] = g["close"].transform(lambda s: s.rolling(w).mean())
        out[f"slope_ma{w}_bps"] = g[f"ma{w}"].transform(lambda s: (s / s.shift(3) - 1) * 10000)
    out["oi_chg_pct"] = g["open_interest"].transform(lambda s: (s / s.shift(1) - 1) * 100)
    out["oi_chg_3_pct"] = g["open_interest"].transform(lambda s: (s / s.shift(3) - 1) * 100)
    out["future_1"] = g["close"].shift(-1) / out["close"] - 1
    out["future_2"] = g["close"].shift(-2) / out["close"] - 1
    out["future_4"] = g["close"].shift(-4) / out["close"] - 1
    out["interval"] = interval
    return out.dropna(subset=["ma20", "future_1", "oi_chg_pct"])


def setup_mask(df: pd.DataFrame, setup: str, oi_min: float, funding_max: float) -> pd.Series:
    base = (df["funding_rate"] < funding_max) & (df["oi_chg_pct"] >= oi_min)
    if setup == "short_bull":
        return base & (df["close"] > df["ma5"]) & (df["ma5"] > df["ma20"]) & (df["slope_ma5_bps"] > 0)
    if setup == "mid_bull":
        return base & (df["close"] > df["ma5"]) & (df["ma5"] > df["ma20"]) & (df["ma20"] > df["ma60"]) & (df["slope_ma20_bps"] >= 0)
    if setup == "strengthening":
        return base & (df["close"] > df["ma20"]) & (df["slope_ma5_bps"] > 0) & (df["slope_ma20_bps"] > 0)
    raise ValueError(setup)


def summarize(trades: pd.DataFrame, hold_col: str, fee_bps: float) -> dict:
    if trades.empty:
        return {}
    ret = trades[hold_col] - fee_bps / 10000
    by_day = ret.groupby(trades["time"].dt.date).sum()
    return {
        "trades": int(len(trades)),
        "symbols": int(trades["symbol"].nunique()),
        "win_rate": float((ret > 0).mean()),
        "avg_bps": float(ret.mean() * 10000),
        "median_bps": float(ret.median() * 10000),
        "sum_bps": float(ret.sum() * 10000),
        "positive_days": int((by_day > 0).sum()),
        "days": int(len(by_day)),
        "day_win_rate": float((by_day > 0).mean()) if len(by_day) else math.nan,
    }


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="-7d")
    ap.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    ap.add_argument("--limit", type=int, default=0, help="limit common-symbol universe for quick tests")
    ap.add_argument("--chunk-size", type=int, default=30)
    ap.add_argument("--fee-bps", type=float, default=12.0, help="round-trip fee/slippage haircut in bps")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    symbols = common_symbols(args.limit)
    rows = []
    best_trades = []
    for interval in [x.strip() for x in args.intervals.split(",") if x.strip()]:
        raw = query_interval(symbols, interval, args.start, args.chunk_size)
        df = enrich(raw, interval)
        print(f"interval={interval} rows={len(df)} symbols={df.symbol.nunique() if not df.empty else 0}")
        if df.empty:
            continue
        for setup in DEFAULT_SETUPS:
            for oi_min in (0.2, 0.5, 1.0, 2.0):
                for funding_max in (0.0, -0.0001, -0.0003):
                    mask = setup_mask(df, setup, oi_min, funding_max)
                    trades = df.loc[mask].copy()
                    for hold_col in ("future_1", "future_2", "future_4"):
                        s = summarize(trades.dropna(subset=[hold_col]), hold_col, args.fee_bps)
                        if not s:
                            continue
                        row = {
                            "interval": interval,
                            "setup": setup,
                            "oi_min_pct": oi_min,
                            "funding_lt": funding_max,
                            "hold": hold_col.replace("future_", "") + " bars",
                            **s,
                        }
                        rows.append(row)
                        if s["trades"] >= 10 and s["win_rate"] >= 0.55 and s["avg_bps"] > 0:
                            sample = trades.assign(setup=setup, oi_min_pct=oi_min, funding_lt=funding_max,
                                                   hold=row["hold"], net_bps=(trades[hold_col] - args.fee_bps / 10000) * 10000)
                            best_trades.append(sample[["time", "symbol", "interval", "setup", "hold", "close",
                                                       "funding_rate", "oi_chg_pct", "slope_ma5_bps",
                                                       "slope_ma20_bps", "net_bps"]])
    res = pd.DataFrame(rows)
    if res.empty:
        print("no triggers")
        return
    res = res.sort_values(["avg_bps", "win_rate", "trades"], ascending=[False, False, False])
    print("\nTOP RESULTS")
    print(res.head(30).to_string(index=False, formatters={
        "win_rate": "{:.1%}".format,
        "avg_bps": "{:.2f}".format,
        "median_bps": "{:.2f}".format,
        "sum_bps": "{:.1f}".format,
        "day_win_rate": "{:.1%}".format,
    }))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        res.to_csv(out, index=False)
        print(f"wrote {out}")
        if best_trades:
            detail = out.with_name(out.stem + "_trades.csv")
            pd.concat(best_trades, ignore_index=True).to_csv(detail, index=False)
            print(f"wrote {detail}")


if __name__ == "__main__":
    main()
