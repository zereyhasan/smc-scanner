"""SMC Terminal — SON SÜRÜM (paper v2 entegre)
Tek komut: tarama (MCAP-50) → kapı backtest → Notion → paper (bakiyeli) → HTML rapor.
Hızlı mod: python report.py hizli"""
import sys, webbrowser
from pathlib import Path
from datetime import datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import smc
import scanner
import backtest

NOTION_ENABLED = False
try:
    import config as _cfg
    _tok = str(getattr(_cfg, "NOTION_TOKEN", ""))
    NOTION_ENABLED = bool(_tok) and ("XXXX" not in _tok) and ("BURAYA" not in _tok)
except Exception:
    pass

PAPER_OK = False
try:
    import paper_trade
    PAPER_OK = True
except Exception:
    pass

BT_GATE_TRADES = 8
BT_GATE_PF     = 1.2

def kpi_html(label, val, color="#eaf0ff"):
    return (f'<div class="kpi"><small>{label}</small>'
            f'<b style="color:{color}">{val}</b></div>')

def rich_chart(s):
    ltf = scanner.klines(s["symbol"], "15m", 300)
    htf = scanner.klines(s["symbol"], "1h", 500)
    ctx = scanner.build_context(s["symbol"], htf, ltf)

    fig = go.Figure(go.Candlestick(
        x=ltf["t"], open=ltf["open"], high=ltf["high"], low=ltf["low"],
        close=ltf["close"], name=s["symbol"],
        increasing_line_color="#26a65b", decreasing_line_color="#e14b5a"))

    t0, t1 = ltf["t"].iloc[0], ltf["t"].iloc[-1]
    zone = s.get("zone")

    def near(a, b):
        return zone is not None and abs(a - b) <= 1e-9 * max(1.0, abs(a))

    for ob in [o for o in ctx["obs"] if not o["mitigated"]][-6:]:
        aktif = near(ob["bottom"], zone[0]) and near(ob["top"], zone[1])
        if aktif:
            col = "rgba(38,166,91,.35)" if ob["type"] == "bullish" else "rgba(225,75,90,.35)"
            fig.add_shape(type="rect", x0=t0, x1=t1, y0=ob["bottom"], y1=ob["top"],
                          fillcolor=col, line=dict(color=col.replace(".35", ".9"), width=1),
                          layer="below")
            fig.add_annotation(x=t0, y=ob["top"], text="TETİKLEYEN OB", showarrow=False,
                               yshift=10, font=dict(size=9, color="#8ea0c9"))
        else:
            fig.add_shape(type="rect", x0=t0, x1=t1, y0=ob["bottom"], y1=ob["top"],
                          fillcolor="rgba(120,140,190,.10)", line=dict(width=0),
                          layer="below")

    for f in [f for f in ctx["fvgs"] if not f["filled"]][-6:]:
        aktif = near(f["bottom"], zone[0]) and near(f["top"], zone[1])
        fig.add_shape(type="rect", x0=t0, x1=t1, y0=f["bottom"], y1=f["top"],
                      fillcolor=f"rgba(90,140,255,{'.30' if aktif else '.10'})",
                      line=dict(width=1 if aktif else 0,
                                color="rgba(90,140,255,.8)" if aktif else None),
                      layer="below")
        if aktif:
            fig.add_annotation(x=t0, y=f["top"], text="TETİKLEYEN FVG", showarrow=False,
                               yshift=10, font=dict(size=9, color="#8ea0c9"))

    ph = pl = None
    for sw in smc.find_swings(ltf)[-10:]:
        if sw.kind == "H":
            lab = "HH" if (ph is not None and sw.price > ph) else ("LH" if ph else "H")
            ph = sw.price
            col = "#26a65b" if lab == "HH" else "#e14b5a"
        else:
            lab = "HL" if (pl is not None and sw.price > pl) else ("LL" if pl else "L")
            pl = sw.price
            col = "#26a65b" if lab == "HL" else "#e14b5a"
        fig.add_annotation(x=ltf["t"].iloc[sw.idx], y=sw.price, text=lab, showarrow=False,
                           yshift=12 if sw.kind == "H" else -16,
                           font=dict(size=9, color=col))

    ema_df = pd.DataFrame({"t": htf["t"],
                           "ema": htf["close"].ewm(span=200, adjust=False).mean()})
    m = pd.merge_asof(ltf[["t"]], ema_df, on="t")
    fig.add_scatter(x=m["t"], y=m["ema"], mode="lines", name="EMA200 (1H)",
                    line=dict(color="#ff9f43", width=1.2))

    if ctx["pd"]:
        fig.add_hline(y=ctx["pd"]["eq"], line_dash="dot", line_color="#8ea0c9",
                      annotation_text="EQ (50%)")

    if "Süpürme" in s["strategy"] and zone:
        y = zone[0] if s["direction"] == "LONG" else zone[1]
        fig.add_hline(y=y, line_dash="dot", line_color="#e14b5a",
                      annotation_text="Sweep (stop hunt)")

    for y, c, txt in [(s["entry"], "#f5c542", "GİRİŞ"),
                      (s["sl"], "#e14b5a", "SL"), (s["tp"], "#26a65b", "TP")]:
        fig.add_hline(y=y, line_dash="dash", line_color=c, annotation_text=txt)

    fig.add_annotation(x=t1, y=s["entry"], text="SİNYAL MUMU", ax=-45, ay=-45,
                       arrowhead=2, font=dict(size=9, color="#f5c542"))

    fig.add_annotation(xref="paper", yref="paper", x=0, y=1.10, xanchor="left",
                       text="Neden: " + " · ".join(s["confluences"]),
                       showarrow=False, font=dict(size=10, color="#8ea0c9"), align="left")

    fig.update_layout(template="plotly_dark", height=520,
                      title=f'{s["symbol"]} — {s["strategy"]} ({s["direction"]}) | '
                            f'Skor {s["score"]} | RR 1:{s["rr"]}',
                      xaxis_rangeslider_visible=False,
                      margin=dict(l=10, r=10, t=80, b=10),
                      legend=dict(orientation="h", y=1.06, x=1, xanchor="right"))
    return fig

