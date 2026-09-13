"""Backtest v4: kısmi TP (%50 @1R → BE → kalan @TP) + dinamik sembol listesi"""
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
import scanner

FEE_RT  = 0.001
WARMUP  = 220
MAX_BARS = 96
PARTIAL_TP = True   # A/B anahtarı: False yaparsan v3 davranışı (tek çıkış) döner

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

def backtest_symbol(symbol: str, bars: int = 3000) -> dict:
    ltf = scanner.klines_multi(symbol, "15m", total=bars)
    htf = scanner.klines(symbol, "1h",  limit=600)
    if len(ltf) < WARMUP + 50 or len(htf) < 210:
        return {}
    trades, state = {f.__name__: [] for f in scanner.STRATS}, {}
    for i in range(WARMUP, len(ltf)):
        bar = ltf.iloc[i]
        # --- 1) Açık pozisyonlar: SL önce (muhafazakâr), sonra 1R kısmi, sonra TP
        for name, p in list(state.items()):
            d, entry, sl, tp, risk, i0, cost, half = p
            r1 = entry + risk if d == "LONG" else entry - risk
            if d == "LONG":
                hit_sl = bar["low"] <= sl
                hit_r1 = (not half) and bar["high"] >= r1
                hit_tp = bar["high"] >= tp
            else:
                hit_sl = bar["high"] >= sl
                hit_r1 = (not half) and bar["low"] <= r1
                hit_tp = bar["low"] <= tp

            if hit_sl:
                # half alındıysa SL artık BE'de: kalan yarım 0R, toplam +0.5R
                trades[name].append((0.5 if half else -1.0) - cost)
                del state[name]
            elif PARTIAL_TP and hit_r1:
                state[name] = (d, entry, entry, tp, risk, i0, cost, True)  # SL → BE
            elif hit_tp:
                rr = (tp - entry) / risk if d == "LONG" else (entry - tp) / risk
                trades[name].append(((0.5 + 0.5 * rr) if half else rr) - cost)
                del state[name]
            elif i - i0 >= MAX_BARS:
                px = bar["close"]
                r = (px - entry) / risk if d == "LONG" else (entry - px) / risk
                trades[name].append(((0.5 + 0.5 * r) if half else r) - cost)
                del state[name]
        # --- 2) Sinyal arama (sadece o ana kadarki veriyle)
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
                                          sig["tp"], risk, i, cost, False)
    out = {}
    for fn in scanner.STRATS:
        m = _metrics(np.array(trades[fn.__name__]))
        m["score"] = backtest_score(m)
        out[fn.__name__] = m
    return out

def backtest_all(n_coins=30, bars=3000, workers=6, symbols=None) -> dict:
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