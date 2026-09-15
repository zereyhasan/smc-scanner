"""Backtest v9.4: derin araştırma + arşiv + HTML + PENDING simülasyonu + YOLCULUK VERİSİ.
v9.4 yenilikleri:
1) Pending sayaç FIX: 'orders' artık sinyal üretilirken sayılır → gerçek dolma oranı görünür
2) Her işlem için YOLCULUK verisi: mfe_r (en iyi R), mae_r (en kötü R), exit_reason,
   bars_held, be_moved → 'MFE≥2R ama TP'ye varamayan' analizi artık mümkün
3) Terminal + HTML'e 'Yolculuk Analizi' tablosu (strateji × MFE kırılımı)
Kullanım: python backtest.py [coins] [bars]"""
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
    h = ts.hour if hasattr(ts, "hour") else 0
    return "Asya" if h < 8 else ("Avrupa" if h < 16 else "ABD")

def backtest_symbol(symbol: str, bars: int = DEFAULT_BARS) -> dict:
    ltf = scanner.klines_multi(symbol, "15m", total=bars)
    htf = scanner.klines(symbol, "1h",  limit=600)
    if len(ltf) < WARMUP + 50 or len(htf) < 210:
        return {}
    rank, cat = scanner.MCAP_RANK.get(symbol, (None, "—"))
    trades, detail = {f.__name__: [] for f in scanner.STRATS}, {}
    pend_meta = {}   # name -> dict(orders, filled)
    state = {}
    for i in range(WARMUP, len(ltf)):
        bar = ltf.iloc[i]
        # ---------- 1) Açık pozisyonları yönet ----------
        for name, p in list(state.items()):
            d, entry, sl, tp, risk, i0, cost, half, etime, run = p
            r1 = entry + risk if d == "LONG" else entry - risk
            if d == "LONG":
                hit_sl = bar["low"] <= sl
                hit_r1 = (not half) and bar["high"] >= r1
                hit_tp = bar["high"] >= tp
            else:
                hit_sl = bar["high"] >= sl
                hit_r1 = (not half) and bar["low"]  <= r1
                hit_tp = bar["low"]  <= tp
            # --- yolculuk takibi: MFE / MAE (bu barda görülen ekstremumlar) ---
            if d == "LONG":
                mfe_bar = (bar["high"] - entry) / risk
                mae_bar = (bar["low"]  - entry) / risk
            else:
                mfe_bar = (entry - bar["low"])  / risk
                mae_bar = (entry - bar["high"]) / risk
            run["mfe"] = max(run["mfe"], mfe_bar)
            run["mae"] = min(run["mae"], mae_bar)
            run["bars"] += 1
            # --- BE taşıma: %50 @1R ---
            if PARTIAL_TP and hit_r1 and not half:
                half = True
                run["r_at_partial"] = r1
                run["be_moved"] = True
                state[name] = (d, entry, entry, tp, risk, i0, cost, True, etime, run)
                continue
            exit_reason = None
            if hit_sl:
                r = (0.5 if half else -1.0)
                cp, exit_t, exit_reason = sl, bar["t"], "SL"
            elif hit_tp:
                raw = (tp - entry) / risk if d == "LONG" else (entry - tp) / risk
                r = (0.5 + 0.5 * raw) if half else raw
                cp, exit_t, exit_reason = tp, bar["t"], "TP"
            elif i - i0 >= MAX_BARS:
                px = float(bar["close"])
                raw = (px - entry) / risk if d == "LONG" else (entry - px) / risk
                r = (0.5 + 0.5 * raw) if half else raw
                cp, exit_t, exit_reason = px, bar["t"], "TIMEOUT"
            else:
                continue
            net = r - cost
            trades[name].append(net)
            detail.setdefault(name, []).append(dict(
                symbol=symbol, kategori=cat,
                strateji=scanner.STRAT_LABELS.get(name, name),
                yon=d, seans=_session(etime),
                giris=str(etime), cikis=str(exit_t),
                rr_net=round(float(net), 3),
                mfe_r=round(float(run["mfe"]), 3),
                mae_r=round(float(run["mae"]), 3),
                be_moved=bool(run.get("be_moved")),
                exit_reason=exit_reason,
                bars_held=int(run["bars"])))
            del state[name]
        # ---------- 2) Yeni sinyaller ----------
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
                if not sig:
                    continue
                risk = abs(sig["entry"] - sig["sl"])
                if risk <= 0:
                    continue
                pm = pend_meta.setdefault(fn.__name__, dict(orders=0, filled=0))
                if sig.get("pending"):
                    pm["orders"] += 1                     # FIX: sinyal üretilirken say
                    exp = sig.get("expiry_bars", 96)
                    limit_px = sig["entry"]
                    filled_i = None
                    for j in range(i, min(i + exp, len(ltf))):
                        b = ltf.iloc[j]
                        if sig["direction"] == "LONG":
                            if b["low"] <= sig["sl"]:
                                break                      # SL bölgesi görüldü → iptal
                        else:
                            if b["high"] >= sig["sl"]:
                                break
                        if b["low"] <= limit_px <= b["high"]:
                            filled_i = j; break
                    if filled_i is None:
                        continue                           # dolmadı → işlem yok (orders sayıldı)
                    pm["filled"] += 1
                    entry, i0, e_time = limit_px, filled_i, ltf.iloc[filled_i]["t"]
                    cost = FEE_RT * entry / risk
                    state[fn.__name__] = (sig["direction"], entry, sig["sl"], sig["tp"],
                                          risk, i0, cost, False, e_time,
                                          dict(mfe=0.0, mae=0.0, bars=0,
                                               be_moved=False, r_at_partial=None))
                    continue
                pm["orders"] += 1                          # market giriş da bir emirdir
                pm["filled"] += 1
                cost = FEE_RT * sig["entry"] / risk
                state[fn.__name__] = (sig["direction"], sig["entry"], sig["sl"],
                                      sig["tp"], risk, i, cost, False, sig["time"],
                                      dict(mfe=0.0, mae=0.0, bars=0,
                                           be_moved=False, r_at_partial=None))
    out = {}
    for fn in scanner.STRATS:
        m = _metrics(np.array(trades[fn.__name__]))
        m["score"] = backtest_score(m)
        m["detail"] = detail.get(fn.__name__, [])
        if fn.__name__ in pend_meta:
            m["pending_orders"] = pend_meta[fn.__name__]["orders"]
            m["pending_filled"] = pend_meta[fn.__name__]["filled"]
        out[fn.__name__] = m
    return out