def run():
    print("1/4) Tarama (MCAP ilk 50)...", flush=True)
    res = scanner.scan()

    bt = {}
    if "hizli" not in sys.argv and res:
        sig_syms = sorted({r["symbol"] for r in res})
        print(f"2/4) Backtest — sinyal veren {len(sig_syms)} coin × 3000 bar (~3-6 dk)...", flush=True)
        bt = backtest.backtest_all(bars=3000, symbols=sig_syms)
    else:
        print("2/4) Hızlı mod — backtest atlandı, kapı gevşek.", flush=True)

    def enrich(r):
        key = scanner.STRAT_BY_LABEL.get(r["strategy"], r["strategy"])
        m = bt.get(r["symbol"], {}).get(key) if bt else None
        if bt:
            if m is None or m["trades"] < BT_GATE_TRADES or m["pf"] < BT_GATE_PF:
                return None
            bts = m["score"]
        else:
            bts = 0
        return {**r, "m": m, "bt_score": bts,
                "final": round(0.55 * r["score"] + 0.45 * bts)}

    sigs = sorted([e for e in (enrich(r) for r in res) if e], key=lambda x: -x["final"])

    print("3/4) Notion + Paper trade...", flush=True)
    notion_written = None
    if NOTION_ENABLED and sigs:
        try:
            import notion
            notion_written = notion.write_signals(sigs, top=10)
        except Exception as e:
            print(f"  ⚠ Notion yazımı başarısız: {e!r}")
    elif not NOTION_ENABLED:
        print("Notion kapalı (config.py token'ı yok/yer tutucu).")
    else:
        print("Notion: kapıdan geçen sinyal yok.")

    paper = None
    if PAPER_OK:
        try:
            paper_trade.evaluate()
            paper_trade.open_positions(sigs, max_open=paper_trade.MAX_OPEN)
            paper = paper_trade.summary()
        except Exception as e:
            print(f"  ⚠ Paper trade atlandı: {e!r}")
    else:
        print("paper_trade.py bulunamadı — paper bölümü atlandı.")

    print("4/4) HTML oluşturuluyor...", flush=True)

    blocks, first_js = [], True
    def add_fig(fig):
        nonlocal first_js
        blocks.append(fig.to_html(full_html=False, include_plotlyjs=first_js))
        first_js = False

    # ---- Paper equity eğrisi (en üstte — ana hikaye) ----
    if paper and len(paper["equity"]) >= 2:
        eq_df = pd.DataFrame(paper["equity"], columns=["t", "bakiye"])
        fig_eq = go.Figure(go.Scatter(x=eq_df["t"], y=eq_df["bakiye"],
                                      mode="lines+markers", line=dict(color="#26a65b", width=2),
                                      fill="tozeroy", name="Bakiye"))
        fig_eq.add_hline(y=paper["start_balance"], line_dash="dot", line_color="#8ea0c9",
                         annotation_text=f"Başlangıç {paper['start_balance']:.0f}")
        fig_eq.update_layout(template="plotly_dark", height=320,
                             title=f'💼 Paper Portföy — {paper["balance"]:.2f} USDT '
                                   f'({paper["pnl_total"]:+.2f} / %{paper["pnl_pct"]:+.2f})',
                             margin=dict(l=10, r=10, t=60, b=10))
        add_fig(fig_eq)

    if bt:
        rows = [{"Coin": sym, "Strateji": scanner.STRAT_LABELS.get(lab, lab), "WR": m["wr"]}
                for sym, ss in bt.items() for lab, m in ss.items() if m["trades"] >= 5]
        if rows:
            piv = pd.DataFrame(rows).pivot(index="Coin", columns="Strateji", values="WR")
            add_fig(px.imshow(piv, text_auto=".0f", color_continuous_scale="RdYlGn",
                              aspect="auto", template="plotly_dark",
                              title="🔥 Coin × Strateji Win Rate (%)"))
        rows2 = [dict(Strateji=scanner.STRAT_LABELS.get(lab, lab), WR=m["wr"], PF=m["pf"],
                      Sharpe=m["sharpe"], İşlem=m["trades"])
                 for ss in bt.values() for lab, m in ss.items()]
        if rows2:
            agg = pd.DataFrame(rows2).groupby("Strateji").agg(
                WR=("WR", "mean"), PF=("PF", "mean"), Sharpe=("Sharpe", "mean"),
                Toplamİşlem=("İşlem", "sum")).round(2)
            add_fig(px.bar(agg.reset_index(), x="Strateji", y="WR", color="WR",
                           color_continuous_scale="RdYlGn", template="plotly_dark",
                           title="📊 Strateji başına ortalama Win Rate %"))

    for s in sigs[:12]:
        add_fig(rich_chart(s))

    # ---------- Strateji özet tablosu ----------
    sum_html = ""
    if bt:
        rows = []
        for lab in next(iter(bt.values())).keys():
            ms = [m[lab] for m in bt.values() if m.get(lab, {}).get("trades", 0) > 0]
            if not ms:
                continue
            tot = sum(m["trades"] for m in ms)
            wr  = sum(m["wr"] * m["trades"] for m in ms) / tot
            pfs = sorted(m["pf"] for m in ms)
            shr = sum(m["sharpe"] * m["trades"] for m in ms) / tot
            rows.append(dict(Strateji=scanner.STRAT_LABELS.get(lab, lab), İşlem=tot,
                             WR=round(wr, 1), MedyanPF=round(pfs[len(pfs)//2], 2),
                             Sharpe=round(shr, 2)))
        if rows:
            head = "".join(f"<th>{h}</th>" for h in rows[0].keys())
            body = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r.values()) + "</tr>" for r in rows)
            sum_html = f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

    # ---------- Sinyal tablosu ----------
    thead = "".join(f"<th>{h}</th>" for h in
                    ["Parite", "Strateji", "Yön", "Nihai", "Canlı", "BT", "WR%", "PF",
                     "Sharpe", "İşlem", "RR", "Giriş", "SL", "TP"])
    trs = []
    for s in sigs[:25]:
        trs.append(
            f'<tr><td><b>{s["symbol"]}</b></td><td>{s["strategy"]}</td>'
            f'<td class="{"L" if s["direction"]=="LONG" else "S"}">'
            f'{"🟢 LONG" if s["direction"]=="LONG" else "🔴 SHORT"}</td>'
            f'<td><b>{s["final"]}</b></td><td>{s["score"]}</td>'
            f'<td>{s["bt_score"] if s["bt_score"] else "—"}</td>'
            f'<td>{s["m"]["wr"] if s["m"] else "—"}</td>'
            f'<td>{s["m"]["pf"] if s["m"] else "—"}</td>'
            f'<td>{s["m"]["sharpe"] if s["m"] else "—"}</td>'
            f'<td>{s["m"]["trades"] if s["m"] else "—"}</td>'
            f'<td>1:{s["rr"]}</td><td>{s["entry"]:.6g}</td>'
            f'<td class="S">{s["sl"]:.6g}</td><td class="L">{s["tp"]:.6g}</td></tr>')
    table = (f'<table><thead><tr>{thead}</tr></thead><tbody>{"".join(trs)}</tbody></table>'
             if trs else '<p class="empty">BT kapısını geçen sinyal yok — disiplinli boşluk.</p>')

    # ---------- Paper bölümü (bakiye + istatistik + tablolar) ----------
    paper_html = '<p class="empty">Paper verisi yok — paper_trade.py eksik ya da henüz pozisyon açılmadı.</p>'
    if paper:
        head_p = "".join(f"<th>{h}</th>" for h in
                         ["Kapanan", "WR", "PF", "Expectancy", "Bakiye", "PnL USDT", "PnL %"])
        row_p = (f'<tr><td>{paper["n_closed"]}</td>'
                 f'<td>{paper["wr"] if paper["wr"] is not None else "—"}%</td>'
                 f'<td>{paper["pf"]}</td><td>{paper["exp_r"]:+.3f}R</td>'
                 f'<td><b>{paper["balance"]:.2f}</b></td>'
                 f'<td class="{"L" if paper["pnl_total"]>=0 else "S"}">{paper["pnl_total"]:+.2f}</td>'
                 f'<td class="{"L" if paper["pnl_total"]>=0 else "S"}">%{paper["pnl_pct"]:+.2f}</td></tr>')
        stats_tbl = f'<table><thead><tr>{head_p}</tr></thead><tbody>{row_p}</tbody></table>'

        oh = "".join(f"<th>{h}</th>" for h in
                     ["Parite", "Strateji", "Yön", "Risk USDT", "Giriş", "SL", "TP",
                      "Açılış", "%50 alındı", "Geçen mum"])
        ob = "".join(
            f'<tr><td><b>{p["symbol"]}</b></td><td>{p["strategy"]}</td>'
            f'<td class="{"L" if p["direction"]=="LONG" else "S"}">{p["direction"]}</td>'
            f'<td>{p.get("risk_usdt", "—")}</td>'
            f'<td>{p["entry"]:.6g}</td><td class="S">{p["sl"]:.6g}</td>'
            f'<td class="L">{p["tp"]:.6g}</td><td>{p["opened"]}</td>'
            f'<td>{"✔" if p["half"] else "—"}</td><td>{p["bars"]}/96</td></tr>'
            for p in paper["open"])
        ch = "".join(f"<th>{h}</th>" for h in
                     ["Parite", "Strateji", "Yön", "Sonuç", "R", "PnL USDT",
                      "Bakiye sonrası", "Kapanış"])
        cb = "".join(
            f'<tr><td><b>{c["symbol"]}</b></td><td>{c["strategy"]}</td>'
            f'<td class="{"L" if c["direction"]=="LONG" else "S"}">{c["direction"]}</td>'
            f'<td class="{"L" if c["result_r"]>0 else "S"}">{c["result_label"]}</td>'
            f'<td class="{"L" if c["result_r"]>0 else "S"}"><b>{c["result_r"]:+.2f}R</b></td>'
            f'<td class="{"L" if c["pnl_usdt"]>=0 else "S"}">{c["pnl_usdt"]:+.2f}</td>'
            f'<td>{c["balance_after"]:.2f}</td><td>{c["closed"]}</td></tr>'
            for c in paper["closed"])
        paper_html = (stats_tbl +
                      f'<table style="margin-top:14px"><thead><tr>{oh}</tr></thead><tbody>{ob}</tbody></table>' +
                      f'<table style="margin-top:14px"><thead><tr>{ch}</tr></thead><tbody>{cb}</tbody></table>')

    # ---------- Journal ----------
    journals = "".join(f'<pre>{scanner.journal(s)}</pre>' for s in sigs[:10])

    # ---------- KPI'lar ----------
    longs = [s for s in sigs if s["direction"] == "LONG"]
    kpis = [kpi_html("Fırsat (BT-doğrulanmış)", len(sigs)),
            kpi_html("LONG / SHORT", f'{len(longs)} / {len(sigs)-len(longs)}', "#26a65b")]
    if sigs:
        kpis.append(kpi_html("Ort. RR", f'1:{round(sum(s["rr"] for s in sigs)/len(sigs), 2)}'))
    if bt:
        pairs = [(m, lab) for v in bt.values() for lab, m in v.items()
                 if m["trades"] >= BT_GATE_TRADES]
        if pairs:
            best = max(pairs, key=lambda x: x[0]["score"])
            kpis.append(kpi_html("Backtest WR (genel)",
                                 f'{round(sum(x[0]["wr"] for x in pairs)/len(pairs), 1)}%'))
            kpis.append(kpi_html("En iyi strateji (BT)",
                                 scanner.STRAT_LABELS.get(best[1], best[1])))
    if paper:
        col = "#26a65b" if paper["pnl_total"] >= 0 else "#e14b5a"
        kpis.append(kpi_html("Paper Bakiye", f'{paper["balance"]:.0f}', col))
        kpis.append(kpi_html("Paper PnL", f'{paper["pnl_total"]:+.0f} (%{paper["pnl_pct"]:+.1f})', col))
        if paper["n_closed"]:
            kpis.append(kpi_html("Paper WR", f'{paper["wr"]}%', col))
    kpis.append(kpi_html("Notion",
                         f"Yazıldı ✓ ({notion_written})" if notion_written else
                         ("Kapalı" if not NOTION_ENABLED else "Yeni yazılmadı"),
                         "#26a65b" if notion_written else "#8ea0c9"))

    html = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<title>SMC Rapor — {datetime.now():%d.%m.%Y %H:%M}</title><style>
