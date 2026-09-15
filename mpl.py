"""Maximum Pain Level (MPL) — v1.3.1
v1.3.1 FIX (kullanıcı grafik bulgusu): DÜŞEN trendin ortasında sahte sinyal.
  Kök neden: havuz-süpürme bağlantısı hiç test edilmiyordu; Idm gevşek; MSB stale-HL
  ile tetiklenebiliyordu.
  Düzeltmeler:
  A) SÜPÜRME ZORUNLU: son 8×15M mumda havuz top'unu AŞAN + havuz altına KAPANAN mum
     aranır (multisweep mantığı). Yoksa → sinyal yok.
  B) Idm sıkılaştı: süpürme mumundan önceki 20 mumda ara tepe, VE havuz top'unun
     ALTINDA olmalı (Idm = süpürülene kadar alınmamış ara likidite).
  C) MSB tazelik: HL altına kapanıştan ÖNCE son 20 mum içinde HL ÜSTÜNDE kapanış
     olmalı (yapı gerçekten kırılıyor, çoktan kırılmış değil).
Zincir: havuz → süpürme → Idm → MSB → FVG %50 limit → kök-SL → havuz-TP."""
import numpy as np
import pandas as pd
import smc

POOL_MIN_MEMBERS = 2
TOL_ATR          = 0.35
LTF_EXPIRY_BARS  = 96
IDM_WINDOW       = 20
IDM_PROMINENCE   = 1.0
MIN_RR_STRUCTURE = 1.0
MAX_RISK_PCT     = 0.05
SWEEP_WINDOW     = 8     # havuz süpürmesi aranacak son N×15M mum
MSB_FRESH_WINDOW = 20    # MSB tazeliği: HL üstünde kapanış aralığı

def _find_idm_high(df, end_idx):
    start = max(0, end_idx - IDM_WINDOW)
    if end_idx - start < 5: return None
    h = df["high"].values; l = df["low"].values
    atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[:end_idx].iloc[-1] or 0)
    if atr <= 0: return None
    top = float(h[start:end_idx].max()); bot = float(l[start:end_idx].min())
    if (top - bot) < IDM_PROMINENCE * atr: return None
    return top

def _find_idm_low(df, end_idx):
    start = max(0, end_idx - IDM_WINDOW)
    if end_idx - start < 5: return None
    h = df["high"].values; l = df["low"].values
    atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[:end_idx].iloc[-1] or 0)
    if atr <= 0: return None
    bot = float(l[start:end_idx].min()); top = float(h[start:end_idx].max())
    if (top - bot) < IDM_PROMINENCE * atr: return None
    return bot

def _clean_pool(prices_idx, highs):
    members = []
    for k, (px, ix) in enumerate(prices_idx):
        if k == 0:
            members.append((px, ix)); continue
        prev_px, prev_ix = members[-1]
        between_max = max(highs[prev_ix:ix + 1]) if ix > prev_ix else prev_px
        if between_max > prev_px:
            members[-1] = (px, ix)
        else:
            members.append((px, ix))
    return members

def _pool_stats(df, swings, kind):
    highs = df["high"].values; lows = df["low"].values
    atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[-1] or 0)
    tol = max(atr * TOL_ATR, 1e-12)
    pools = []
    if kind == "H":
        pts = sorted([(s.price, s.idx) for s in swings if s.kind == "H"], key=lambda x: x[1])
        clean = _clean_pool(pts, highs)
        group = []
        for px, ix in sorted(clean, key=lambda x: x[0]):
            if group and px - group[-1][0] > tol:
                if len(group) >= POOL_MIN_MEMBERS:
                    pools.append(dict(level=max(g[0] for g in group), members=len(group),
                                      top=max(g[0] for g in group), bottom=min(g[0] for g in group)))
                group = []
            group.append((px, ix))
        if len(group) >= POOL_MIN_MEMBERS:
            pools.append(dict(level=max(g[0] for g in group), members=len(group),
                              top=max(g[0] for g in group), bottom=min(g[0] for g in group)))
    else:
        pts = sorted([(s.price, s.idx) for s in swings if s.kind == "L"], key=lambda x: x[1])
        clean = _clean_pool([(-px, ix) for px, ix in pts], -lows)
        clean = [(-px, ix) for px, ix in clean]
        group = []
        for px, ix in sorted(clean, key=lambda x: x[0]):
            if group and group[-1][0] - px > tol:
                if len(group) >= POOL_MIN_MEMBERS:
                    pools.append(dict(level=min(g[0] for g in group), members=len(group),
                                      top=max(g[0] for g in group), bottom=min(g[0] for g in group)))
                group = []
            group.append((px, ix))
        if len(group) >= POOL_MIN_MEMBERS:
            pools.append(dict(level=min(g[0] for g in group), members=len(group),
                              top=max(g[0] for g in group), bottom=min(g[0] for g in group)))
    return pools

