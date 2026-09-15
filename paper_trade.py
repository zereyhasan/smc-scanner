"""Paper trade motoru v2.1 — BAKİYE + PENDING (limit emir) desteği.
MPL gibi 'pending=True' sinyalleri 'pending' listesinde bekler:
  - fiyat limit'e dokunursa → pozisyona dönüşür (bars sayacı başlar)
  - expiry_bars boyunca dokunmazsa → emir iptal
Aynı coin+strateji+yön pending'de/açıktayken tekrar emir açılmaz."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import scanner

FEE_RT   = 0.001
MAX_BARS = 96
MAX_OPEN = 6
MAX_SAME_DIR = 3
RISK_PCT = 1.0
START_BALANCE = 1000.0
TR = timezone(timedelta(hours=3))
STATE = Path("paper.json")

def _load():
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        st = {}
    st.setdefault("balance", START_BALANCE)
    st.setdefault("start_balance", START_BALANCE)
    st.setdefault("open", [])
    st.setdefault("closed", [])
    st.setdefault("pending", [])
    st.setdefault("equity", [])
    return st

def _save(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

def _label(r):
    return "WIN" if r > 0.05 else ("BE" if r >= -0.05 else "LOSS")

def _notion_result(rec, balance_after):
    try:
        import notion
        if rec.get("page_id"):
            notion.update_result(rec["page_id"],
                f'{rec["result_label"]} {rec["result_r"]:+.2f}R '
                f'({rec["pnl_usdt"]:+.2f} USDT · bakiye {balance_after:.1f})')
    except Exception as e:
        print(f"  ⚠ Notion sonucu atlandı: {e!r}")

def _equity_point(st):
    st["equity"].append([datetime.now(TR).strftime("%d.%m %H:%M"), round(st["balance"], 2)])
    if len(st["equity"]) > 300:
        st["equity"] = st["equity"][-300:]

def evaluate():
    """1) Pending emirler: limit dokunuşu → pozisyon / expiry → iptal
       2) Açık pozisyonlar: SL→BE→TP simülasyonu, kapanışlar bakiyeye işlenir."""
    st = _load()
    # ---- pending emirler ----
    still_pending = []
    for o in st["pending"]:
        try:
            df = scanner.klines(o["symbol"], "15m", 200)
            last = datetime.fromisoformat(o["last_check"])
            candles = df[df["t"] > last]
            filled = False
            for _, bar in candles.iterrows():
                o["wait_bars"] += 1
                touched = (bar["high"] >= o["limit"] >= bar["low"])
                if o["direction"] == "LONG":
                    invalid = bar["high"] >= o["sl"]
                else:
                    invalid = bar["low"] <= o["sl"]
                if touched:
                    o["entry"] = o["limit"]          # limit fill
                    st["open"].append(o)
                    filled = True
                    print(f"  ✎ Paper: {o['symbol']} MPL limit doldu → pozisyon")
                    break
                if invalid or o["wait_bars"] >= o["expiry_bars"]:
                    filled = True                    # iptal (open'a girmeden biter)
                    print(f"  ✎ Paper: {o['symbol']} MPL emri {(o['wait_bars']>=o['expiry_bars']) and 'süre doldu' or 'SL seviyesi görüldü — iptal'}")
                    break
            if not filled:
                if len(candles):
                    o["last_check"] = str(candles["t"].iloc[-1])
                still_pending.append(o)
        except Exception as e:
            print(f"  ⚠ pending {o['symbol']}: {e!r}")
            still_pending.append(o)
    st["pending"] = still_pending

    # ---- açık pozisyonlar (aynen v2) ----
    still = []
    for p in st["open"]:
        try:
            df = scanner.klines(p["symbol"], "15m", 200)
            last = datetime.fromisoformat(p["last_check"])
            candles = df[df["t"] > last]
            closed = False
            for _, bar in candles.iterrows():
                p["bars"] += 1
                if p["direction"] == "LONG":
                    hit_sl = bar["low"]  <= p["sl"]
                    hit_r1 = (not p["half"]) and bar["high"] >= p["r1"]
                    hit_tp = bar["high"] >= p["tp"]
                else:
                    hit_sl = bar["high"] >= p["sl"]
                    hit_r1 = (not p["half"]) and bar["low"]  <= p["r1"]
                    hit_tp = bar["low"]  <= p["tp"]
                if hit_sl:
                    r = (0.5 if p["half"] else -1.0)
                    cp, cat = p["sl"], (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M")
                elif hit_r1:
                    p["half"] = True
                    p["last_check"] = str(bar["t"]); continue
                elif hit_tp:
                    r = (0.5 + 0.5 * p["rr_raw"]) if p["half"] else p["rr_raw"]
                    cp, cat = p["tp"], (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M")
                elif p["bars"] >= MAX_BARS:
                    px = float(bar["close"])
                    raw = (px - p["entry"]) / p["risk"] if p["direction"] == "LONG" \
                          else (p["entry"] - px) / p["risk"]
                    r = (0.5 + 0.5 * raw) if p["half"] else raw
                    cp, cat = px, (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M") + " (24s)"
                else:
                    p["last_check"] = str(bar["t"]); continue
                fee = FEE_RT * p["entry"] / p["risk"]
                net_r = r - fee
                pnl = net_r * p["risk_usdt"]
                st["balance"] = round(st["balance"] + pnl, 2)
                rec = dict(p)
                rec.update(result_r=round(net_r, 3), result_label=_label(net_r),
                           pnl_usdt=round(pnl, 2), balance_after=st["balance"],
                           close_price=cp, closed=cat)
                st["closed"].append(rec)
                _notion_result(rec, st["balance"])
                closed = True
                break
            if closed:
                continue
            if len(candles):
                p["last_check"] = str(candles["t"].iloc[-1])
            still.append(p)
        except Exception as e:
            print(f"  ⚠ paper {p['symbol']}: {e!r}")
            still.append(p)
    st["open"] = still
    _equity_point(st)
    _save(st)

def open_positions(sigs, max_open=MAX_OPEN, balance=None, risk_pct=RISK_PCT):
    """pending=True sinyaller → 'pending' listesine limit emir;
    normal sinyaller → doğrudan pozisyon (v2 davranışı)."""
    st = _load()
    bal = balance if balance is not None else st["balance"]
    have = {(p["symbol"], p["strategy"], p["direction"]) for p in st["open"]}
    have |= {(o["symbol"], o["strategy"], o["direction"]) for o in st["pending"]}
    dir_count = {"LONG": sum(1 for p in st["open"] if p["direction"] == "LONG"),
                 "SHORT": sum(1 for p in st["open"] if p["direction"] == "SHORT")}
    opened, pend = 0, 0
    for s in sigs:
        d = "LONG" if s["direction"] == "LONG" else "SHORT"
        key = (s["symbol"], s["strategy"], d)
        if key in have:
            continue
        is_pending = bool(s.get("pending"))
        if not is_pending:
            if len(st["open"]) >= max_open:
                continue
            if dir_count[d] >= MAX_SAME_DIR:
                continue
        risk = abs((s.get("limit", s["entry"])) - s["sl"])
        if risk <= 0:
            continue
        risk_usdt = round(max(bal * risk_pct / 100, 1.0), 2)
        if is_pending:
            st["pending"].append(dict(
                symbol=s["symbol"], strategy=s["strategy"], direction=d,
                limit=s["entry"], sl=s["sl"], tp=s["tp"], risk=risk,
                risk_usdt=risk_usdt,
                rr_raw=s["rr"], score=s["score"],
                expiry_bars=s.get("expiry_bars", 96), wait_bars=0,
                opened=datetime.now(TR).isoformat(timespec="minutes"),
                last_check=str(s["time"]),
                page_id=s.get("notion_page_id")))
            have.add(key); pend += 1
        else:
            entry = s["entry"]
            st["open"].append(dict(
                symbol=s["symbol"], strategy=s["strategy"], direction=d,
                entry=entry, sl=s["sl"], tp=s["tp"], risk=risk,
                risk_usdt=risk_usdt,
                r1=entry + (risk if d == "LONG" else -risk),
                rr_raw=s["rr"], half=False, bars=0, score=s["score"],
                opened=datetime.now(TR).isoformat(timespec="minutes"),
                last_check=str(s["time"]),
                page_id=s.get("notion_page_id")))
            have.add(key); dir_count[d] += 1; opened += 1
    _save(st)
    if opened or pend:
        print(f"✅ Paper: {opened} pozisyon, {pend} limit emir eklendi "
              f"(açık {len(st['open'])}, bekleyen {len(st['pending'])}, bakiye {st['balance']:.2f}).")
    return opened

def summary():
    st = _load()
    cl = st["closed"]
    bal = st["balance"]
    pnl_total = round(bal - st["start_balance"], 2)
    pnl_pct = round(100 * pnl_total / st["start_balance"], 2)
    wins = sum(1 for c in cl if c["result_r"] > 0.05)
    wr = round(100 * wins / len(cl), 1) if cl else None
    gross = sum(c["result_r"] for c in cl if c["result_r"] > 0)
    loss = -sum(c["result_r"] for c in cl if c["result_r"] < 0)
    pf = round(gross / loss, 2) if loss > 0 else (9.99 if gross > 0 else 0.0)
    exp_r = round(sum(c["result_r"] for c in cl) / len(cl), 3) if cl else 0.0
    return dict(
        open=st["open"][-12:][::-1],
        pending=st["pending"][-8:][::-1],
        closed=cl[-15:][::-1],
        equity=st["equity"][-120:],
        n_open=len(st["open"]), n_pending=len(st["pending"]), n_closed=len(cl),
        balance=bal, start_balance=st["start_balance"],
        pnl_total=pnl_total, pnl_pct=pnl_pct,
        wr=wr, pf=pf, exp_r=exp_r)

if __name__ == "__main__":
    evaluate()
    s = summary()
    print(f"Bakiye: {s['balance']:.2f} ({s['pnl_total']:+.2f} USDT, %{s['pnl_pct']:+.2f}) | "
          f"Açık: {s['n_open']} | Bekleyen: {s['n_pending']} | Kapanan: {s['n_closed']} | "
          f"WR: {s['wr'] if s['wr'] is not None else '—'}% | PF: {s['pf']}")