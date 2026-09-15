"""MPL bileşen testi: 5 coinde havuz/MSB/FVG/Idm zincirinin hangi halkasının kırıldığını gösterir."""
import scanner, mpl, smc

syms = scanner.universe(5)
print(f"Coinler: {syms}\n")

for sym in syms:
    ltf = scanner.klines(sym, "15m", 600)
    htf = scanner.klines(sym, "1h", 600)
    if len(ltf) < 60 or len(htf) < 210:
        print(f"{sym:12s} veri eksik"); continue
    ctx = scanner.build_context(sym, htf, ltf)

    # 1) Havuz var mı? (1H'da)
    pools_h = mpl._pool_stats(htf, ctx["htf_swings"], "H")
    pools_l = mpl._pool_stats(htf, ctx["htf_swings"], "L")
    # 2) MSB: 1H son HL altına 15m kapanış?
    sig = None
    try:
        sig = mpl.signal(ctx)
    except Exception as e:
        print(f"{sym:12s} mpl.signal HATA: {e!r}")
        continue
    hs = [s for s in ctx["htf_swings"] if s.kind == "L"]
    hl = hs[-1].price if hs else None
    closes = ltf["close"].values[-3:]
    msb = any(c < hl for c in closes) if hl else False
    # 3) FVG (bearish, MSB penceresi) — signal içinde sayılıyor; burada ham sayı
    n_fvg = len([f for f in ctx["fvgs"] if f["type"]=="bearish" and not f["filled"]])

    print(f"{sym:12s} tepeHavuz={len(pools_h)} dipHavuz={len(pools_l)} | "
          f"1H HL={hl and round(hl,5) or '—'} MSB(15m kapanış)={msb} | "
          f"dolmamış bearFVG={n_fvg} | SİNYAL={'✅ '+sig['strategy'] if sig else 'yok'}")

print("\n— Her coin'de hangi halka kırılıyor görülüyor —")
print("tepeHavuz=0 → havuz şartı tutmuyor (en sık beklenen)")
print("MSB=False → HL altına kapanış yok (2. en sık)")
print("her ikisi OK ama sinyal yok → FVG/Idm/price konumu şartları")