import pandas as pd
import json, os, datetime

OUT_DIR = "/Users/nakamurashunsuke/Desktop/Claude code/株シミュレーション"
START_EQUITY = 100_000.0


def reason_text(r):
    top3_str = "、".join(
        f"{n}({p:+.2f}%{', 出来高'+str(rv)+'倍' if rv else ''})" for _, n, p, rv in r["top3"]
    )
    skip_note = ""
    if r["rank_among_movers"] == 2:
        skip_note = "値動き1位は60分以内のブレイク不成立、または資金管理条件を満たせずスキップ。"
    elif r["rank_among_movers"] > 2:
        skip_note = (f"値動き1〜{r['rank_among_movers']-1}位は60分以内のブレイク不成立、"
                     f"または資金管理条件を満たせずスキップ。")
    relvol_note = f"直近5営業日平均比 出来高{r['rel_volume']}倍。" if r.get("rel_volume") else ""
    return (f"根拠: 9:00〜{r['decision_time']}時点、自身の分足EMA9/20のトレンド方向と一致する銘柄の中で"
            f"値動き上位3銘柄は{top3_str}。{skip_note}"
            f"値動き{r['rank_among_movers']}位の本銘柄を選定(始値{r['day_open']}円→{r['decision_time']}時点{r['pct_move_at_decision']:+.2f}%、"
            f"{relvol_note}EMAトレンドと同方向)。"
            f"{r['breakout_time']}にレンジを{('上抜け' if r['pct_move_at_decision']>0 else '下抜け')}(終値{r['breakout_close']}円)、次の足の始値でエントリー。")


def main():
    trades = pd.read_pickle(os.path.join(OUT_DIR, "stock_trades.pkl"))
    days = sorted(trades["date"].unique())
    equity = START_EQUITY
    lines = []
    wins = int((trades["pnl"] >= 0).sum())
    losses = int((trades["pnl"] < 0).sum())
    lines.append("## 株シミュレーション サマリー(9/1〜9/11)")
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
