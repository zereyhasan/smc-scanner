"""100→50 coin SMC tarayıcı (v8): Piyasa değeri (market cap) evreni + kategori haritası.
Evren: CoinGecko MCAP ilk 50 → Bybit futures kesişimi. CoinGecko erişilemezse
turnover fallback + mcap_cache.json. Grup analizi için: scanner.MCAP_RANK[sym]=(rank,kategori)"""
import sys, json
from pathlib import Path
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import smc

FAPI = "https://api.bybit.com"
CG   = "https://api.coingecko.com/api/v3"
_IV = {"15m": "15", "1h": "60", "4h": "240"}
MIN_RR = 1.8
RR_CAP = 2.5
MCAP_TOP  = 50                                   # evren büyüklüğü (piyasa değeri sırası)
CAT_BANDS = ((10, "Majör"), (25, "Large"), (50, "Mid"))

STOCKS = {"AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "META",
          "COIN", "SPX", "NDX", "XAU"}

MCAP_RANK = {}   # sembol -> (mcap_rank, kategori)   [gruplu analiz bunu kullanır]

STRAT_LABELS = {
    "strategy_pullback": "Trend Pullback (OB)",
    "strategy_fvg":      "FVG Retest",
    "strategy_sweep":    "Likidite Süpürme (Reversal)",
    "strategy_breaker":  "Breaker Block",
}
STRAT_BY_LABEL = {v: k for k, v in STRAT_LABELS.items()}

# ---------------- Market Cap evreni ----------------
def _category(rank: int) -> str:
    for cap, name in CAT_BANDS:
        if rank <= cap:
            return name
    return "Mid"

def ensure_mcap(limit: int = MCAP_TOP, force: bool = False) -> dict:
    """CoinGecko MCAP sıralamasını çeker, Bybit futures sembolleriyle eşleştirir.
    Sonuç MCAP_RANK'e yazılır; ağ hatasında mcap_cache.json'a düşer."""
    global MCAP_RANK
    if MCAP_RANK and not force:
        return MCAP_RANK
    cache = Path("mcap_cache.json")
    data = None
    try:
        r = requests.get(f"{CG}/coins/markets",
                         params=dict(vs_currency="usd", order="market_cap_desc",
                                     per_page=min(limit, 250), page=1),
                         timeout=15)
        if r.status_code == 200 and isinstance(r.json(), list):
            data = r.json()
            cache.write_text(json.dumps(data), encoding="utf-8")
        else:
            print(f"  ⚠ CoinGecko HTTP {r.status_code} — cache/fallback deneniyor", flush=True)
    except Exception as e:
        print(f"  ⚠ CoinGecko erişilemedi ({e!r}) — cache/fallback", flush=True)
    if data is None and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if not data:
        return MCAP_RANK                      # boş → çağıran turnover fallback'e düşer
    try:
        bybit = {x["symbol"] for x in requests.get(
            f"{FAPI}/v5/market/tickers", params=dict(category="linear"),
            timeout=15).json()["result"]["list"]}
    except Exception:
        bybit = set()
    rank = {}
    for c in data:
        sym = (c.get("symbol") or "").upper() + "USDT"
        rk = c.get("market_cap_rank") or 999
        if sym in bybit and sym not in rank and sym[:-4] not in STOCKS:
            rank[sym] = (rk, _category(rk))
    MCAP_RANK = rank
    return MCAP_RANK

# ---------------- Veri (Bybit) ----------------
def universe(limit=MCAP_TOP):
    """Piyasa değeri sırasına göre evren. CoinGecko yoksa turnover fallback."""
    limit = min(limit, MCAP_TOP)
    ensure_mcap(limit)
    if len(MCAP_RANK) >= limit:
        return sorted(MCAP_RANK, key=lambda s: MCAP_RANK[s][0])[:limit]
    if MCAP_RANK:                              # kısmi liste → yine mcap sırası
        print(f"  ⚠ MCAP eşleşen coin {len(MCAP_RANK)} adet (< {limit}) — bunlar kullanılıyor")
        return sorted(MCAP_RANK, key=lambda s: MCAP_RANK[s][0])
    print("  ⚠ MCAP alınamadı — turnover fallback (24s hacim sıralı)")
    t = requests.get(f"{FAPI}/v5/market/tickers",
                     params=dict(category="linear"), timeout=15).json()
    rows = [x for x in t["result"]["list"] if x["symbol"].endswith("USDT")
            and x["symbol"][:-4] not in STOCKS
            and not any(k in x["symbol"] for k in ("UP", "DOWN", "BULL", "BEAR"))]
    rows.sort(key=lambda x: float(x["turnover24h"]), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def klines(symbol, interval, limit=250):
    limit = min(limit, 1000)
    d = requests.get(f"{FAPI}/v5/market/kline",
                     params=dict(category="linear", symbol=symbol,
                                 interval=_IV.get(interval, interval), limit=limit),
                     timeout=15).json()
    rows = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            for k in d["result"]["list"]]
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df.sort_values("t").reset_index(drop=True)

def klines_multi(symbol, interval, total=6000):
    """Sayfa sayfa geriye giderek uzun geçmiş (derin backtest ~62 gün)."""
    pages, end = [], None
    while sum(len(p) for p in pages) < total:
        params = dict(category="linear", symbol=symbol,
                      interval=_IV.get(interval, interval), limit=1000)
        if end:
            params["end"] = end
        d = requests.get(f"{FAPI}/v5/market/kline", params=params, timeout=15).json()
        rows = d.get("result", {}).get("list", [])
        if not rows:
            break
        pages.append(rows)
        end = int(rows[-1][0]) - 1
        if len(rows) < 1000:
            break
    rows = [k for p in pages for k in p]
    if not rows:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                       for k in rows],
                      columns=["t", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df

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
def _finish(ctx, name, direction, zone_bottom, zone_top, extra_conf, require_conf=True):
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

STRATS = [strategy_pullback, strategy_fvg, strategy_sweep, strategy_breaker]
_BY_NAME = {f.__name__: f for f in STRATS}
ACTIVE_STRATS = ["strategy_sweep", "strategy_breaker"]   # canlıda açık stratejiler

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
    except Exception:
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
    seen, tek = set(), []
    for r in results:
        if (r["symbol"], r["direction"]) in seen:
            continue
        seen.add((r["symbol"], r["direction"]))
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
   Giriş        : {sig['entry']:.6g}
   Giriş Mumu   : {sig['candle']}

4️⃣ STOP LOSS
   SL           : {sig['sl']:.6g}  (bölge/swing + ATR tamponu, min %0.8)
   Risk         : {risk_amt:.2f} USDT (%{risk_pct})
   Pozisyon     : {qty:.6g} adet

5️⃣ ÇIKIŞ PLANI (kısmi TP)
   TP           : {sig['tp']:.6g}
   Plan         : %50 kâr @1R → SL girişe (BE) → kalan %50 @TP
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