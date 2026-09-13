"""SMC (Smart Money Concepts) analiz motoru"""
import numpy as np
import pandas as pd
from dataclasses import dataclass

@dataclass
class Swing:
    idx: int
    price: float
    kind: str  # 'H' | 'L'

# ---------- Market Structure ----------
def find_swings(df: pd.DataFrame, lb: int = 3) -> list:
    """Fractal swing point'ler: iki taraftan lb mum üstünde/altında olan tepeler-dipler"""
    h, l = df["high"].values, df["low"].values
    out = []
    for i in range(lb, len(df) - lb):
        if h[i] == h[i - lb: i + lb + 1].max():
            out.append(Swing(i, float(h[i]), "H"))
        if l[i] == l[i - lb: i + lb + 1].min():
            out.append(Swing(i, float(l[i]), "L"))
    return out

def trend(swings: list) -> str:
    """Son 2 swing high + 2 swing low ile trend: HH+HL=BULLISH, LH+LL=BEARISH"""
    hs = [s.price for s in swings if s.kind == "H"][-2:]
    ls = [s.price for s in swings if s.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "NÖTR"
    if hs[-1] > hs[-2] and ls[-1] > ls[-2]:
        return "BULLISH"
    if hs[-1] < hs[-2] and ls[-1] < ls[-2]:
        return "BEARISH"
    return "NÖTR"

# ---------- Fair Value Gap ----------
def find_fvgs(df: pd.DataFrame, scan=150) -> list:
    """3 mumluk dengesizlik. filled=True = doldurulmuş (geçersiz)"""
    fvgs = []
    h, l = df["high"].values, df["low"].values
    start = max(2, len(df) - scan)
    for i in range(start, len(df)):
        if l[i] > h[i - 2]:  # Bullish FVG
            filled = bool((l[i + 1:] <= h[i - 2]).any()) if i + 1 < len(df) else False
            fvgs.append(dict(type="bullish", bottom=float(h[i-2]), top=float(l[i]), idx=i, filled=filled))
        if h[i] < l[i - 2]:  # Bearish FVG
            filled = bool((h[i + 1:] >= l[i - 2]).any()) if i + 1 < len(df) else False
            fvgs.append(dict(type="bearish", bottom=float(h[i]), top=float(l[i-2]), idx=i, filled=filled))
    return fvgs

# ---------- Order Block ----------
def find_order_blocks(df: pd.DataFrame, scan=150) -> list:
    """Impuls öncesi son karşı mum. Impuls: ortalama gövdenin 1.5 katı"""
    obs = []
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    avg = pd.Series(body).rolling(20).mean().values
    start = max(21, len(df) - scan)
    for i in range(start, len(df) - 9):
        n = i + 1
        strong = body[n] > 1.5 * max(avg[i], 1e-12)
        if c[i] < o[i] and c[n] > o[n] and strong and h[n:n+8].max() > h[i]:
            mitigated = bool((l[n+9:] <= h[i]).any())
            obs.append(dict(type="bullish", top=float(h[i]), bottom=float(l[i]), idx=i, mitigated=mitigated))
        if c[i] > o[i] and c[n] < o[n] and strong and l[n:n+8].min() < l[i]:
            mitigated = bool((h[n+9:] >= l[i]).any())
            obs.append(dict(type="bearish", top=float(h[i]), bottom=float(l[i]), idx=i, mitigated=mitigated))
    return obs

# ---------- Likidite Süpürmesi (Stop Hunt) ----------
def liquidity_sweep(df: pd.DataFrame, swings: list, lookback=5) -> dict:
    """Son mumlarda swing seviyesi fitille ihlal + karşı tarafa kapanış"""
    win, last = df.iloc[-lookback:], df.iloc[-1]
    res = dict(bull=False, bear=False)
    for s in [x for x in swings if x.kind == "L"][-5:]:
        if (win["low"] < s.price).any() and last["close"] > s.price:
            res["bull"] = True; break
    for s in [x for x in swings if s.kind == "H"][-5:]:
        if (win["high"] > s.price).any() and last["close"] < s.price:
            res["bear"] = True; break
    return res

# ---------- Premium / Discount ----------
def premium_discount(df: pd.DataFrame, swings: list):
    hs = [s.price for s in swings if s.kind == "H"][-3:]
    ls = [s.price for s in swings if s.kind == "L"][-3:]
    if not hs or not ls:
        return None
    rh, rl = max(hs), min(ls)
    eq = (rh + rl) / 2
    px = float(df["close"].iloc[-1])
    zone = "İNDİRİM" if px < eq else ("PRİM" if px > eq else "DENGELİK")
    return dict(range_high=rh, range_low=rl, eq=eq, zone=zone, price=px)

# ---------- Onay Mumu ----------
def confirmation(df: pd.DataFrame, direction: str):
    c, p = df.iloc[-1], df.iloc[-2]
    rng = c["high"] - c["low"]
    if direction == "LONG":
        if c["close"] > p["high"] and c["close"] > c["open"]:
            return True, "Bullish Engulfing"
        if rng > 0 and (c["close"] - c["low"]) / rng > 0.65 and c["close"] > c["open"]:
            return True, "Güçlü Bullish Kapanış"
    else:
        if c["close"] < p["low"] and c["close"] < c["open"]:
            return True, "Bearish Engulfing"
        if rng > 0 and (c["high"] - c["close"]) / rng > 0.65 and c["close"] < c["open"]:
            return True, "Güçlü Bearish Kapanış"
    return False, ""

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()