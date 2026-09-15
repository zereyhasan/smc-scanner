"""Backtest v9.2: derin araştırma + geçmiş arşivi + HTML özet raporu.
Her derin koşuda üç çıktı:
  1) backtest_arsiv/YYYY-MM-DD_HHMM.json  → kalıcı geçmiş (asla ezilmez)
  2) backtest_sonuc.json                  → son koşu (kapı/analiz bunu okur)
  3) backtest_rapor.html                  → aç-bak özet (kırılımlar, renk kodlu)
Günlük akış (report.py) aynı API'yi kullanır; derin çalıştırma: python backtest.py"""
import sys, json
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import scanner

FEE_RT   = 0.001
WARMUP   = 220
MAX_BARS = 96
PARTIAL_TP = True
DEFAULT_COINS = 50
DEFAULT_BARS  = 6000
ARCHIVE_DIR = "backtest_arsiv"

_CSS = """
body{background:radial-gradient(1200px 600px at 70% -10%,#16213e 0%,#0b0e17 55%);
     color:#eaf0ff;font-family:Segoe UI,Arial,sans-serif;margin:24px}
h1{font-size:1.4rem} h2{color:#8ea0c9;font-size:1.05rem;margin-top:30px}
.kpi{background:linear-gradient(135deg,#121a2e,#0f1526);border:1px solid #22305c;
     border-radius:16px;padding:14px;text-align:center;display:inline-block;
     margin:4px;min-width:150px}
.kpi small{color:#8ea0c9;display:block} .kpi b{font-size:1.35rem}
table{border-collapse:collapse;width:100%;font-size:.82rem;margin-top:10px}
th,td{border:1px solid #1f2b4a;padding:5px 7px;text-align:center}
th{background:#121a2e;color:#8ea0c9}
td.good{background:rgba(38,166,91,.16);color:#26a65b;font-weight:600}
td.warn{background:rgba(245,197,66,.12);color:#f5c542}
td.bad{background:rgba(225,75,90,.12);color:#e14b5a}
td.dim{color:#5a6a90}
.note{color:#8ea0c9;font-size:.85rem;margin-top:18px}
"""

# ---------------- Metrikler ----------------
def _metrics(rs: np.ndarray) -> dict:
    if len(rs) < 3:
        return dict(trades=len(rs), wr=0.0, pf=0.0, sharpe=0.0,
                    mdd=0.0, exp=0.0, equity=[0.0])
    wr   = float((rs > 0).mean() * 100)
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum()
    pf   = min(gain / loss, 9.99) if loss > 0 else 9.99
    sh   = float(rs.mean() / rs.std()) if rs.std() > 0 else 0.0
    eq   = rs.cumsum()
    mdd  = float((np.maximum.accumulate(eq) - eq).max())
    return dict(trades=len(rs), wr=round(wr, 1), pf=round(pf, 2),
                sharpe=round(sh, 2), mdd=round(mdd, 2),
                exp=round(float(rs.mean()), 3), equity=eq.tolist())

def backtest_score(m: dict) -> int:
    if not m or m["trades"] < 8:
        return 0
    s  = 40 * min(m["wr"], 65) / 65
    s += 30 * min(m["pf"], 2.5) / 2.5
    s += 15 * max(min(m["sharpe"], .30), 0) / .30
    s += 15 * min(m["trades"], 60) / 60
    return round(s)

def _session(ts) -> str:
    """UTC saate göre seans: 00-08 Asya, 08-16 Avrupa, 16-24 ABD"""
    h = ts.hour if hasattr(ts, "hour") else 0
    return "Asya" if h < 8 else ("Avrupa" if h < 16 else "ABD")

