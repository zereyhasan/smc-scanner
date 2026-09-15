"""100→50 coin SMC tarayıcı (v8.3.1): çift veri kaynağı + stratejiler + MPL köprüsü.
v8.3.1 FIX: scan() dedup anahtarı (symbol, direction) → (symbol, strategy, direction).
  Eskiden aynı coin+yön'de Sweep, MPL'yi eziyordu → MPL hiç Notion'a/paper'a ulaşmıyordu.
  Artık her strateji kendi sinyalini taşır (ayrı işlem = ayrı satır)."""
import os, sys, json, time
from pathlib import Path
import requests
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import smc
import mpl

SOURCE = os.environ.get("DATA_SOURCE", "bybit").lower()   # 'bybit' | 'okx'

FAPI = "https://api.bybit.com"
OKX  = "https://www.okx.com"
CG   = "https://api.coingecko.com/api/v3"
PAPRIKA = "https://api.coinpaprika.com/v1"
_IV_BYBIT = {"15m": "15", "1h": "60", "4h": "240"}
_IV_OKX   = {"15m": "15m", "1h": "1H", "4h": "4H"}
MIN_RR = 1.8
RR_CAP = 2.5
MCAP_TOP  = 50
CAT_BANDS = ((10, "Majör"), (25, "Large"), (50, "Mid"))

STOCKS = {"AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "META",
          "COIN", "SPX", "NDX", "XAU"}

MCAP_RANK = {}
_OKX_MAP = {}
_COLS = ["t", "open", "high", "low", "close", "volume"]

STRAT_LABELS = {
    "strategy_pullback": "Trend Pullback (OB)",
    "strategy_fvg":      "FVG Retest",
    "strategy_sweep":    "Likidite Süpürme (Reversal)",
    "strategy_breaker":  "Breaker Block",
    "strategy_multisweep": "Çoklu Tepe Süpürme (FVG)",
    "strategy_mpl":      "Maximum Pain Level",
}
STRAT_BY_LABEL = {v: k for k, v in STRAT_LABELS.items()}

# ---------------- Dayanıklı HTTP+JSON ----------------
def _get_json(url, params=None, timeout=15, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
            else:
                try:
                    return r.json()
                except ValueError:
                    raise RuntimeError(
                        f"JSON-olmayan yanıt: HTTP {r.status_code} | "
                        f"content-type={r.headers.get('content-type')} | "
                        f"ilk 200 karakter: {r.text[:200]!r}")
        except requests.RequestException as e:
            last = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} başarısız ({retries + 1} deneme): {last}")

