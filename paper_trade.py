"""Paper trade motoru v2 — BAKİYE TAKİPLİ PORTFÖY SİMÜLASYONU.
Başlangıç bakiyesinden her sinyal %RISK ile pozisyon açar; SL/TP/BE simülasyonu
bakiyeye işlenir. Equity geçmişi paper.json'da birikir → rapor istatistik + eğri çizer.
State: paper.json | report.py'den çağrılır | tek başına: python paper_trade.py"""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import scanner

FEE_RT   = 0.001          # işlem ücreti (notional, R'ye çevrilerek düşülür)
MAX_BARS = 96             # 15m × 96 = 24 saat timeout
MAX_OPEN = 6
MAX_SAME_DIR = 3          # korelasyon limiti: aynı yönde en fazla 3 pozisyon
RISK_PCT = 1.0            # işlem başına risk (% — bakiyeden)
START_BALANCE = 1000.0    # simülasyon başlangıç bakiyesi
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
    st.setdefault("equity", [])          # [(zaman, bakiye), ...]
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

def _equity_point(st, now=None):
    ts = (now or datetime.now(TR)).strftime("%d.%m %H:%M")
    st["equity"].append([ts, round(st["balance"], 2)])
    # eğri hafif kalsın: son 300 nokta yeter
    if len(st["equity"]) > 300:
        st["equity"] = st["equity"][-300:]

def evaluate():
    """Açık pozisyonları son kontrolden bu yana olan 15m mumlarla değerlendirir.
    Kapanan işlemlerin PnL'i bakiyeye işlenir (compounding)."""
    st = _load()
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

                if hit_sl:                                        # SL önce (muhafazakâr)
                    r = (0.5 if p["half"] else -1.0)
                    cp, cat = p["sl"], (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M")
                elif hit_r1:                                      # %50 kâr @1R → SL girişe (BE)
                    p["half"] = True
                    p["last_check"] = str(bar["t"]); continue
                elif hit_tp:
                    r = (0.5 + 0.5 * p["rr_raw"]) if p["half"] else p["rr_raw"]
                    cp, cat = p["tp"], (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M")
                elif p["bars"] >= MAX_BARS:                       # 24 saat → piyasadan kapat
                    px = float(bar["close"])
                    raw = (px - p["entry"]) / p["risk"] if p["direction"] == "LONG" \
                          else (p["entry"] - px) / p["risk"]
                    r = (0.5 + 0.5 * raw) if p["half"] else raw
                    cp, cat = px, (bar["t"] + timedelta(hours=3)).strftime("%d.%m %H:%M") + " (24s)"
                else:
                    p["last_check"] = str(bar["t"]); continue

                fee = FEE_RT * p["entry"] / p["risk"]
                net_r = r - fee
                pnl = net_r * p["risk_usdt"]                      # 1R = risk_usdt
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
    """Yeni sinyalleri sanal pozisyona çevirir:
    - bakiye %risk ile risk_usdt hesaplanır (compounding)
    - aynı coin+strateji+yön açıkken tekrar açılmaz
    - aynı yönde en fazla MAX_SAME_DIR pozisyon (korelasyon limiti)
    - toplam açık pozisyon ≤ max_open"""
    st = _load()
    bal = balance if balance is not None else st["balance"]
    have = {(p["symbol"], p["strategy"], p["direction"]) for p in st["open"]}
    dir_count = {"LONG": sum(1 for p in st["open"] if p["direction"] == "LONG"),
                 "SHORT": sum(1 for p in st["open"] if p["direction"] == "SHORT")}
    opened = 0
    for s in sigs:
        if len(st["open"]) >= max_open:
            break
        d = "LONG" if s["direction"] == "LONG" else "SHORT"
        if dir_count[d] >= MAX_SAME_DIR:
            continue
        key = (s["symbol"], s["strategy"], d)
        if key in have:
            continue
        risk = abs(s["entry"] - s["sl"])
        if risk <= 0:
            continue
        risk_usdt = round(bal * risk_pct / 100, 2)
        if risk_usdt < 1:                     # bakiye eridüyse min. 1$ risk altına inme
            risk_usdt = 1.0
        st["open"].append(dict(
            symbol=s["symbol"], strategy=s["strategy"], direction=d,
            entry=s["entry"], sl=s["sl"], tp=s["tp"], risk=risk,
            risk_usdt=risk_usdt,
            r1=s["entry"] + (risk if d == "LONG" else -risk),
            rr_raw=s["rr"], half=False, bars=0, score=s["score"],
            opened=datetime.now(TR).isoformat(timespec="minutes"),
            last_check=str(s["time"]),
            page_id=s.get("notion_page_id")))
        have.add(key)
        dir_count[d] += 1
        opened += 1
    _save(st)
    if opened:
        print(f"✅ Paper: {opened} yeni sanal pozisyon açıldı "
              f"(açık {len(st['open'])}, bakiye {st['balance']:.2f}).")
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
        closed=cl[-15:][::-1],
        equity=st["equity"][-120:],
        n_open=len(st["open"]), n_closed=len(cl),
        balance=bal, start_balance=st["start_balance"],
        pnl_total=pnl_total, pnl_pct=pnl_pct,
        wr=wr, pf=pf, exp_r=exp_r)

if __name__ == "__main__":
    evaluate()
    s = summary()
    print(f"Bakiye: {s['balance']:.2f} ({s['pnl_total']:+.2f} USDT, %{s['pnl_pct']:+.2f}) | "
          f"Açık: {s['n_open']} | Kapanan: {s['n_closed']} | "
          f"WR: {s['wr'] if s['wr'] is not None else '—'}% | PF: {s['pf']}")