# ---------------- Çekirdek simülasyon ----------------
def backtest_symbol(symbol: str, bars: int = DEFAULT_BARS) -> dict:
    ltf = scanner.klines_multi(symbol, "15m", total=bars)
    htf = scanner.klines(symbol, "1h",  limit=600)
    if len(ltf) < WARMUP + 50 or len(htf) < 210:
        return {}
    rank, cat = scanner.MCAP_RANK.get(symbol, (None, "—"))
    trades, detail = {f.__name__: [] for f in scanner.STRATS}, {}
    state = {}
    for i in range(WARMUP, len(ltf)):
        bar = ltf.iloc[i]
        for name, p in list(state.items()):
            d, entry, sl, tp, risk, i0, cost, half, etime = p
            r1 = entry + risk if d == "LONG" else entry - risk
            if d == "LONG":
                hit_sl = bar["low"] <= sl
                hit_r1 = (not half) and bar["high"] >= r1
                hit_tp = bar["high"] >= tp
            else:
                hit_sl = bar["high"] >= sl
                hit_r1 = (not half) and bar["low"]  <= r1
                hit_tp = bar["low"]  <= tp
            if hit_sl:
                r = (0.5 if half else -1.0)
                net, exit_t = r - cost, bar["t"]
            elif PARTIAL_TP and hit_r1:
                state[name] = (d, entry, entry, tp, risk, i0, cost, True, etime)
                continue
            elif hit_tp:
                raw = (tp - entry) / risk if d == "LONG" else (entry - tp) / risk
                r = (0.5 + 0.5 * raw) if half else raw
                net, exit_t = r - cost, bar["t"]
            elif i - i0 >= MAX_BARS:
                px = float(bar["close"])
                raw = (px - entry) / risk if d == "LONG" else (entry - px) / risk
                r = (0.5 + 0.5 * raw) if half else raw
                net, exit_t = r - cost, bar["t"]
            else:
                continue
            trades[name].append(net)
            detail.setdefault(name, []).append(dict(
                symbol=symbol, kategori=cat,
                strateji=scanner.STRAT_LABELS.get(name, name),
                yon=d, seans=_session(etime),
                giris=str(etime), cikis=str(exit_t),
                rr_net=round(float(net), 3)))
            del state[name]
        if len(state) < len(scanner.STRATS):
            htf_s = htf[htf["t"] <= bar["t"]].tail(250)
            if len(htf_s) < 210:
                continue
            ctx = scanner.build_context(symbol, htf_s, ltf.iloc[:i + 1])
            for fn in scanner.STRATS:
                if fn.__name__ in state:
                    continue
                try:
                    sig = fn(ctx)
                except Exception:
                    sig = None
                if sig:
                    risk = abs(sig["entry"] - sig["sl"])
                    if risk <= 0:
                        continue
                    cost = FEE_RT * sig["entry"] / risk
                    state[fn.__name__] = (sig["direction"], sig["entry"], sig["sl"],
                                          sig["tp"], risk, i, cost, False, sig["time"])
    out = {}
    for fn in scanner.STRATS:
        m = _metrics(np.array(trades[fn.__name__]))
        m["score"] = backtest_score(m)
        m["detail"] = detail.get(fn.__name__, [])
        out[fn.__name__] = m
    return out

def _agg_str(rs):
    """Terminal için sabit genişlikli özet"""
    rs = np.array(rs) if len(rs) else np.array([])
    if len(rs) == 0:
        return "     —      "
    wr  = 100 * float((rs > 0.05).mean())
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum()
    pf  = min(gain / loss, 9.99) if loss > 0 else 9.99
    return f"n={len(rs):3d} WR={wr:4.1f}% PF={pf:4.2f}"

def _agg_html(rs):
    """HTML için renk kodlu hücre: PF ≥1.2 yeşil, 1.0-1.2 sarı, <1.0 kırmızı"""
    rs = np.array(rs) if len(rs) else np.array([])
    if len(rs) == 0:
        return '<td class="dim">—</td>'
    n   = len(rs)
    wr  = 100 * float((rs > 0.05).mean())
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum()
    pf  = min(gain / loss, 9.99) if loss > 0 else 9.99
    cls = "good" if pf >= 1.2 else ("warn" if pf >= 1.0 else "bad")
    return (f'<td class="{cls}">n={n}<br>WR {wr:.0f}%<br>PF {pf:.2f}</td>')

def backtest_all(n_coins=DEFAULT_COINS, bars=DEFAULT_BARS, workers=8, symbols=None) -> dict:
    scanner.ensure_mcap()
    syms = symbols if symbols else scanner.universe(n_coins)
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(backtest_symbol, s, bars): s for s in syms}
        for k, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                out[futs[f]] = r
            print(f"\rBacktest {k}/{len(syms)}", end="", flush=True)
    print()
    if out:
        print("\n--- STRATEJİ ÖZETİ (tüm test edilen coinler) ---")
        labs = next(iter(out.values())).keys()
        for lab in labs:
            ms = [m[lab] for m in out.values() if m.get(lab, {}).get("trades", 0) > 0]
            if not ms:
                continue
            tot = sum(m["trades"] for m in ms)
            wr  = sum(m["wr"] * m["trades"] for m in ms) / tot
            pfs = sorted(m["pf"] for m in ms)
            print(f"  {scanner.STRAT_LABELS.get(lab, lab):32s} "
                  f"işlem={tot:4d}  WR={wr:5.1f}%  medyanPF={pfs[len(pfs)//2]:.2f}")
    return out