# ---------------- Terminal yardımcıları ----------------
def _agg_str(rs):
    rs = np.array(rs) if len(rs) else np.array([])
    if len(rs) == 0:
        return "     —      "
    wr  = 100 * float((rs > 0.05).mean())
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum()
    pf  = min(gain / loss, 9.99) if loss > 0 else 9.99
    return f"n={len(rs):3d} WR={wr:4.1f}% PF={pf:4.2f}"

def _agg_html(rs):
    rs = np.array(rs) if len(rs) else np.array([])
    if len(rs) == 0:
        return '<td class="dim">—</td>'
    n   = len(rs)
    wr  = 100 * float((rs > 0.05).mean())
    gain, loss = rs[rs > 0].sum(), -rs[rs < 0].sum()
    pf  = min(gain / loss, 9.99) if loss > 0 else 9.99
    cls = "good" if pf >= 1.2 else ("warn" if pf >= 1.0 else "bad")
    return f'<td class="{cls}">n={n}<br>WR {wr:.0f}%<br>PF {pf:.2f}</td>'

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
        print("\n--- STRATEJİ ÖZETİ ---")
        labs = next(iter(out.values())).keys()
        for lab in labs:
            ms = [m[lab] for m in out.values() if m.get(lab, {}).get("trades", 0) > 0]
            if not ms:
                continue
            tot = sum(m["trades"] for m in ms)
            wr  = sum(m["wr"] * m["trades"] for m in ms) / tot
            pfs = sorted(m["pf"] for m in ms)
            pend = sum(m.get("pending_orders", 0) for m in ms)
            fill = sum(m.get("pending_filled", 0) for m in ms)
            extra = f" | emir {pend} → doldu {fill} (%{100*fill/pend:.0f})" if pend else ""
            print(f"  {scanner.STRAT_LABELS.get(lab, lab):32s} "
                  f"işlem={tot:4d}  WR={wr:5.1f}%  medyanPF={pfs[len(pfs)//2]:.2f}{extra}")
    return out

# ---------------- Kırılımlar ----------------
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

# ---------------- YENİ: Yolculuk Analizi ----------------
def _journey_table(out: dict, mfe_threshold: float = 2.0) -> tuple:
    """Strateji × MFE kırılımı: 'TP'ye varamadan X R gören işlemler' oranı."""
    rows_term, rows_html = [], []
    labs = next(iter(out.values())).keys()
    for lab in labs:
        det = []
        for sym in out:
            det.extend(out[sym].get(lab, {}).get("detail", []))
        if len(det) < 5:
            continue
        big = [d for d in det if d.get("mfe_r", 0) >= mfe_threshold]
        big_no_tp = [d for d in big if d.get("exit_reason") != "TP"]
        pct = round(100 * len(big_no_tp) / len(big), 1) if big else None
        avg_mfe = round(sum(d["mfe_r"] for d in det) / len(det), 2)
        rows_term.append((scanner.STRAT_LABELS.get(lab, lab), len(det), len(big),
                          len(big_no_tp), pct, avg_mfe))
        rows_html.append((lab, len(det), len(big), len(big_no_tp), pct, avg_mfe))
    return rows_term, rows_html