body{{background:radial-gradient(1200px 600px at 70% -10%,#16213e 0%,#0b0e17 55%);
     color:#eaf0ff;font-family:Segoe UI,Arial,sans-serif;margin:24px}}
h1{{font-size:1.5rem}} h2{{color:#8ea0c9;font-size:1.05rem;margin-top:34px}}
.kpi{{background:linear-gradient(135deg,#121a2e,#0f1526);border:1px solid #22305c;
     border-radius:16px;padding:14px;text-align:center;display:inline-block;
     margin:4px;min-width:150px}}
.kpi small{{color:#8ea0c9;display:block}} .kpi b{{font-size:1.4rem}}
table{{border-collapse:collapse;width:100%;font-size:.85rem;margin-top:10px}}
th,td{{border:1px solid #1f2b4a;padding:6px 8px;text-align:center}}
th{{background:#121a2e;color:#8ea0c9}}
td.L{{color:#26a65b}} td.S{{color:#e14b5a}}
tr:nth-child(even){{background:#0f1526}}
pre{{background:#0f1526;border:1px solid #22305c;border-radius:12px;
    padding:14px;overflow-x:auto;font-size:.8rem;line-height:1.5}}
.empty{{background:#121a2e;border:1px solid #22305c;border-radius:12px;padding:16px}}
</style></head><body>
<h1>🎯 SMC Terminal Raporu — {datetime.now():%d.%m.%Y %H:%M}</h1>
<div>{''.join(kpis)}</div>
<h2>💼 Paper Portföy İstatistiği (bakiye takipli simülasyon — %1 risk/işlem)</h2>
{paper_html}
<h2>📊 Strateji Özeti (sinyal veren coinler, işlem-ağırlıklı)</h2>
{sum_html if sum_html else '<p class="empty">Backtest atlandı veya veri yok.</p>'}
<h2>🎯 Sinyaller — hepsi backtest kapısından geçti</h2>
{table}
<h2>📈 Grafikler & Analiz — equity eğrisi + sinyal gerekçeleri işaretli</h2>
{''.join(blocks)}
<h2>📝 İlk 10 İşlem — Trade Journal (kısmi TP planlı)</h2>
{journals}
</body></html>"""

    out = Path("smc_rapor.html").resolve()
    out.write_text(html, encoding="utf-8")
    print(f"✅ Rapor hazır: {out}")
    webbrowser.open(out.as_uri())

if __name__ == "__main__":
    run()