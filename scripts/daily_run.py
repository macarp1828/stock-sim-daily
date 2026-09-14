"""One entry point for the daily scheduled run: refresh data, extend the
mechanical backtest through yesterday, regenerate the report + artifact JSON,
and build today's premarket watchlist. Does not touch git -- the caller
(the routine's prompt) commits and pushes after this exits."""
import subprocess, sys, os, json
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(HERE)


def run(script):
    print(f"=== running {script} ===", flush=True)
    r = subprocess.run([sys.executable, script], cwd=HERE)
    if r.returncode != 0:
        print(f"!!! {script} exited {r.returncode}", flush=True)


def export_trades_json():
    pkl = os.path.join(BASE_DIR, "stock_trades.pkl")
    if not os.path.exists(pkl):
        return
    t = pd.read_pickle(pkl)

    def reason_dict_out(r):
        return {
            "decision_time": r["decision_time"], "day_open": r["day_open"],
            "pct_move_at_decision": r["pct_move_at_decision"],
            "range_high": r["range_high"], "range_low": r["range_low"],
            "breakout_time": r["breakout_time"], "breakout_close": r["breakout_close"],
            "rel_volume": r.get("rel_volume"),
            "gap_reason": r.get("gap_reason", ""),
            "top3": r.get("top3", []),
        }

    out = []
    for _, r in t.iterrows():
        out.append({
            "date": r["date"], "ticker": r["ticker"], "name": r["name"], "dir": r["dir"],
            "entry_time": r["entry_time"], "entry": r["entry"], "sl": r["sl"], "tp": r["tp"],
            "shares": r["shares"], "exit_time": r["exit_time"], "exit_price": r["exit_price"],
            "exit_reason": r["exit_reason"], "pnl": r["pnl"], "equity_after": r["equity_after"],
            "reason": reason_dict_out(r["reason"]),
        })
    with open(os.path.join(BASE_DIR, "stock_trades_for_artifact.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"exported {len(out)} trades to stock_trades_for_artifact.json", flush=True)


def main():
    run(os.path.join(HERE, "fetch_all.py"))
    run(os.path.join(HERE, "stock_backtest.py"))
    run(os.path.join(HERE, "stock_generate_report.py"))
    export_trades_json()
    run(os.path.join(HERE, "premarket_watchlist.py"))
    print("=== daily_run.py complete ===", flush=True)


if __name__ == "__main__":
    main()