def _journey_table_html(rows_html, thr) -> str:
    if not rows_html:
        return ""
    head = ("<th>Strateji</th><th>İşlem</th><th>MFE≥" + f"{thr}R</th>"
            f"<th>Bunlardan TP'siz kaplanan</th><th>Oran</th><th>Ort. MFE</th>")
    body = "".join(
        f'<tr><td class="left"><b>{scanner.STRAT_LABELS.get(lab, lab)}</b></td>'
        f'<td>{n}</td><td>{big}</td>'
        f'<td class="{"warn" if pct and pct >= 30 else ""}">{no}</td>'
        f'<td class="{"warn" if pct and pct >= 30 else ""}">%{pct if pct is not None else "—"}</td>'
        f'<td>{avg:+.2f}R</td></tr>'
        for lab, n, big, no, pct, avg in rows_html)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

def _coin_table_html(out: dict) -> str:
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

def _research_html(trades, out, n, bars, ts_str, arch_name, thr=2.0) -> str:
    rs = np.array([t["rr_net"] for t in trades]) if trades else np.array([])
    gen_wr = 100 * float((rs > 0.05).mean()) if len(rs) else 0.0
    gain = rs[rs > 0].sum() if len(rs) else 0.0
    loss = -rs[rs < 0].sum() if len(rs) else 0.0
    gen_pf = min(gain / loss, 9.99) if loss > 0 else (9.99 if gain > 0 else 0.0)
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
    _, jrows = _journey_table(out, thr)
    j_html = _journey_table_html(jrows, thr)
    coin_html = _coin_table_html(out) if out else ""
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<title>SMC Derin Backtest — {ts_str}</title><style>{_CSS}</style></head><body>
<h1>🔬 SMC Derin Backtest Raporu</h1>
<div>{kpis}</div>
<h2>📊 Strateji Özeti</h2>
{sum_html if sum_html else '<p>Veri yok.</p>'}
<h2>🧭 Yolculuk Analizi (MFE) — TP'ye varamadan {thr}R görenler</h2>
{j_html if j_html else '<p>Veri yok.</p>'}
<h2>🏷️ Kategori × Strateji</h2>
{kat_html if kat_html else '<p>Veri yok.</p>'}
<h2>🕐 Seans × Strateji (UTC)</h2>
{ses_html if ses_html else '<p>Veri yok.</p>'}
<h2>🪙 Coin × Strateji Detayı</h2>
{coin_html}
<p class="note">Renk: yeşil PF≥1.2 · sarı 1.0-1.2 · kırmızı &lt;1.0 · gri az örneklem.<br>
MFE = işlem boyunca görülen en yüksek R. MFE≥{thr}R işlemlerin büyük kısmı TP'siz kapandıysa →
TP hedefi geri çekilmeli (deney adayı). Arşiv: {arch_name}.</p>
</body></html>"""

def research(n=DEFAULT_COINS, bars=DEFAULT_BARS):
    print(f"🔬 DERİN ARAŞTIRMA: {n} coin (MCAP) × {bars} bar...\n", flush=True)
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

    print("\n--- YOLCULUK ANALİZİ (MFE≥2R ama TP'ye varamayanlar) ---")
    rows_term, _ = _journey_table(out, 2.0)
    for lab, n_t, big, no, pct, avg in rows_term:
        pct_s = f"%{pct}" if pct is not None else "—"
        print(f"  {lab:32s} işlem={n_t:4d}  MFE≥2R={big:3d}  TP'siz={no:3d}  ({pct_s})  ort.MFE={avg:+.2f}R")

    ts = datetime.now()
    ts_str = ts.strftime("%d.%m.%Y %H:%M")
    arch_name = f"{ts:%Y-%m-%d_%H%M}.json"
    coins = {sym: dict(rank=scanner.MCAP_RANK.get(sym, (None, "—"))[0],
                       kategori=scanner.MCAP_RANK.get(sym, (None, "—"))[1])
             for sym in out}
    payload = dict(generated=str(ts), coins=n, bars=bars, coin_meta=coins, trades=trades)
    Path("backtest_sonuc.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    arch = Path(ARCHIVE_DIR); arch.mkdir(exist_ok=True)
    (arch / arch_name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    html = _research_html(trades, out, n, bars, ts_str, f"{ARCHIVE_DIR}/{arch_name}", 2.0)
    Path("backtest_rapor.html").write_text(html, encoding="utf-8")
    print(f"\n✅ Son durum : backtest_sonuc.json")
    print(f"✅ Arşiv     : {ARCHIVE_DIR}/{arch_name}")
    print(f"✅ HTML özet : backtest_rapor.html")

if __name__ == "__main__":
    n    = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COINS
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BARS
    research(n, bars)