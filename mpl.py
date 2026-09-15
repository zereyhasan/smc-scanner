"""Maximum Pain Level (MPL) — v1.2.4
v1.2.4 FIX: bacak kökü tanımı daraltıldı.
  ESKİ (hatalı): MSB'den geriye pencere EN DERİN dibi alıyordu → risk patlıyor, TP ulaşılmaz
  YENİ: MSB'den geriye İLK ONAYLI 15M swing low = yükseliş bacağının başladığı dip.
        (Kullanıcı çizimindeki 'hareketin kökü' tam olarak bu dönüş noktası.)
  Sonra kök zamanı 1H'a map'lenir (±1 mum penceresi min low) + yapısal extreme güvencesi.
v1.2: TP=2R sabit, kısmi TP kapalı | v1.1: IDM şartı | havuz: birbirini yememiş tepe/dip"""
import numpy as np
import pandas as pd
import smc

POOL_MIN_MEMBERS = 2
TOL_ATR          = 0.35
LTF_EXPIRY_BARS  = 96
IDM_WINDOW       = 20
IDM_PROMINENCE   = 1.0
TP_R             = 2.0
MAX_RISK_PCT     = 0.05   # güvenlik: risk, fiyatın %5'ini aşamaz (bozuk geometri koruması)

def _find_idm_high(df, end_idx: int):
    start = max(0, end_idx - IDM_WINDOW)
    if end_idx - start < 5:
        return None
    h = df["high"].values
    l = df["low"].values
    atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[:end_idx].iloc[-1] or 0)
    if atr <= 0:
        return None
    window_top = float(h[start:end_idx].max())
    window_bot = float(l[start:end_idx].min())
    if (window_top - window_bot) < IDM_PROMINENCE * atr:
        return None
    return window_top

def _find_idm_low(df, end_idx: int):
    start = max(0, end_idx - IDM_WINDOW)
    if end_idx - start < 5:
        return None
    h = df["high"].values
    l = df["low"].values
    atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[:end_idx].iloc[-1] or 0)
    if atr <= 0:
        return None
    window_bot = float(l[start:end_idx].min())
    window_top = float(h[start:end_idx].max())
    if (window_top - window_bot) < IDM_PROMINENCE * atr:
        return None
    return window_bot

def _clean_pool(prices_idx: list, highs: list) -> list:
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

def _leg_root_time(ltf, msb_idx: int, ltf_swings) -> tuple:
    """SHORT: MSB'den geriye İLK onaylı 15M swing low = yükselişin başladığı dip (kök)."""
    for s in sorted(ltf_swings, key=lambda s: -s.idx):
        if s.idx < msb_idx and s.kind == "L":
            return ltf.loc[s.idx, "t"], float(ltf.loc[s.idx, "low"])
    # swing yoksa (nadir): 10 mumluk mini pencere dibi
    window = ltf.iloc[max(0, msb_idx - 10):msb_idx + 1]
    lo_i = window["low"].idxmin()
    return ltf.loc[lo_i, "t"], float(ltf.loc[lo_i, "low"])

def _leg_root_time_long(ltf, msb_idx: int, ltf_swings) -> tuple:
    """LONG: MSB'den geriye İLK onaylı 15M swing high = düşüşün başladığı tepe (kök)."""
    for s in sorted(ltf_swings, key=lambda s: -s.idx):
        if s.idx < msb_idx and s.kind == "H":
            return ltf.loc[s.idx, "t"], float(ltf.loc[s.idx, "high"])
    window = ltf.iloc[max(0, msb_idx - 10):msb_idx + 1]
    hi_i = window["high"].idxmax()
    return ltf.loc[hi_i, "t"], float(ltf.loc[hi_i, "high"])

def _sl_anchor_short_1h(htf, root_time, htf_swings):
    hs = [s.price for s in htf_swings if s.kind == "H"]
    idx_near = int((htf["t"] - root_time).abs().idxmin())
    start = max(0, idx_near - 1)
    end = min(len(htf), idx_near + 2)
    root_low = float(htf["low"].iloc[start:end].min())
    structural = hs[-1] if hs else None
    if structural:
        return max(root_low, min(structural, root_low + (root_low * 0.02)))
    return root_low