def _leg_root_time(ltf, msb_idx, ltf_swings):
    for s in sorted(ltf_swings, key=lambda s: -s.idx):
        if s.idx < msb_idx and s.kind == "L":
            return ltf.loc[s.idx, "t"]
    window = ltf.iloc[max(0, msb_idx - 10):msb_idx + 1]
    return ltf.loc[window["low"].idxmin(), "t"]

def _leg_root_time_long(ltf, msb_idx, ltf_swings):
    for s in sorted(ltf_swings, key=lambda s: -s.idx):
        if s.idx < msb_idx and s.kind == "H":
            return ltf.loc[s.idx, "t"]
    window = ltf.iloc[max(0, msb_idx - 10):msb_idx + 1]
    return ltf.loc[window["high"].idxmax(), "t"]

def _sl_anchor_short_1h(htf, root_time, htf_swings):
    hs = [s.price for s in htf_swings if s.kind == "H"]
    idx_near = int((htf["t"] - root_time).abs().idxmin())
    root_low = float(htf["low"].iloc[max(0, idx_near - 1):min(len(htf), idx_near + 2)].min())
    structural = hs[-1] if hs else None
    if structural:
        return max(root_low, min(structural, root_low + root_low * 0.02))
    return root_low

def _sl_anchor_long_1h(htf, root_time, htf_swings):
    ls = [s.price for s in htf_swings if s.kind == "L"]
    idx_near = int((htf["t"] - root_time).abs().idxmin())
    root_high = float(htf["high"].iloc[max(0, idx_near - 1):min(len(htf), idx_near + 2)].max())
    structural = ls[-1] if ls else None
    if structural:
        return min(root_high, max(structural, root_high - root_high * 0.02))
    return root_high