# ---------------- Kırılım yardımcıları ----------------
def _collect_trades(out: dict) -> list:
    rows = []
    for sym, ss in out.items():
        for lab, m in ss.items():
            rows.extend(m.get("detail", []))
    return rows

def _breakdown(trades: list, key: str) -> dict:
    grid = {}
    for t in trades:
        grid.setdefault(t[key], {}).setdefault(t["strateji"], []).append(t["rr_net"])
    return grid

def _strategy_summary_rows(out: dict) -> list:
    rows = []
    labs = next(iter(out.values())).keys()
    for lab in labs:
        ms = [m[lab] for m in out.values() if m.get(lab, {}).get("trades", 0) > 0]
        if not ms:
            continue
        tot = sum(m["trades"] for m in ms)
        wr  = sum(m["wr"] * m["trades"] for m in ms) / tot
        pfs = sorted(m["pf"] for m in ms)
        rows.append(dict(Strateji=scanner.STRAT_LABELS.get(lab, lab), İşlem=tot,
                         WR=round(wr, 1), MedyanPF=round(pfs[len(pfs) // 2], 2)))
    return rows

def _breakdown_table_html(trades: list, key: str, order: list) -> str:
    grid = _breakdown(trades, key)
    if not grid:
        return ""
    strats = sorted({t["strateji"] for t in trades})
    head = "<th>Grup</th>" + "".join(f"<th>{s.split(' (')[0]}</th>" for s in strats)
    rows = []
    for g in order:
        if g not in grid:
            continue
        cells = [f"<td><b>{g}</b></td>"]
        cells.extend(_agg_html(grid[g].get(s, [])) for s in strats)
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        return ""
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'

def _coin_table_html(out: dict) -> str:
    """Coin × Strateji detayı — 'hangi coin hangi stratejide çalışmış' arama tablosu"""
    labs = list(next(iter(out.values())).keys())
    head = ("<th>Parite</th><th>MCAP</th><th>Kategori</th>" +
            "".join(f"<th>{scanner.STRAT_LABELS.get(l, l).split(' (')[0]}</th>" for l in labs))
    rows = []
    for sym in sorted(out, key=lambda s: scanner.MCAP_RANK.get(s, (999, ""))[0]):
        rank, cat = scanner.MCAP_RANK.get(sym, (None, "—"))
        cells = [f"<td class='left'><b>{sym}</b></td>",
                 f"<td>{rank if rank else '—'}</td><td>{cat}</td>"]
        for l in labs:
            m = out[sym].get(l, {})
            n = m.get("trades", 0)
            if n >= 8:
                cls = "good" if m["pf"] >= 1.2 else ("warn" if m["pf"] >= 1.0 else "bad")
                cells.append(f'<td class="{cls}">n={n}<br>PF {m["pf"]:.2f}</td>')
            elif n > 0:
                cells.append(f'<td class="dim">n={n}<br>az örneklem</td>')
            else:
                cells.append('<td class="dim">—</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'

def _research_html(trades, out, n, bars, ts_str, arch_name) -> str:
    rs = np.array([t["rr_net"] for t in trades]) if trades else np.array([])
    gen_wr = 100 * float((rs > 0.05).mean()) if len(rs) else 0.0
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum() if len(rs) else (0, 0)
    if len(rs) and loss > 0:
        gen_pf = min(gain / loss, 9.99)
    elif len(rs) and gain > 0:
        gen_pf = 9.99
    else:
        gen_pf = 0.0

    kpis = (f'<div class="kpi"><small>Koşu zamanı</small><b style="font-size:1.1rem">{ts_str}</b></div>'
            f'<div class="kpi"><small>Coin / Bar</small><b>{len(out)} / {bars}</b></div>'
            f'<div class="kpi"><small>Toplam işlem</small><b>{len(rs)}</b></div>'
            f'<div class="kpi"><small>Genel WR</small><b>{gen_wr:.1f}%</b></div>'
            f'<div class="kpi"><small>Genel PF</small><b>{gen_pf:.2f}</b></div>')

    sum_rows = _strategy_summary_rows(out)
    sum_html = ""
    if sum_rows:
        head = "".join(f"<th>{h}</th>" for h in sum_rows[0].keys())
        body = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r.values()) + "</tr>"
                       for r in sum_rows)
        sum_html = f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

    kat_html = _breakdown_table_html(trades, "kategori", ["Majör", "Large", "Mid", "—"])
    ses_html = _breakdown_table_html(trades, "seans", ["Asya", "Avrupa", "ABD"])
    coin_html = _coin_table_html(out) if out else ""

    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<title>SMC Derin Backtest — {ts_str}</title><style>{_CSS}</style></head><body>
<h1>🔬 SMC Derin Backtest Raporu</h1>
<div>{kpis}</div>
<h2>📊 Strateji Özeti (işlem-ağırlıklı)</h2>
{sum_html if sum_html else '<p>Veri yok.</p>'}
<h2>🏷️ Kategori × Strateji</h2>
{kat_html if kat_html else '<p>Veri yok.</p>'}
<h2>🕐 Seans × Strateji (UTC saatine göre)</h2>
{ses_html if ses_html else '<p>Veri yok.</p>'}
<h2>🪙 Coin × Strateji Detayı (MCAP sırasına göre)</h2>
{coin_html}
<p class="note">Renk kodu: yeşil PF≥1.2 · sarı 1.0-1.2 · kırmızı &lt;1.0 · gri = az örneklem (&lt;8 işlem).<br>
Arşiv: {arch_name} — kalıcı geçmiş. Karar kayıtları Notion → Hipotez Bankası'na elle yazılır.</p>
</body></html>"""

def research(n=DEFAULT_COINS, bars=DEFAULT_BARS):
    """Derin araştırma: koş → terminal tabloları → arşiv JSON + HTML özeti yaz."""
    print(f"🔬 DERİN ARAŞTIRMA: {n} coin (MCAP) × {bars} bar (~10-15 dk)...\n", flush=True)
    out = backtest_all(n_coins=n, bars=bars)
    trades = _collect_trades(out)
    print(f"\nToplam işlem: {len(trades)}\n")

    print("--- KATEGORİ × STRATEJİ ---")
    grid = _breakdown(trades, "kategori")
    strats = sorted({t["strateji"] for t in trades})
    for cat in ["Majör", "Large", "Mid", "—"]:
        if cat not in grid:
            continue
        cells = " | ".join(f"{s.split(' (')[0]:>24s}: {_agg_str(grid[cat].get(s, []))}"
                           for s in strats)
        print(f"  {cat:6s} {cells}")

    print("\n--- SEANS × STRATEJİ ---")
    grid = _breakdown(trades, "seans")
    for ses in ["Asya", "Avrupa", "ABD"]:
        if ses not in grid:
            continue
        cells = " | ".join(f"{s.split(' (')[0]:>24s}: {_agg_str(grid[ses].get(s, []))}"
                           for s in strats)
        print(f"  {ses:6s} {cells}")

    # ---------- Çıktılar: son-durum + arşiv + HTML ----------
    ts = datetime.now()
    ts_str = ts.strftime("%d.%m.%Y %H:%M")
    arch_name = f"{ts:%Y-%m-%d_%H%M}.json"

    coins = {sym: dict(rank=scanner.MCAP_RANK.get(sym, (None, "—"))[0],
                       kategori=scanner.MCAP_RANK.get(sym, (None, "—"))[1])
             for sym in out}
    payload = dict(generated=str(ts), coins=n, bars=bars,
                   coin_meta=coins, trades=trades)

    Path("backtest_sonuc.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    arch_dir = Path(ARCHIVE_DIR)
    arch_dir.mkdir(exist_ok=True)
    (arch_dir / arch_name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    html = _research_html(trades, out, n, bars, ts_str, f"{ARCHIVE_DIR}/{arch_name}")
    Path("backtest_rapor.html").write_text(html, encoding="utf-8")

    print(f"\n✅ Son durum    : backtest_sonuc.json")
    print(f"✅ Arşiv        : {ARCHIVE_DIR}/{arch_name}  (kalıcı geçmiş)")
    print(f"✅ HTML özet    : backtest_rapor.html")
    print(f"ℹ  Karar kayıtları → Notion Hipotez Bankası (elle)")

if __name__ == "__main__":
    n    = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COINS
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BARS
    research(n, bars)