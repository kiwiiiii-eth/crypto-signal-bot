"""HyperLiquid 聰明錢監控（純情報推播，不下單）。

- 名單: 排行榜篩「帳戶>50萬U、全期ROI>50%、全期賺>100萬U、本月為正、非做市型」，
  取全期 PnL 前 N 名且倉位可查者，每 24h 刷新一次。
- 輪詢: 每 HL_POLL_MIN 分鐘掃全名單倉位，偵測 開新倉/平倉/翻向/加減倉>50%。
- 快照全量落檔 data/hl_snapshots.csv —— 這是未來回測「跟單 fwd 報酬」的資料收集，
  HL 沒有歷史倉位 API，現在開始存才有得測。
"""
from __future__ import annotations

import csv
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAP_FILE = DATA_DIR / "hl_snapshots.csv"

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"


def addr_links(addr: str) -> str:
    return (f"[Hypurrscan](https://hypurrscan.io/address/{addr}) | "
            f"[HyperDash](https://hyperdash.info/trader/{addr}) | "
            f"[HL App](https://app.hyperliquid.xyz/explorer/address/{addr})")


def _post(payload: dict, timeout: float = 15):
    req = urllib.request.Request(INFO, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def build_watchlist(top_n: int = 60) -> list[str]:
    with urllib.request.urlopen(LEADERBOARD, timeout=60) as r:
        rows = json.loads(r.read())["leaderboardRows"]
    smart = []
    for row in rows:
        w = {k: v for k, v in row["windowPerformances"]}
        av = float(row["accountValue"])
        at, m, wk = w.get("allTime", {}), w.get("month", {}), w.get("week", {})
        pnl_at, vlm_at = float(at.get("pnl", 0)), float(at.get("vlm", 0))
        if av < 500_000 or float(at.get("roi", 0)) < 0.5 or pnl_at < 1_000_000:
            continue
        if float(m.get("roi", 0)) <= 0:
            continue
        if vlm_at > 0 and pnl_at / vlm_at < 0.0005:  # 做市/HFT 型
            continue
        if float(wk.get("vlm", 0)) < 100_000:  # 本週沒在交易的不追(名單要活躍)
            continue
        smart.append((row["ethAddress"], pnl_at))
    smart.sort(key=lambda x: -x[1])
    out = []
    for addr, _ in smart:
        if len(out) >= top_n:
            break
        try:  # 過濾倉位查不到的（資金在 vault/子帳戶）
            st = _post({"type": "clearinghouseState", "user": addr})
            if float(st["marginSummary"]["accountValue"]) > 100_000:
                out.append(addr)
        except Exception:
            continue
        time.sleep(0.2)
    return out


def fetch_positions(addr: str) -> dict[str, float]:
    """coin -> 帶方向名目 USDT（多為正）。"""
    st = _post({"type": "clearinghouseState", "user": addr})
    out = {}
    for p in st.get("assetPositions", []):
        pos = p["position"]
        szi = float(pos["szi"])
        if szi == 0:
            continue
        out[pos["coin"]] = abs(float(pos["positionValue"])) * (1 if szi > 0 else -1)
    return out


def daily_digest(addr: str) -> str:
    """單一地址的真實績效日報：已實現+未實現一起看，戳破「只平獲利單」的假勝率。"""
    st = _post({"type": "clearinghouseState", "user": addr})
    ms = st["marginSummary"]
    lines = [f"📋 *HL 追蹤日報* `{addr[:8]}`"]
    upnl_total = 0.0
    for p in st.get("assetPositions", []):
        q = p["position"]
        szi = float(q["szi"])
        if szi == 0:
            continue
        upnl = float(q["unrealizedPnl"])
        upnl_total += upnl
        side = "多" if szi > 0 else "空"
        lines.append(f"  {q['coin']} {side} `{abs(float(q['positionValue']))/1e6:.2f}M` "
                     f"{q['leverage']['value']}x 未實現 `{upnl:+,.0f}`")
    pf = _post({"type": "portfolio", "user": addr})
    for period, label in (("day", "24h"), ("week", "7日"), ("month", "30日")):
        d = dict(pf).get(period)
        if not d or not d.get("pnlHistory"):
            continue
        h = d["pnlHistory"]
        av = d["accountValueHistory"]
        lines.append(f"  {label} PnL `{float(h[-1][1]) - float(h[0][1]):+,.0f}` "
                     f"帳戶 `{float(av[-1][1])/1e6:.2f}M`")
    lines.append(f"  未實現合計 `{upnl_total:+,.0f}`")
    lines.append(addr_links(addr))
    return "\n".join(lines)


class HLWatcher:
    def __init__(self, notifier, poll_min: int = 5, top_n: int = 60,
                 min_notional: float = 500_000, tracked: list[str] | None = None):
        self.notifier = notifier
        self.poll_min = poll_min
        self.top_n = top_n
        self.min_notional = min_notional  # 小於此名目的變動不推播（快照照存）
        self.tracked = tracked or []  # 每日推真實績效日報的地址
        self.digest_day = ""
        self.watchlist: list[str] = []
        self.prev: dict[str, dict[str, float]] = {}
        self.list_refreshed = 0.0

    def _log_snapshots(self, ts: int, addr: str, positions: dict[str, float]) -> None:
        new = not SNAP_FILE.exists()
        with SNAP_FILE.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "addr", "coin", "signed_notional"])
            for coin, ntl in positions.items():
                w.writerow([ts, addr, coin, round(ntl, 2)])

    def _diff_and_notify(self, addr: str, old: dict[str, float], new: dict[str, float]) -> None:
        tag = addr[:8]
        for coin in set(old) | set(new):
            o, n = old.get(coin, 0.0), new.get(coin, 0.0)
            if max(abs(o), abs(n)) < self.min_notional:
                continue
            msg = None
            side = "多" if n > 0 else "空"
            if o == 0 and n != 0:
                msg = f"🐳 `{tag}` 開新倉 *{coin} {side}* `{abs(n)/1e6:.2f}M`"
            elif o != 0 and n == 0:
                msg = f"🐳 `{tag}` 平掉 *{coin}*（原 `{abs(o)/1e6:.2f}M`）"
            elif o * n < 0:
                msg = f"🐳🔄 `{tag}` *{coin} 翻向 → {side}* `{abs(n)/1e6:.2f}M`"
            elif abs(n) > abs(o) * 1.5:
                msg = f"🐳 `{tag}` *{coin} {side}* 加倉 `{abs(o)/1e6:.2f}M→{abs(n)/1e6:.2f}M`"
            elif abs(n) < abs(o) * 0.5:
                msg = f"🐳 `{tag}` *{coin} {side}* 減倉 `{abs(o)/1e6:.2f}M→{abs(n)/1e6:.2f}M`"
            if msg:
                self.notifier.send(msg + "\n" + addr_links(addr))

    def _loop(self):
        while True:
            try:
                if time.time() - self.list_refreshed > 86400 or not self.watchlist:
                    self.watchlist = build_watchlist(self.top_n)
                    self.list_refreshed = time.time()
                    print(f"HL 名單刷新: {len(self.watchlist)} 地址", file=sys.stderr)
                day = time.strftime("%Y-%m-%d", time.localtime())
                if self.tracked and day != self.digest_day:
                    self.digest_day = day
                    for a in self.tracked:
                        try:
                            self.notifier.send(daily_digest(a))
                        except Exception as e:
                            print(f"HL 日報錯誤 {a[:8]}: {e}", file=sys.stderr)
                        time.sleep(0.3)
                ts = int(time.time())
                for addr in self.watchlist:
                    try:
                        cur = fetch_positions(addr)
                    except Exception:
                        continue
                    self._log_snapshots(ts, addr, cur)
                    if addr in self.prev:
                        self._diff_and_notify(addr, self.prev[addr], cur)
                    self.prev[addr] = cur
                    time.sleep(0.3)  # 全名單一輪 ~20s, 對 API 溫柔
            except Exception as e:
                print(f"HL watch 錯誤: {e}", file=sys.stderr)
            time.sleep(self.poll_min * 60)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="hl-watch").start()