def _sl_anchor_long_1h(htf, root_time, htf_swings):
    ls = [s.price for s in htf_swings if s.kind == "L"]
    idx_near = int((htf["t"] - root_time).abs().idxmin())
    start = max(0, idx_near - 1)
    end = min(len(htf), idx_near + 2)
    root_high = float(htf["high"].iloc[start:end].max())
    structural = ls[-1] if ls else None
    if structural:
        return min(root_high, max(structural, root_high - (root_high * 0.02)))
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

    # ---------- SHORT ----------
    if h_lows:
        hl_price = h_lows[-1].price
        closes = ltf["close"].values[-3:]
        msb_idx = None
        for k in range(len(closes) - 1, -1, -1):
            if closes[k] < hl_price:
                msb_idx = n15 - (len(closes) - k); break
        if msb_idx is None:
            return None
        idm = _find_idm_high(ltf, msb_idx)
        if idm is None:
            return None
        cands = [f for f in ctx["fvgs"] if f["type"] == "bearish" and not f["filled"]
                 and abs(f["idx"] - msb_idx) <= 4]
        if not cands:
            return None
        fvg = max(cands, key=lambda f: (f["top"] - f["bottom"]))
        mid = (fvg["top"] + fvg["bottom"]) / 2.0
        if ctx["price"] >= mid:
            return None
        pools_h = _pool_stats(htf, htf_swings, "H") or _pool_stats(ltf, ltf_swings, "H")
        pools_h = [p for p in pools_h if p["level"] > fvg["top"]]
        score = 25
        members = max((p["members"] for p in pools_h), default=0)
        if members >= POOL_MIN_MEMBERS:
            score += 5 + 5 * (members - POOL_MIN_MEMBERS)
            if members >= 3: score += 5
        else:
            return None
        if any(f["bottom"] > p["level"] for p in pools_h for f in [fvg]):
            score += 10
        if ctx["sweep"]["bear"]: score += 5
        score += 10
        root_time, _ = _leg_root_time(ltf, msb_idx, ltf_swings)
        anchor = _sl_anchor_short_1h(htf, root_time, htf_swings)
        sl = anchor + max(ctx["atr"] * 0.25, ctx["price"] * 0.0015)
        risk = sl - mid
        # v1.2.4 güvenlik: risk fiyatın %5'ini aşamaz (geometri koruması)
        if risk <= 0 or risk > ctx["price"] * MAX_RISK_PCT:
            return None
        tp = mid - TP_R * risk
        rr = TP_R
        conf = [f"MPL: {members} üyeli tepe havuzu (likidite almamış)",
                "MSB: 1H HL altına 15M kapanış",
                f"IDM alındı: ara tepe {idm:.6g} süpürüldü",
                "Giriş: FVG %50 (CE) limit — 24s ömür",
                f"SL: bacak kökü (ilk swing low) 1H extreme {anchor:.6g}+",
                f"TP: sabit {TP_R}R (kısmi TP kapalı)"]
        return dict(kind="MPL_PENDING", symbol=ctx["symbol"], strategy="Maximum Pain Level",
                    direction="SHORT", limit=mid, sl=sl, tp=tp, rr=round(rr, 2),
                    score=min(score, 100), expiry_bars=LTF_EXPIRY_BARS,
                    confluences=conf, time=ltf["t"].iloc[-1])

    # ---------- LONG (ayna) ----------
    if h_highs:
        lh_price = h_highs[-1].price
        closes = ltf["close"].values[-3:]
        msb_idx = None
        for k in range(len(closes) - 1, -1, -1):
            if closes[k] > lh_price:
                msb_idx = n15 - (len(closes) - k); break
        if msb_idx is None:
            return None
        idm = _find_idm_low(ltf, msb_idx)
        if idm is None:
            return None
        cands = [f for f in ctx["fvgs"] if f["type"] == "bullish" and not f["filled"]
                 and abs(f["idx"] - msb_idx) <= 4]
        if not cands:
            return None
        fvg = max(cands, key=lambda f: (f["top"] - f["bottom"]))
        mid = (fvg["top"] + fvg["bottom"]) / 2.0
        if ctx["price"] <= mid:
            return None
        pools_l = _pool_stats(htf, htf_swings, "L") or _pool_stats(ltf, ltf_swings, "L")
        pools_l = [p for p in pools_l if p["level"] < fvg["bottom"]]
        score = 25
        members = max((p["members"] for p in pools_l), default=0)
        if members >= POOL_MIN_MEMBERS:
            score += 5 + 5 * (members - POOL_MIN_MEMBERS)
            if members >= 3: score += 5
        else:
            return None
        if any(f["top"] < p["level"] for p in pools_l for f in [fvg]):
            score += 10
        if ctx["sweep"]["bull"]: score += 5
        score += 10
        root_time, _ = _leg_root_time_long(ltf, msb_idx, ltf_swings)
        anchor = _sl_anchor_long_1h(htf, root_time, htf_swings)
        sl = anchor - max(ctx["atr"] * 0.25, ctx["price"] * 0.0015)
        risk = mid - sl
        if risk <= 0 or risk > ctx["price"] * MAX_RISK_PCT:
            return None
        tp = mid + TP_R * risk
        rr = TP_R
        conf = [f"MPL: {members} üyeli dip havuzu (likidite almamış)",
                "MSB: 1H LH üstüne 15M kapanış",
                f"IDM alındı: ara dip {idm:.6g} süpürüldü",
                "Giriş: FVG %50 (CE) limit — 24s ömür",
                f"SL: bacak kökü (ilk swing high) 1H extreme {anchor:.6g}-",
                f"TP: sabit {TP_R}R (kısmi TP kapalı)"]
        return dict(kind="MPL_PENDING", symbol=ctx["symbol"], strategy="Maximum Pain Level",
                    direction="LONG", limit=mid, sl=sl, tp=tp, rr=round(rr, 2),
                    score=min(score, 100), expiry_bars=LTF_EXPIRY_BARS,
                    confluences=conf, time=ltf["t"].iloc[-1])
    return None