def signal(ctx):
    htf, ltf = ctx["htf"], ctx["ltf"]
    n15 = len(ltf)
    if n15 < 60 or len(htf) < 60:
        return None
    htf_swings = ctx["htf_swings"]
    ltf_swings = smc.find_swings(ltf)
    h_lows  = [s for s in htf_swings if s.kind == "L"]
    h_highs = [s for s in htf_swings if s.kind == "H"]
    h = ltf["high"].values; l = ltf["low"].values; c = ltf["close"].values

    # ---------- SHORT: eşit-TEPE havuzu → süpürme → Idm → MSB → FVG %50 ----------
    pools_h = _pool_stats(ltf, ltf_swings, "H")
    for pool in pools_h:
        # (A) SÜPÜRME ZORUNLU: son SWEEP_WINDOW mumda top'u aşan + altına kapanan mum
        sweep_i = None
        for j in range(max(0, n15 - SWEEP_WINDOW), n15):
            if h[j] > pool["top"] and c[j] < pool["top"]:
                sweep_i = j; break
        if sweep_i is None:
            continue                                   # havuz süpürülmemiş → sıradaki havuz
        # (B) Idm: süpürmeden önceki ara tepe — havuzun ALTINDA kalmalı
        idm = _find_idm_high(ltf, sweep_i)
        if idm is None or idm >= pool["top"]:
            continue
        # (C) MSB tazeliği: süpürmeden önce HL ÜSTÜNDE kapanış, sonra altında kapanış
        if not h_lows:
            continue
        hl_price = h_lows[-1].price
        if c[sweep_i] >= hl_price:
            continue                                    # süpürme mumu hâlâ HL üstünde → MSB yok
        prev_close = c[max(0, sweep_i - MSB_FRESH_WINDOW):sweep_i]
        if not (prev_close > hl_price).any():
            continue                                    # çoktan kırılmıştı (stale HL) → geçersiz
        # (D) FVG: süpürme sonrası çöküşün bıraktığı bearish FVG
        cands = [f for f in ctx["fvgs"] if f["type"] == "bearish" and not f["filled"]
                 and f["idx"] >= sweep_i - 1]
        if not cands:
            continue
        fvg = max(cands, key=lambda f: (f["top"] - f["bottom"]))
        mid = (fvg["top"] + fvg["bottom"]) / 2.0
        if ctx["price"] >= mid:
            continue                                    # fiyat henüz geri dönmedi → limit bekler
        # havuz sayısı puanı (süpürülen bu havuz)
        members = pool["members"]
        score = 25 + 5 + 5 * (members - POOL_MIN_MEMBERS)
        if members >= 3: score += 5
        score += 10                                     # Idm kalite bonusu
        if ctx["sweep"]["bear"]: score += 5
        # SL: bacak kökü 1H extreme
        root_time = _leg_root_time(ltf, sweep_i, ltf_swings)
        anchor = _sl_anchor_short_1h(htf, root_time, htf_swings)
        sl = anchor + max(ctx["atr"] * 0.25, ctx["price"] * 0.0015)
        risk = sl - mid
        if risk <= 0 or risk > ctx["price"] * MAX_RISK_PCT:
            continue
        # TP: karşı (dip) havuzu — yoksa işlemin ilkesi yok
        pools_l = _pool_stats(ltf, ltf_swings, "L")
        cand_tp = [p for p in pools_l if p["level"] < ctx["price"]]
        if not cand_tp:
            continue
        tp_pool = max(cand_tp, key=lambda p: (p["members"], p["level"]))
        tp = tp_pool["level"]
        rr = (mid - tp) / risk
        if rr < MIN_RR_STRUCTURE:
            continue
        conf = [f"MPL: {members} üyeli tepe havuzu süpürüldü (Idm aşılı)",
                "MSB: 1H HL altına TAZE 15M kapanış",
                "Giriş: FVG %50 (CE) limit — 24s ömür",
                f"SL: bacak kökü 1H extreme {anchor:.6g}+ (kural)",
                f"TP: {tp_pool['members']} üyeli dip havuzu {tp:.6g} (yapısal)"]
        return dict(kind="MPL_PENDING", symbol=ctx["symbol"], strategy="Maximum Pain Level",
                    direction="SHORT", limit=mid, sl=sl, tp=tp, rr=round(rr, 2),
                    score=min(score, 100), expiry_bars=LTF_EXPIRY_BARS,
                    confluences=conf, time=ltf["t"].iloc[-1])

    # ---------- LONG: eşit-DİP havuzu → süpürme → Idm → MSB → FVG %50 (ayna) ----------
    pools_l = _pool_stats(ltf, ltf_swings, "L")
    for pool in pools_l:
        sweep_i = None
        for j in range(max(0, n15 - SWEEP_WINDOW), n15):
            if l[j] < pool["bottom"] and c[j] > pool["bottom"]:
                sweep_i = j; break
        if sweep_i is None:
            continue
        idm = _find_idm_low(ltf, sweep_i)
        if idm is None or idm <= pool["bottom"]:
            continue
        if not h_highs:
            continue
        lh_price = h_highs[-1].price
        if c[sweep_i] <= lh_price:
            continue
        prev_close = c[max(0, sweep_i - MSB_FRESH_WINDOW):sweep_i]
        if not (prev_close < lh_price).any():
            continue
        cands = [f for f in ctx["fvgs"] if f["type"] == "bullish" and not f["filled"]
                 and f["idx"] >= sweep_i - 1]
        if not cands:
            continue
        fvg = max(cands, key=lambda f: (f["top"] - f["bottom"]))
        mid = (fvg["top"] + fvg["bottom"]) / 2.0
        if ctx["price"] <= mid:
            continue
        members = pool["members"]
        score = 25 + 5 + 5 * (members - POOL_MIN_MEMBERS)
        if members >= 3: score += 5
        score += 10
        if ctx["sweep"]["bull"]: score += 5
        root_time = _leg_root_time_long(ltf, sweep_i, ltf_swings)
        anchor = _sl_anchor_long_1h(htf, root_time, htf_swings)
        sl = anchor - max(ctx["atr"] * 0.25, ctx["price"] * 0.0015)
        risk = mid - sl
        if risk <= 0 or risk > ctx["price"] * MAX_RISK_PCT:
            continue
        pools_h = _pool_stats(ltf, ltf_swings, "H")
        cand_tp = [p for p in pools_h if p["level"] > ctx["price"]]
        if not cand_tp:
            continue
        tp_pool = max(cand_tp, key=lambda p: (p["members"], p["level"]))
        tp = tp_pool["level"]
        rr = (tp - mid) / risk
        if rr < MIN_RR_STRUCTURE:
            continue
        conf = [f"MPL: {members} üyeli dip havuzu süpürüldü (Idm aşılı)",
                "MSB: 1H LH üstüne TAZE 15M kapanış",
                "Giriş: FVG %50 (CE) limit — 24s ömür",
                f"SL: bacak kökü 1H extreme {anchor:.6g}- (kural)",
                f"TP: {tp_pool['members']} üyeli tepe havuzu {tp:.6g} (yapısal)"]
        return dict(kind="MPL_PENDING", symbol=ctx["symbol"], strategy="Maximum Pain Level",
                    direction="LONG", limit=mid, sl=sl, tp=tp, rr=round(rr, 2),
                    score=min(score, 100), expiry_bars=LTF_EXPIRY_BARS,
                    confluences=conf, time=ltf["t"].iloc[-1])
    return None