#!/usr/bin/env python3
"""crypto-signal-bot 入口。

重放模式（paper trading 回放驗證）:
    python main.py replay <minute_last.csv> <funding_minute.csv> [trades_out.csv]

實時模式（等新 InfluxDB 上線後接上）:
    python main.py live   # TODO: InfluxFeed 每 60s 輪詢，走相同 check() 路徑
"""
import sys

import pandas as pd


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"replay", "live"}:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "live":
        print("實時模式尚未接上資料源（等新 InfluxDB 伺服器），先用 replay 驗證。")
        sys.exit(1)

    minute_csv, funding_csv = sys.argv[2], sys.argv[3]
    out_csv = sys.argv[4] if len(sys.argv) > 4 else ""

    from bot.replay import run
    ledger, notifier = run(minute_csv, funding_csv, out_csv)

    R = pd.DataFrame([{
        "strategy": p.signal.strategy, "reason": p.exit_reason,
        "pnl_bps": p.pnl_bps, "pnl_usdt": p.pnl_usdt,
        "day": pd.to_datetime(p.exit_minute * 60, unit="s").date(),
    } for p in ledger.closed])

    print("\n===== Paper Trading 重放結果 =====")
    print(f"總平倉單數: {len(R)}  總淨損益: {ledger.total_pnl_usdt:+.2f} USDT")
    if len(R):
        for s, g in R.groupby("strategy"):
            wr = (g["pnl_bps"] > 0).mean() * 100
            daily = g.groupby("day")["pnl_usdt"].sum()
            print(f"  策略{s}: n={len(g)} 淨={g['pnl_bps'].mean():+.1f}bps 勝率={wr:.0f}% "
                  f"合計={g['pnl_usdt'].sum():+.2f} USDT 正日比={(daily > 0).mean() * 100:.0f}%")
        stops = (R["reason"] == "stop").sum()
        print(f"  災難停損觸發: {stops} 次 ({stops / len(R) * 100:.1f}%)")
        print("\n  逐日淨損益 (USDT):")
        for d, v in R.groupby("day")["pnl_usdt"].sum().items():
            print(f"    {d}: {v:+.2f}")
    print(f"\n未成交拒絕統計: {ledger.rejected}")
    print(f"推播訊息數: {len(notifier.sent)}")
    print("\n----- 訊息範例 (前 2 則) -----")
    for m in notifier.sent[:2]:
        print(m, "\n" + "-" * 40)


if __name__ == "__main__":
    main()
