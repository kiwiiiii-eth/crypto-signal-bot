"""Longer-timeframe trend helpers for discretionary review and future filters."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


BINANCE = "https://fapi.binance.com"
COINGLASS = "https://open-api-v4.coinglass.com"


def _get_json(url: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "crypto-signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ema(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (window + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * alpha + out[-1] * (1 - alpha))
    return out


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i < window - 1:
            out.append(None)
        else:
            chunk = values[i - window + 1:i + 1]
            out.append(sum(chunk) / window)
    return out


def binance_klines(symbol: str, interval: str, limit: int = 220) -> list[dict]:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = _get_json(f"{BINANCE}/fapi/v1/klines?{q}")
    return [{"time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
            for k in raw]


def binance_trend(symbol: str, interval: str) -> dict:
    ks = binance_klines(symbol, interval)
    closes = [k["close"] for k in ks]
    if len(closes) < 80:
        raise ValueError(f"{symbol} {interval} K線不足")
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ma120 = _sma(closes, 120)
    close = closes[-1]
    ma5v, ma20v, ma60v, ma120v = ma5[-1], ma20[-1], ma60[-1], ma120[-1]
    slope5 = (ma5[-1] / ma5[-4] - 1) * 10000 if ma5[-1] and ma5[-4] else 0
    slope20 = (ma20[-1] / ma20[-4] - 1) * 10000 if ma20[-1] and ma20[-4] else 0
    slope60 = (ma60[-1] / ma60[-4] - 1) * 10000 if ma60[-1] and ma60[-4] else 0
    bull = bool(close > ma5v > ma20v > ma60v and slope5 > 0 and slope20 >= 0)
    full_bull = bool(ma120v and close > ma5v > ma20v > ma60v > ma120v and slope20 >= 0 and slope60 >= 0)
    boxed_up = bool(close > ma20v and ma5v > ma20v and (ma60v is None or close > ma60v) and slope20 >= 0)
    return {"source": "binance", "symbol": symbol, "interval": interval,
            "close": close, "ma5": ma5v, "ma20": ma20v,
            "ma60": ma60v, "ma120": ma120v, "slope5_bps": slope5,
            "slope20_bps": slope20, "slope60_bps": slope60,
            "bull": bull, "full_bull": full_bull, "boxed_up": boxed_up}


def coinglass_ema(symbol: str, interval: str, window: int, api_key: str,
                  exchange: str = "Binance") -> dict | None:
    if not api_key:
        return None
    q = urllib.parse.urlencode({
        "exchange": exchange, "symbol": symbol, "interval": interval,
        "limit": 10, "window": window, "series_type": "close",
    })
    headers = {"accept": "application/json", "CG-API-KEY": api_key,
               "User-Agent": "crypto-signal-bot"}
    raw = _get_json(f"{COINGLASS}/api/futures/indicators/ema?{q}", headers=headers)
    data = raw.get("data") or []
    if not data:
        return None
    last = data[-1]
    return {"source": "coinglass", "symbol": symbol, "interval": interval,
            "window": window, "ema": float(last["ema_value"]),
            "time": int(last["time"])}


def coinglass_ma(symbol: str, interval: str, window: int, api_key: str,
                 exchange: str = "Binance") -> dict | None:
    if not api_key:
        return None
    q = urllib.parse.urlencode({
        "exchange": exchange, "symbol": symbol, "interval": interval,
        "limit": 10, "window": window, "series_type": "close",
    })
    headers = {"accept": "application/json", "CG-API-KEY": api_key,
               "User-Agent": "crypto-signal-bot"}
    raw = _get_json(f"{COINGLASS}/api/futures/indicators/ma?{q}", headers=headers)
    data = raw.get("data") or []
    if not data:
        return None
    last = data[-1]
    return {"source": "coinglass", "symbol": symbol, "interval": interval,
            "window": window, "ma": float(last["ma_value"]),
            "time": int(last["time"])}


def trend_summary(symbol: str, coinglass_key: str = "",
                  coinglass_exchange: str = "Binance") -> str:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    parts = []
    for interval in ("4h", "1d"):
        t = binance_trend(symbol, interval)
        state = "完整多排" if t["full_bull"] else (
            "短多排列" if t["bull"] else ("偏多整理" if t["boxed_up"] else "未偏多"))
        parts.append(
            f"`{interval}` {state}｜close `{t['close']:.6g}`｜"
            f"MA5/20/60/120 `{t['ma5']:.6g}` / `{t['ma20']:.6g}` / "
            f"`{t['ma60']:.6g}` / `{(t['ma120'] or 0):.6g}`｜"
            f"slope5 `{t['slope5_bps']:+.0f}bps` "
            f"slope20 `{t['slope20_bps']:+.0f}bps` slope60 `{t['slope60_bps']:+.0f}bps`")
    if coinglass_key:
        cg_bits = []
        for interval in ("4h", "1d"):
            for window in (5, 20, 60):
                try:
                    r = coinglass_ma(symbol, interval, window, coinglass_key, coinglass_exchange)
                except Exception:
                    r = None
                if r:
                    cg_bits.append(f"{interval} MA{window} `{r['ma']:.6g}`")
        if cg_bits:
            parts.append("CoinGlass: " + "｜".join(cg_bits))
    return f"📈 *趨勢檢查* `{symbol}`\n\n" + "\n".join(parts)
