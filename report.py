"""v5: Tek tarama → HTML rapor + Notion (senkron akış).
Hızlı mod:  python report.py hizli   (backtest yok, Notion kapıyı atlar)
Normal   :  python report.py         (backtest + BT kapısı + Notion yazımı)"""
import sys, webbrowser
from pathlib import Path
from datetime import datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import scanner, backtest

# ---- Notion modülü: token girilmişse aktif, değilse rapor yine çalışır ----
NOTION_ENABLED = False
try:
    import notion
    NOTION_ENABLED = "TOKENIN_BURAYA" not in notion.TOKEN
except Exception:
    pass

BT_GATE_TRADES = 8
BT_GATE_PF     = 1.2

def kpi_html(label, val, color="#eaf0ff"):
    return (f'<div class="kpi"><small>{label}</small>'
            f'<b style="color:{color}">{val}</b></div>')

def candle_figure(s):
    ltf = scanner.klines(s["symbol"], "15m", 200)
    htf = scanner.klines(s["symbol"], "1h", 300)
    ctx = scanner.build_context(s["symbol"], htf, ltf)
    fig = go.Figure(go.Candlestick(x=ltf["t"], open=ltf["open"], high=ltf["high"],
                                   low=ltf["low"], close=ltf["close"], name=s["symbol"]))
    t0, t1 = ltf["t"].iloc[0], ltf["t"].iloc[-1]
    for ob in [o for o in ctx["obs"] if not o["mitigated"]][-4:]:
        fig.add_shape(type="rect", x0=t0, x1=t1, y0=ob["bottom"], y1=ob["top"],
                      fillcolor="rgba(38,166,91,.20)" if ob["type"] == "bullish" else "rgba(225,75,90,.20)",
                      line=dict(width=0), layer="below")
    for f in [f for f in ctx["fvgs"] if not f["filled"]][-4:]:
        fig.add_shape(type="rect", x0=t0, x1=t1, y0=f["bottom"], y1=f["top"],
                      fillcolor="rgba(90,140,255,.15)", line=dict(width=0), layer="below")
    for y, c, txt in [(s["entry"], "#f5c542", "GİRİŞ"), (s["sl"], "#e14b5a", "SL"),
                      (s["tp"], "#26a65b", "TP")]:
        fig.add_hline(y=y, line_dash="dash", line_color=c, annotation_text=txt)
    fig.update_layout(template="plotly_dark", height=430,
                      title=f'{s["symbol"]} — {s["strategy"]} ({s["direction"]})',
                      xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=50, b=10))
    return fig

def run():
    print("1/4) Canlı tarama (100 coin)...", flush=True)
    res = scanner.scan(100)

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

    # ---- 3/4: Notion'a yaz (rapor sinyalleriyle AYNI liste) ----
    print("3/4) Notion'a yazılıyor...", flush=True)
    if NOTION_ENABLED and sigs:
        notion.write_signals(sigs, top=10)
    elif not NOTION_ENABLED:
        print("Notion kapalı (token girilmemiş) — sadece HTML rapor üretilecek.")
    else:
        print("Notion: kapıdan geçen sinyal yok, yazılacak bir şey bulunamadı.")

    print("4/4) HTML oluşturuluyor...", flush=True)

    # ---------- Grafikler ----------
    blocks, first_js = [], True
    def add_fig(fig):
        nonlocal first_js
        blocks.append(fig.to_html(full_html=False, include_plotlyjs=first_js))
        first_js = False

    if bt:
        rows = [{"Coin": sym, "Strateji": scanner.STRAT_LABELS.get(lab, lab), "WR": m["wr"]}
                for sym, ss in bt.items() for lab, m in ss.items() if m["trades"] >= 5]
        if rows:
            piv = pd.DataFrame(rows).pivot(index="Coin", columns="Strateji", values="WR")
            add_fig(px.imshow(piv, text_auto=".0f", color_continuous_scale="RdYlGn",
                              aspect="auto", template="plotly_dark",
                              title="🔥 Coin × Strateji Win Rate (%) — yeşil = geçmişte çalıştı"))
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

    for s in sigs[:3]:
        add_fig(candle_figure(s))

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
             if trs else '<p class="empty">BT kapısını geçen sinyal yok — bugün için '
                         'disiplinli boşluk (bu da bir sinyaldir: zorla işlem açma).</p>')

    # ---------- Journal (ilk 10) ----------
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
    else:
        kpis.append(kpi_html("Backtest", "Atlandı (hızlı mod)", "#8ea0c9"))
    kpis.append(kpi_html("Notion", "Yazıldı ✓" if (NOTION_ENABLED and sigs) else
                         ("Kapalı" if not NOTION_ENABLED else "Sinyal yok"), "#8ea0c9"))

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
<h2>📊 Strateji Özeti (sinyal veren coinler, işlem-ağırlıklı)</h2>
{sum_html if sum_html else '<p class="empty">Backtest atlandı veya veri yok.</p>'}
<h2>🎯 Sinyaller — hepsi backtest kapısından geçti</h2>
{table}
<h2>📈 Grafikler & Analiz</h2>
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