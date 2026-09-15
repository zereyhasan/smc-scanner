"""MPL SL tarifini görselleştirir: rastgele/belirli bir MPL sinyali bulur,
grafikte bacak kökü + 1H extreme + SL + TP + FVG %50 noktalarını işaretler.
Kullanım:  python mpl_goster.py            (ilk MPL sinyalini arar, 15 coine kadar)
           python mpl_goster.py SOLUSDT    (belirli coin'de arar)"""
import sys
import plotly.graph_objects as go
import scanner, mpl, smc

def show(sym):
    ltf = scanner.klines(sym, "15m", 400)
    htf = scanner.klines(sym, "1h", 600)
    ctx = scanner.build_context(sym, htf, ltf)
    s = mpl.signal(ctx)
    if not s:
        print(f"{sym}: şu an MPL sinyali yok (koşullar sağlanmadı).")
        print("  (İpucu: MSB tetiği 'son 3 mumda 1H HL altına kapanış' ister — ")
        print("   yeni MSB olduğunda tekrar dene ya da başka coin dene.)")
        return

    # SL bileşenlerini yeniden hesapla (görselleştirme için)
    ltf_swings = smc.find_swings(ltf)
    htf_swings = ctx["htf_swings"]
    closes = ltf["close"].values[-3:]
    h_lows = [x for x in htf_swings if x.kind == "L"]
    h_highs = [x for x in htf_swings if x.kind == "H"]
    hl_price = h_lows[-1].price if h_lows else None
    lh_price = h_highs[-1].price if h_highs else None

    if s["direction"] == "SHORT":
        msb_idx = None
        for k in range(len(closes)-1, -1, -1):
            if closes[k] < hl_price:
                msb_idx = len(ltf) - (len(closes) - k); break
        root_time, root_low15 = mpl._leg_root_time(ltf, msb_idx, ltf_swings)
        anchor = mpl._sl_anchor_short_1h(htf, root_time, htf_swings)
        sl, tp, entry = s["sl"], s["tp"], s["limit"]
        y_root, y_anchor = root_low15, anchor
    else:
        msb_idx = None
        for k in range(len(closes)-1, -1, -1):
            if closes[k] > lh_price:
                msb_idx = len(ltf) - (len(closes) - k); break
        root_time, root_high15 = mpl._leg_root_time_long(ltf, msb_idx, ltf_swings)
        anchor = mpl._sl_anchor_long_1h(htf, root_time, htf_swings)
        sl, tp, entry = s["sl"], s["tp"], s["limit"]
        y_root, y_anchor = root_high15, anchor

    fig = go.Figure(go.Candlestick(
        x=ltf["t"], open=ltf["open"], high=ltf["high"], low=ltf["low"],
        close=ltf["close"], name=sym,
        increasing_line_color="#26a65b", decreasing_line_color="#e14b5a"))

    # SL bileşen çizgileri
    fig.add_hline(y=y_root, line_dash="dot", line_color="#8ea0c9",
                  annotation_text="15M Bacak Kökü (dip/tepe)")
    fig.add_hline(y=y_anchor, line_dash="dot", line_color="#ff9f43",
                  annotation_text="1H Extreme (SL çapası)")
    fig.add_hline(y=sl, line_dash="dash", line_color="#e14b5a", annotation_text="SL (nihai)")
    fig.add_hline(y=entry, line_dash="dash", line_color="#f5c542", annotation_text="GİRİŞ (FVG %50)")
    fig.add_hline(y=tp, line_dash="dash", line_color="#26a65b", annotation_text=f"TP ({s['rr']}R)")

    # MSB tetiği oku
    fig.add_annotation(x=ltf["t"].iloc[msb_idx], y=ltf["low"].iloc[msb_idx],
                       text="MSB tetik (15m kapanış)", ax=30, ay=-60, arrowhead=2,
                       font=dict(size=10, color="#f5c542"))

    fig.update_layout(template="plotly_dark", height=600,
                      title=f'{sym} — MPL {s["direction"]} | SL Tarif Görselleştirmesi',
                      xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=60, b=10))
    out = f"mpl_goster_{sym}.html"
    import pathlib, webbrowser
    p = pathlib.Path(out).resolve()
    p.write_text(fig.to_html(full_html=True, include_plotlyjs=True), encoding="utf-8")
    print(f"✅ Grafik hazır: {p}")
    print(f"   GİRİŞ (FVG %50): {entry:.6g}")
    print(f"   15M Bacak Kökü : {y_root:.6g}  ← hareketin başladığı dip/tepe (15m)")
    print(f"   1H Extreme     : {y_anchor:.6g}  ← kökün 1H karşılığı (SL çapası)")
    print(f"   SL (nihai)     : {sl:.6g}  (1H extreme + ATR tamponu)")
    print(f"   TP             : {tp:.6g}  ({s['rr']}R)")
    webbrowser.open(p.as_uri())

if __name__ == "__main__":
    if len(sys.argv) > 1:
        show(sys.argv[1].upper())
    else:
        # İlk MPL sinyali veren coine kadar tara (max 15)
        syms = scanner.universe(15)
        print("MPL sinyali araniyor (ilk 15 coin)...")
        for sym in syms:
            try:
                ltf = scanner.klines(sym, "15m", 400)
                htf = scanner.klines(sym, "1h", 600)
                ctx = scanner.build_context(sym, htf, ltf)
                if mpl.signal(ctx):
                    show(sym); break
            except Exception:
                continue
        else:
            print("İlk 15 coin'de MPL sinyali bulunamadı — MSB yeni tetiklenmedi demektir.")
            print("Belirli bir coin denemek için: python mpl_goster.py SOLUSDT")