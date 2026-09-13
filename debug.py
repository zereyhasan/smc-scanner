"""Hızlı teşhis: veri akışı ve strateji bileşenleri sağlıklı mı?"""
import scanner

syms = scanner.universe(12)
print("İlk pariteler:", syms[:5], flush=True)

for s in syms:
    try:
        ltf = scanner.klines(s, "15m")
        htf = scanner.klines(s, "1h")
        if len(ltf) < 60 or len(htf) < 60:
            print(f"{s:12s} VERİ EKSİK (15m={len(ltf)}, 1h={len(htf)})")
            continue
        ctx = scanner.build_context(s, htf, ltf)
        sw = ctx["sweep"]
        nob = len([o for o in ctx["obs"] if not o["mitigated"]])
        nfv = len([f for f in ctx["fvgs"] if not f["filled"]])
        zone = ctx["pd"]["zone"] if ctx["pd"] else "-"
        print(f"{s:12s} son mum: {ctx['ltf']['t'].iloc[-1]:%d.%m %H:%M} | "
              f"fiyat={ctx['price']:.6g} | 1H trend={ctx['htf_trend']:7s} | "
              f"OB={nob} FVG={nfv} | sweep(B/A)={int(sw['bull'])}/{int(sw['bear'])} | bölge={zone}")
        for st in scanner.STRATS:
            try:
                r = st(ctx)
                if r:
                    print(f"   ✅ SİNYAL: {r['strategy']} {r['direction']} RR 1:{r['rr']} skor {r['score']}")
            except Exception as e:
                print(f"   ⚠ HATA {st.__name__}: {e!r}")
    except Exception as e:
        print(f"{s:12s} ❌ VERİ/BAGLAM HATASI: {e!r}")