def _to_df(rows):
    df = pd.DataFrame([(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                       for k in rows], columns=_COLS)
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df

# ---------------- OKX katmanı (bulut) ----------------
def _okx_symbol_map():
    if _OKX_MAP:
        return _OKX_MAP
    tick = _get_json(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"})
    for it in tick.get("data", []):
        iid = it.get("instId", "")
        if iid.endswith("-USDT-SWAP"):
            base = iid[:-len("-USDT-SWAP")] + "USDT"
            _OKX_MAP[base] = iid
    return _OKX_MAP

def _okx_klines(symbol, interval, limit):
    m = _okx_symbol_map()
    inst = m.get(symbol)
    if not inst:
        return pd.DataFrame(columns=_COLS)
    bar = _IV_OKX[interval]
    need = min(limit, 3000)
    rows, after, prev = [], None, None
    while len(rows) < need:
        params = {"instId": inst, "bar": bar, "limit": 300}
        if after:
            params["after"] = after
        d = _get_json(f"{OKX}/api/v5/market/candles", params=params)
        batch = d.get("data", [])
        if not batch or batch[-1][0] == prev:
            break
        rows.extend(batch)
        prev = after
        after = batch[-1][0]
        if len(batch) < 300:
            break
    if not rows:
        return pd.DataFrame(columns=_COLS)
    return _to_df(rows)

def _okx_universe_fallback(limit):
    tick = _get_json(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"})
    rows = []
    for it in tick.get("data", []):
        iid = it.get("instId", "")
        if not iid.endswith("-USDT-SWAP"):
            continue
        base = iid[:-len("-USDT-SWAP")] + "USDT"
        if base[:-4] in STOCKS:
            continue
        try:
            notional = float(it.get("last") or 0) * float(it.get("volCcy24h") or 0)
        except Exception:
            notional = 0.0
        rows.append((base, notional))
    rows.sort(key=lambda x: -x[1])
    return [b for b, _ in rows[:limit]]

# ---------------- Market Cap evreni ----------------
def _category(rank: int) -> str:
    for cap, name in CAT_BANDS:
        if rank <= cap:
            return name
    return "Mid"

def _exchange_symbol_set() -> set:
    if SOURCE == "okx":
        return set(_okx_symbol_map().keys())
    tick = _get_json(f"{FAPI}/v5/market/tickers", params=dict(category="linear"))
    return {x["symbol"] for x in tick["result"]["list"]}

def ensure_mcap(limit: int = MCAP_TOP, force: bool = False) -> dict:
    global MCAP_RANK
    if MCAP_RANK and not force:
        return MCAP_RANK
    cache = Path("mcap_cache.json")
    data, src = None, None
    try:
        data = _get_json(f"{CG}/coins/markets",
                         params=dict(vs_currency="usd", order="market_cap_desc",
                                     per_page=min(limit, 250), page=1))
        src = "coingecko"
    except Exception as e:
        print(f"  ⚠ CoinGecko başarısız: {e}", flush=True)
    if data is None:
        try:
            rows = _get_json(f"{PAPRIKA}/tickers")
            data = [dict(symbol=c.get("symbol"), market_cap_rank=c.get("rank"))
                    for c in rows if (c.get("rank") or 999) <= limit + 50]
            src = "coinpaprika"
        except Exception as e:
            print(f"  ⚠ CoinPaprika da başarısız: {e}", flush=True)
    if data is None and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            src = "cache"
        except Exception:
            data = None
    if not data:
        print("  ⚠ MCAP alınamadı — turnover fallback kullanılacak", flush=True)
        return MCAP_RANK
    try:
        cache.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    try:
        exch = _exchange_symbol_set()
    except Exception as e:
        print(f"  ⚠ Borsa sembol listesi başarısız ({SOURCE}): {e}", flush=True)
        exch = set()
    rank = {}
    for c in data:
        sym = (c.get("symbol") or "").upper() + "USDT"
        rk = c.get("market_cap_rank") or 999
        if sym in exch and sym not in rank and sym[:-4] not in STOCKS:
            rank[sym] = (rk, _category(rk))
    MCAP_RANK = rank
    print(f"  MCAP kaynağı: {src} → {len(rank)} coin {SOURCE.upper()} ile eşleşti", flush=True)
    return MCAP_RANK

# ---------------- Veri ----------------
def universe(limit=MCAP_TOP):
    limit = min(limit, MCAP_TOP)
    ensure_mcap(limit)
    if len(MCAP_RANK) >= limit:
        return sorted(MCAP_RANK, key=lambda s: MCAP_RANK[s][0])[:limit]
    if MCAP_RANK:
        print(f"  ⚠ MCAP eşleşen coin {len(MCAP_RANK)} adet (< {limit}) — bunlar kullanılıyor")
        return sorted(MCAP_RANK, key=lambda s: MCAP_RANK[s][0])
    print(f"  ⚠ MCAP alınamadı — hacim fallback ({SOURCE})")
    if SOURCE == "okx":
        return _okx_universe_fallback(limit)
    t = _get_json(f"{FAPI}/v5/market/tickers", params=dict(category="linear"))
    rows = [x for x in t["result"]["list"] if x["symbol"].endswith("USDT")
            and x["symbol"][:-4] not in STOCKS
            and not any(k in x["symbol"] for k in ("UP", "DOWN", "BULL", "BEAR"))]
    rows.sort(key=lambda x: float(x["turnover24h"]), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def klines(symbol, interval, limit=250):
    if SOURCE == "okx":
        return _okx_klines(symbol, interval, limit)
    limit = min(limit, 1000)
    d = _get_json(f"{FAPI}/v5/market/kline",
                  params=dict(category="linear", symbol=symbol,
                              interval=_IV_BYBIT.get(interval, interval), limit=limit))
    rows = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            for k in d["result"]["list"]]
    df = pd.DataFrame(rows, columns=_COLS)
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df.sort_values("t").reset_index(drop=True)

def klines_multi(symbol, interval, total=6000):
    if SOURCE == "okx":
        return _okx_klines(symbol, interval, total)
    pages, end = [], None
    while sum(len(p) for p in pages) < total:
        params = dict(category="linear", symbol=symbol,
                      interval=_IV_BYBIT.get(interval, interval), limit=1000)
        if end:
            params["end"] = end
        d = _get_json(f"{FAPI}/v5/market/kline", params=params)
        rows = d.get("result", {}).get("list", [])
        if not rows:
            break
        pages.append(rows)
        end = int(rows[-1][0]) - 1
        if len(rows) < 1000:
            break
    rows = [k for p in pages for k in p]
    if not rows:
        return pd.DataFrame(columns=_COLS)
    return _to_df(rows)

def build_context(symbol, htf, ltf):
    hs, ls = smc.find_swings(htf), smc.find_swings(ltf)
    return dict(
        symbol=symbol, htf=htf, ltf=ltf, htf_swings=hs, ltf_swings=ls,
        htf_trend=smc.trend(hs), ltf_trend=smc.trend(ls),
        fvgs=smc.find_fvgs(ltf), obs=smc.find_order_blocks(ltf),
        sweep=smc.liquidity_sweep(ltf, ls),
        pd=smc.premium_discount(ltf, ls),
        price=float(ltf["close"].iloc[-1]),
        ema200=float(smc.ema(htf["close"], 200).iloc[-1]),
        vol_ok=bool(ltf["volume"].iloc[-1] > ltf["volume"].iloc[-21:-1].mean()),
        atr=float((ltf["high"] - ltf["low"]).rolling(14).mean().iloc[-1]),
    )

# ---------------- Ortak Sinyal Tamamlayıcı ----------------
def _finish(ctx, name, direction, zone_bottom, zone_top, extra_conf,
            require_conf=True, extra_sl=None):
    px, atr = ctx["price"], ctx["atr"]
    ok, cname = smc.confirmation(ctx["ltf"], direction)
    if require_conf and not ok:
        return None
    score, conf = 0, list(extra_conf)

    want = "BULLISH" if direction == "LONG" else "BEARISH"
    if ctx["htf_trend"] == want:
        score += 25; conf.append(f"1H trend uyumlu ({ctx['htf_trend']})")
    if (direction == "LONG" and px > ctx["ema200"]) or (direction == "SHORT" and px < ctx["ema200"]):
        score += 10; conf.append("Fiyat 1H EMA200 doğru tarafta")
    if ok:
        score += 15; conf.append(f"Onay mumu: {cname}")
    if ctx["sweep"]["bull" if direction == "LONG" else "bear"]:
        score += 15; conf.append("Likidite süpürmesi (stop hunt) gerçekleşti")
    if ctx["vol_ok"]:
        score += 5; conf.append("Hacim 20 periyot ortalamanın üstünde")

    buf = max(atr * 0.25, px * 0.0015)
    min_stop = max(atr * 0.8, px * 0.008)
    lows  = [s.price for s in ctx["ltf_swings"] if s.kind == "L"][-3:]
    highs = [s.price for s in ctx["ltf_swings"] if s.kind == "H"][-3:]
    if direction == "LONG":
        sl = min([zone_bottom] + lows) - buf
        if extra_sl is not None:
            sl = min(sl, float(extra_sl) - buf)
        risk = px - sl
        if risk < min_stop:
            sl = px - min_stop
            risk = min_stop
        tp = ctx["pd"]["range_high"] if ctx["pd"] else 0
        if tp < px + 1.5 * risk:
            tp = px + 2 * risk
        tp = min(tp, px + RR_CAP * risk)
    else:
        sl = max([zone_top] + highs) + buf
        if extra_sl is not None:
            sl = max(sl, float(extra_sl) + buf)
        risk = sl - px
        if risk < min_stop:
            sl = px + min_stop
            risk = min_stop
        tp = ctx["pd"]["range_low"] if ctx["pd"] else 0
        if tp == 0 or tp > px - 1.5 * risk:
            tp = px - 2 * risk
        tp = max(tp, px - RR_CAP * risk)

    rr = abs(tp - px) / risk if risk > 0 else 0
    if rr >= 2.2:   score += 20
    elif rr >= 2.0: score += 15
    elif rr >= MIN_RR: score += 8
    else: return None

    if ctx["pd"]:
        if direction == "LONG" and ctx["pd"]["zone"] == "İNDİRİM":
            score += 10; conf.append("Discount bölgesinden alım")
        if direction == "SHORT" and ctx["pd"]["zone"] == "PRİM":
            score += 10; conf.append("Premium bölgesinden satım")

    return dict(symbol=ctx["symbol"], strategy=name, direction=direction,
                entry=px, sl=sl, tp=tp, rr=round(rr, 2), score=min(score, 100),
                confluences=conf, candle=cname or "Onaysız",
                zone=(float(zone_bottom), float(zone_top)),
                time=ctx["ltf"]["t"].iloc[-1])

# ---------------- Stratejiler ----------------
def strategy_pullback(ctx):
    for d in ("LONG", "SHORT"):
        if ctx["htf_trend"] != ("BULLISH" if d == "LONG" else "BEARISH"):
            continue
        px, atr = ctx["price"], ctx["atr"]
        for ob in ctx["obs"]:
            if ob["type"] != ("bullish" if d == "LONG" else "bearish") or ob["mitigated"]:
                continue
            if d == "LONG" and ob["bottom"] <= px <= ob["top"] + 0.5 * atr:
                return _finish(ctx, STRAT_LABELS["strategy_pullback"], d, ob["bottom"], ob["top"],
                               ["Fiyat unmitigated Order Block'a geri çekildi"])
            if d == "SHORT" and ob["bottom"] - 0.5 * atr <= px <= ob["top"]:
                return _finish(ctx, STRAT_LABELS["strategy_pullback"], d, ob["bottom"], ob["top"],
                               ["Fiyat unmitigated Order Block'a geri çekildi"])
    return None

def strategy_fvg(ctx):
    for d in ("LONG", "SHORT"):
        if ctx["htf_trend"] != ("BULLISH" if d == "LONG" else "BEARISH"):
            continue
        px = ctx["price"]
        for f in ctx["fvgs"]:
            if f["type"] != ("bullish" if d == "LONG" else "bearish") or f["filled"]:
                continue
            if f["bottom"] <= px <= f["top"]:
                return _finish(ctx, STRAT_LABELS["strategy_fvg"], d, f["bottom"], f["top"],
                               ["Doldurulmamış Fair Value Gap şu an test ediliyor"])
    return None

def strategy_sweep(ctx):
    lows  = [s.price for s in ctx["ltf_swings"] if s.kind == "L"][-2:]
    highs = [s.price for s in ctx["ltf_swings"] if s.kind == "H"][-2:]
    if ctx["sweep"]["bull"]:
        if ctx["pd"] and ctx["pd"]["zone"] != "İNDİRİM":
            return None
        return _finish(ctx, STRAT_LABELS["strategy_sweep"], "LONG",
                       min(lows or [ctx["price"] * 0.99]), ctx["price"],
                       ["Swing low süpürüldü, üstünde kapanış geldi"])
    if ctx["sweep"]["bear"]:
        if ctx["pd"] and ctx["pd"]["zone"] != "PRİM":
            return None
        return _finish(ctx, STRAT_LABELS["strategy_sweep"], "SHORT",
                       ctx["price"], max(highs or [ctx["price"] * 1.01]),
                       ["Swing high süpürüldü, altında kapanış geldi"])
    return None

def strategy_breaker(ctx):
    for d in ("LONG", "SHORT"):
        want = "BULLISH" if d == "LONG" else "BEARISH"
        if ctx["htf_trend"] != want:
            continue
        px, atr = ctx["price"], ctx["atr"]
        for ob in ctx["obs"]:
            if ob["type"] != ("bearish" if d == "LONG" else "bullish"):
                continue
            after = ctx["ltf"]["close"].iloc[ob["idx"] + 3:]
            if d == "LONG" and ob["bottom"] <= px <= ob["top"] + 0.4 * atr and (after > ob["top"]).any():
                return _finish(ctx, STRAT_LABELS["strategy_breaker"], d, ob["bottom"], ob["top"],
                               ["Bearish OB kırıldı → breaker destek testi"])
            if d == "SHORT" and ob["bottom"] - 0.4 * atr <= px <= ob["top"] and (after < ob["bottom"]).any():
                return _finish(ctx, STRAT_LABELS["strategy_breaker"], d, ob["bottom"], ob["top"],
                               ["Bullish OB kırıldı → breaker direnç testi"])
    return None

def strategy_multisweep(ctx):
    """S5: ÇOKLU TEPE/DİP SÜPÜRME + FVG + CHoCH (kullanıcı tanımlı)"""
    df, swings = ctx["ltf"], ctx["ltf_swings"]
    n = len(df)
    pools = smc.find_pools(df, swings)
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    avg = pd.Series(body).rolling(20).mean().values

    for pool in pools["H"]:
        swept_i, sweep_high = None, None
        for j in range(max(0, n - 5), n):
            strong = body[j] > 1.5 * max(avg[j - 1] if j > 0 else 0.0, 1e-12)
            if h[j] > pool["top"] and c[j] < pool["top"] and strong:
                swept_i, sweep_high = j, float(h[j]); break
        if swept_i is None:
            continue
        ch_ok, _ = smc.choch(df, swings, direction="bear", since=swept_i)
        if not ch_ok:
            continue
        for f in ctx["fvgs"]:
            if (f["type"] == "bearish" and not f["filled"]
                    and f["idx"] >= swept_i - 1
                    and f["bottom"] <= ctx["price"] <= f["top"]):
                sig = _finish(ctx, STRAT_LABELS["strategy_multisweep"], "SHORT",
                              f["bottom"], f["top"],
                              [f"Çoklu-tepe havuzu ({pool['touches']} tepe) tek mumla süpürüldü",
                               "Süpürme displacement'ı FVG bıraktı — fiyat retest ediyor",
                               "CHoCH: son HL kırıldı (dönüş onayı)"],
                              require_conf=True, extra_sl=sweep_high)
                if sig:
                    return sig

    for pool in pools["L"]:
        swept_i, sweep_low = None, None
        for j in range(max(0, n - 5), n):
            strong = body[j] > 1.5 * max(avg[j - 1] if j > 0 else 0.0, 1e-12)
            if l[j] < pool["bottom"] and c[j] > pool["bottom"] and strong:
                swept_i, sweep_low = j, float(l[j]); break
        if swept_i is None:
            continue
        ch_ok, _ = smc.choch(df, swings, direction="bull", since=swept_i)
        if not ch_ok:
            continue
        for f in ctx["fvgs"]:
            if (f["type"] == "bullish" and not f["filled"]
                    and f["idx"] >= swept_i - 1
                    and f["bottom"] <= ctx["price"] <= f["top"]):
                sig = _finish(ctx, STRAT_LABELS["strategy_multisweep"], "LONG",
                              f["bottom"], f["top"],
                              [f"Çoklu-dip havuzu ({pool['touches']} dip) tek mumla süpürüldü",
                               "Süpürme displacement'ı FVG bıraktı — fiyat retest ediyor",
                               "CHoCH: son LH kırıldı (dönüş onayı)"],
                              require_conf=True, extra_sl=sweep_low)
                if sig:
                    return sig
    return None

def strategy_mpl(ctx):
    """S6: Maximum Pain Level — mpl.py izole modülünden PENDING sinyal üretir."""
    try:
        s = mpl.signal(ctx)
    except Exception:
        return None
    if not s:
        return None
    return dict(symbol=ctx["symbol"], strategy=s["strategy"], direction=s["direction"],
                entry=s["limit"], sl=s["sl"], tp=s["tp"], rr=s["rr"], score=s["score"],
                confluences=s["confluences"], candle="Limit emir (CE %50)",
                zone=(min(s["limit"], s["sl"]), max(s["limit"], s["sl"])),
                time=ctx["ltf"]["t"].iloc[-1],
                pending=True, expiry_bars=s["expiry_bars"], no_partial=True)

STRATS = [strategy_pullback, strategy_fvg, strategy_sweep,
          strategy_breaker, strategy_multisweep, strategy_mpl]
_BY_NAME = {f.__name__: f for f in STRATS}
ACTIVE_STRATS = ["strategy_sweep", "strategy_breaker", "strategy_multisweep", "strategy_mpl"]

# ---------------- Tarama ----------------
def analyze(symbol):
    try:
        ctx = build_context(symbol, klines(symbol, "1h"), klines(symbol, "15m"))
        sigs = []
        for name in ACTIVE_STRATS:
            try:
                s = _BY_NAME[name](ctx)
            except Exception:
                s = None
            if s:
                sigs.append(s)
        return sigs or None
    except Exception as e:
        print(f"  ⚠ {symbol}: {e!r}", flush=True)
        return None

def scan(limit=MCAP_TOP, workers=8):
    syms = universe(limit)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze, s): s for s in syms}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                results.extend(r)
            print(f"\r[{i}/{len(syms)}] tarandı | {len(results)} sinyal bulundu",
                  end="", flush=True)
    print()
    results.sort(key=lambda x: x["score"], reverse=True)
    # v8.3.1 FIX: dedup anahtarı stratejiyi içerir — farklı stratejiler ayrı işlemlerdir
    seen, tek = set(), []
    for r in results:
        key = (r["symbol"], r["strategy"],
               "LONG" if r["direction"] == "LONG" else "SHORT")
        if key in seen:
            continue
        seen.add(key)
        tek.append(r)
    return tek

# ---------------- Journal ----------------
def journal(sig, balance=1000.0, risk_pct=1.0):
    risk_amt = balance * risk_pct / 100
    qty = risk_amt / abs(sig["entry"] - sig["sl"])
    pl = qty * abs(sig["tp"] - sig["entry"])
    arrow = "🟢 BUY / LONG" if sig["direction"] == "LONG" else "🔴 SELL / SHORT"
    conf = "\n".join(f"   ✔ {c}" for c in sig["confluences"])
    return f"""
🎯 MY TRADE JOURNAL — SMC Otomatik Tarayıcı
TARİH        : {sig['time']:%d/%m/%Y %H:%M}
PARİTE       : {sig['symbol']} (FUTURES)
ZAMAN DİLİMİ : 15M (Giriş) / 1H (Trend)

1️⃣ SETUP
   Setup Tipi   : {sig['strategy']}
   Yön          : {sig['direction']}   Skor: {sig['score']}/100

2️⃣ GEREKÇE
{conf}

3️⃣ GİRİŞ
   Yön          : {arrow}
   Giriş        : {sig['entry']:.6g}{' (LIMIT)' if sig.get('pending') else ''}
   Giriş Mumu   : {sig['candle']}

4️⃣ STOP LOSS
   SL           : {sig['sl']:.6g}  (bölge/swing + ATR tamponu, min %0.8)
   Risk         : {risk_amt:.2f} USDT (%{risk_pct})
   Pozisyon     : {qty:.6g} adet

5️⃣ ÇIKIŞ PLANI
   TP           : {sig['tp']:.6g}
   {'Plan: %50 kâr @1R → SL girişe (BE) → kalan %50 @TP' if not sig.get('no_partial') else f"Plan: TAM {sig.get('rr', 2)}R tek çıkış (kısmi TP yok — MPL)"}
   Risk:Ödül    : 1 : {sig['rr']}
   Beklenen Kâr : +{pl:.2f} USDT
"""

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else MCAP_TOP
    balance = float(sys.argv[2]) if len(sys.argv) > 2 else 1000
    res = scan(n)
    if not res:
        print("Şu an kriterlere uyan fırsat yok (filtreler sıkı — bu normaldir).")
    for i, r in enumerate(res[:10], 1):
        print(f"\n{'='*60}\n#{i} | {r['symbol']} | {r['strategy']} | Skor {r['score']} | RR 1:{r['rr']}")
        print(journal(r, balance))