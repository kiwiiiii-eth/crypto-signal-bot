#!/usr/bin/env python3
"""TradFi 股票永續跨所費率差監控（OKX vs Bitget, 每小時 cron）。

價差 >= 門檻且連續兩次輪詢都超過 → TG 告警（收租機會）。
純公開 API, 不需帳戶。狀態存 data/tradfi_state.json。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

TPE = timezone(timedelta(hours=8))
DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "tradfi_state.json"
LOGCSV = DATA / "tradfi_spreads.csv"
THRESHOLD = float(os.getenv("TRADFI_SPREAD_THRESHOLD", "0.0015"))  # 0.15%/期
UA = {"User-Agent": "Mozilla/5.0"}


def get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def okx_stock_frs() -> dict[str, float]:
    insts = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")["data"]
    # 股票永續: ctVal 對應股數且非加密幣。用白名單前綴最穩。
    names = {"NVDA", "TSLA", "AAPL", "MSTR", "COIN", "HOOD", "SKHYNIX", "SAMSUNG",
             "TSM", "META", "AMZN", "GOOGL", "OPENAI", "CRCL", "PLTR", "AMD",
             "INTC", "MCD", "OPEN"}
    out = {}
    for i in insts:
        base = i["instId"].split("-")[0]
        if base in names:
            try:
                d = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={i['instId']}")["data"]
                out[base] = float(d[0]["fundingRate"])
            except Exception:
                pass
    return out


def bitget_stock_frs(names) -> dict[str, float]:
    out = {}
    for base in names:
        try:
            d = get(f"https://api.bitget.com/api/v2/mix/market/current-fund-rate"
                    f"?symbol={base}USDT&productType=USDT-FUTURES")["data"]
            out[base] = float(d[0]["fundingRate"])
        except Exception:
            pass
    return out


def tg_send(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(dry) " + text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=10)


def main() -> None:
    okx = okx_stock_frs()
    bg = bitget_stock_frs(okx.keys())
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now(TPE)

    rows, hot = [], []
    for base in sorted(set(okx) & set(bg)):
        spread = okx[base] - bg[base]  # >0: OKX空+BG多收租; <0: 反向
        rows.append((base, okx[base], bg[base], spread))
        if abs(spread) >= THRESHOLD and abs(prev.get(base, 0)) >= THRESHOLD:
            legs = ("OKX 空 / Bitget 多" if spread > 0 else "OKX 多 / Bitget 空")
            hot.append(f"`{base}` 價差 `{spread:+.4%}`/期 ≈ `{abs(spread)*3*100:.2f}%`/天\n"
                       f"  OKX `{okx[base]:+.4%}` vs BG `{bg[base]:+.4%}` → {legs}")

    new = not LOGCSV.exists()
    with LOGCSV.open("a") as f:
        if new:
            f.write("ts,base,okx_fr,bg_fr,spread\n")
        for base, o, b, s in rows:
            f.write(f"{int(now.timestamp())},{base},{o},{b},{s}\n")
    STATE.write_text(json.dumps({b: s for b, o, _, s in
                                 [(r[0], r[1], r[2], r[3]) for r in rows]}))

    if hot:
        tg_send("💰 *TradFi 跨所收租機會*（連續兩次輪詢 ≥0.15%/期）\n\n" + "\n".join(hot))
    top = max(rows, key=lambda r: abs(r[3])) if rows else None
    print(f"{now:%m-%d %H:%M} 掃描 {len(rows)} 檔, 告警 {len(hot)}, "
          f"最大價差 {top[0]} {top[3]:+.4%}" if top else "no data")


if __name__ == "__main__":
    main()
