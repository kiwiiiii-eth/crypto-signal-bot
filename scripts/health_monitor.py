#!/usr/bin/env python3
"""Field-level data-pipeline and host health monitor.

Checks, alerting to Telegram with per-key cooldown + recovery notices:
1. crypto_futures field staleness per exchange (a WS leg can die silently
   while the connection stays "up" — the 2026-07-05 Binance !ticker@arr
   incident went unnoticed for 7 days because health only counted messages)
2. crypto_ma freshness
3. Disk usage
4. Memory availability
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "health_monitor_state.json"
COOLDOWN_S = 3600  # repeat an active alert at most hourly

# field -> max acceptable staleness (seconds)
FUTURES_FIELDS = {"last_price": 180, "open_interest": 600, "funding_rate": 900}
EXCHANGES = ("binance", "bitget")
MA_MAX_STALE_S = 20 * 60  # crypto_ma 5m cadence + slack
DISK_ALERT_PCT = 85
MEM_ALERT_PCT = 90  # used


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def influx_last_times(flux: str) -> dict[tuple[str, str], float]:
    """Run a Flux query returning last() rows; map (exchange,_field)->epoch s."""
    url = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    org = os.getenv("INFLUXDB_ORG", "crypto")
    token = os.environ["INFLUXDB_TOKEN"]
    req = urllib.request.Request(
        f"{url}/api/v2/query?org={urllib.parse.quote(org)}",
        data=json.dumps({"query": flux, "type": "flux"}).encode(),
        headers={"Authorization": f"Token {token}",
                 "Content-Type": "application/json", "Accept": "application/csv"},
    )
    out: dict[tuple[str, str], float] = {}
    with urllib.request.urlopen(req, timeout=30) as resp:
        header: list[str] = []
        for raw in resp.read().decode().splitlines():
            if not raw or raw.startswith("#"):
                continue
            cells = raw.split(",")
            if "_time" in cells:
                header = cells
                continue
            if not header or len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            ts = row.get("_time", "")
            try:
                epoch = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except ValueError:
                continue
            out[(row.get("exchange", ""), row.get("_field", ""))] = epoch
    return out


def check_futures_staleness() -> list[str]:
    fields = " or ".join(f'r._field == "{f}"' for f in FUTURES_FIELDS)
    flux = (
        'from(bucket: "exchange") |> range(start: -2h)\n'
        '|> filter(fn: (r) => r._measurement == "crypto_futures"'
        ' and r.symbol == "BTCUSDT")\n'
        f"|> filter(fn: (r) => {fields})\n"
        '|> group(columns: ["exchange", "_field"]) |> last()\n'
        '|> keep(columns: ["exchange", "_field", "_time"])'
    )
    last = influx_last_times(flux)
    now = time.time()
    problems = []
    for exchange in EXCHANGES:
        for field, max_stale in FUTURES_FIELDS.items():
            age = now - last.get((exchange, field), 0)
            if age > max_stale:
                seen = "從未寫入" if (exchange, field) not in last else f"{int(age)}s 未更新"
                problems.append(f"crypto_futures {exchange}.{field}: {seen}（門檻 {max_stale}s）")
    return problems


def check_ma_freshness() -> list[str]:
    flux = (
        'from(bucket: "exchange") |> range(start: -6h)\n'
        '|> filter(fn: (r) => r._measurement == "crypto_ma"'
        ' and r.symbol == "BTCUSDT" and r.interval == "5m"'
        ' and r._field == "close")\n'
        '|> group(columns: ["exchange", "_field"]) |> last()\n'
        '|> keep(columns: ["exchange", "_field", "_time"])'
    )
    last = influx_last_times(flux)
    if not last:
        return [f"crypto_ma: 6 小時內無任何 5m 資料（門檻 {MA_MAX_STALE_S}s）"]
    age = time.time() - max(last.values())
    if age > MA_MAX_STALE_S:
        return [f"crypto_ma: {int(age)}s 未更新（門檻 {MA_MAX_STALE_S}s）"]
    return []


def check_disk() -> list[str]:
    usage = shutil.disk_usage("/")
    pct = usage.used / usage.total * 100
    if pct >= DISK_ALERT_PCT:
        free_gb = usage.free / 1e9
        return [f"磁碟使用 {pct:.0f}%（剩 {free_gb:.1f}G，門檻 {DISK_ALERT_PCT}%）"]
    return []


def check_memory() -> list[str]:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0])  # kB
    total, avail = info["MemTotal"], info.get("MemAvailable", 0)
    pct = (total - avail) / total * 100
    if pct >= MEM_ALERT_PCT:
        return [f"記憶體使用 {pct:.0f}%（可用 {avail // 1024}MB，門檻 {MEM_ALERT_PCT}%）"]
    return []


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"(no telegram configured) {text}", file=sys.stderr)
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15
    ).read()


def main() -> None:
    load_dotenv()
    problems: list[str] = []
    for check in (check_futures_staleness, check_ma_freshness, check_disk, check_memory):
        try:
            problems.extend(check())
        except Exception as exc:
            problems.append(f"{check.__name__} 自身失敗: {exc}")

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except ValueError:
            state = {}
    now = time.time()
    active = {p: state.get(p, 0) for p in problems}

    to_alert = [p for p, last_sent in active.items() if now - last_sent >= COOLDOWN_S]
    recovered = [p for p in state if p not in active]

    if to_alert:
        send_telegram("⚠️ [health] Server A 健康檢查異常\n" + "\n".join(f"- {p}" for p in to_alert))
        for p in to_alert:
            active[p] = now
    if recovered:
        send_telegram("✅ [health] 已恢復\n" + "\n".join(f"- {p}" for p in recovered))

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(active))
    status = "problems=" + str(len(problems)) if problems else "all green"
    print(f"health_monitor {status}")


if __name__ == "__main__":
    main()
