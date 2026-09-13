import pandas as pd
import json, os, datetime

OUT_DIR = "/Users/nakamurashunsuke/Desktop/Claude code/株シミュレーション"
START_EQUITY = 100_000.0


def reason_text(r):
    relvol_note = f"直近5営業日平均比 出来高{r['rel_volume']}倍。" if r.get("rel_volume") else ""
    entry_detail = (
        f"{r['decision_time']}時点の直近30分レンジ(高値{r['range_high']}円/安値{r['range_low']}円、"
        f"始値{r['day_open']}円からの騰落率{r['pct_move_at_decision']:+.2f}%、{relvol_note}EMAトレンドと同方向)を、"
        f"{r['breakout_time']}に{('上抜け' if r['pct_move_at_decision']>0 else '下抜け')}"
        f"(終値{r['breakout_close']}円)、次の足の始値でエントリー。"
    )
    gap = r.get("gap_reason", "")
    return f"経緯: {gap}\n根拠: {entry_detail}"


def main():
    trades = pd.read_pickle(os.path.join(OUT_DIR, "stock_trades.pkl"))
    days = sorted(trades["date"].unique())
    equity = START_EQUITY
    lines = []
    wins = int((trades["pnl"] >= 0).sum())
    losses = int((trades["pnl"] < 0).sum())
    lines.append(f"## 株シミュレーション サマリー({days[0]}〜{days[-1]})")
    lines.append("")
    lines.append(f"**トレード回数: {len(trades)}回 / {wins}勝{losses}敗**")
    lines.append(f"**累計損益: {(trades.iloc[-1]['equity_after']-START_EQUITY):+,.0f}円**")
    lines.append(f"**累計資産: {trades.iloc[-1]['equity_after']:,.0f}円**")
    lines.append("")
    for d in days:
        day_trades = trades[trades["date"] == d]
        start_eq = equity
        end_eq = day_trades.iloc[-1]["equity_after"]
        day_pnl = end_eq - start_eq
        equity = end_eq
        lines.append(f"### {d}")
        lines.append(f"当日損益: {day_pnl:+,.0f}円 / 終了資産: {end_eq:,.0f}円")
        for _, tr in day_trades.iterrows():
            lines.append("")
            lines.append(f"**{tr['ticker']} {tr['name']}** ({tr['dir']})")
            lines.append(f"{tr['entry_time']} Entry {tr['entry']}円 / SL {tr['sl']}円 / TP {tr['tp']}円 / {tr['shares']}株")
            lines.append(reason_text(tr["reason"]))
            lines.append(f"{tr['exit_time']} {tr['exit_reason']} Exit {tr['exit_price']}円 損益 {tr['pnl']:+,.0f}円")
        lines.append("")

    report = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "stock_daily_report.md"), "w") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
