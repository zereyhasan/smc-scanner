"""SMC sinyallerini Notion'a yazar (v3: Tarih sütunu + aynı gün kopya koruması).
Bağımsız: python notion.py 10   |   report.py içinden: notion.write_signals(sigs, top=10)"""
import sys
from datetime import datetime, timezone, timedelta
import requests
import scanner

try:
    from config import NOTION_TOKEN, NOTION_DB_ID
except ImportError:
    raise SystemExit("config.py yok! config_örnek.py'i config.py olarak kopyala, değerleri doldur.")
TOKEN = NOTION_TOKEN
DB    = NOTION_DB_ID
VER   = "2022-06-28"
TR    = timezone(timedelta(hours=3))         # Türkiye saati

def _headers():
    return {"Authorization": f"Bearer {TOKEN}",
            "Notion-Version": VER,
            "Content-Type": "application/json"}

def _txt(props, name):
    parts = props.get(name, {}).get("rich_text", [])
    return parts[0].get("plain_text", "") if parts else ""

def _existing_today():
    """Bugün zaten yazılmış (Parite, Strateji, Yön) üçlülerini döndürür.
    Tarih sütunu yoksa/hata olursa boş küme → her şey yazılır (eski davranış)."""
    today = datetime.now(TR).strftime("%Y-%m-%d")
    try:
        r = requests.post(f"https://api.notion.com/v1/databases/{DB}/query",
                          headers=_headers(), timeout=15,
                          json={"filter": {"property": "Tarih",
                                           "date": {"on_or_after": today}},
                                "page_size": 100})
        if r.status_code != 200:
            return set()
        keys = set()
        for page in r.json().get("results", []):
            p = page.get("properties", {})
            keys.add((_txt(p, "Parite"), _txt(p, "Strateji"), _txt(p, "Yön")))
        return keys
    except Exception:
        return set()

def _page(r, risk_pct=1.0, balance=1000.0):
    risk_amt = balance * risk_pct / 100
    qty = risk_amt / abs(r["entry"] - r["sl"])
    arrow = "LONG" if r["direction"] == "LONG" else "SHORT"
    return {
        "parent": {"database_id": DB},
        "icon": {"emoji": "🟢" if r["direction"] == "LONG" else "🔴"},
        "properties": {
            "Ad":     {"title": [{"text": {"content": f"{r['symbol']} {arrow} {r['time']:%d.%m %H:%M}"}}]},
            "Tarih":  {"date": {"start": datetime.now(TR).isoformat(timespec="minutes")}},
            "Parite":   {"rich_text": [{"text": {"content": r["symbol"]}}]},
            "Strateji": {"rich_text": [{"text": {"content": r["strategy"]}}]},
            "Yön":      {"rich_text": [{"text": {"content": arrow}}]},
            "TF":       {"rich_text": [{"text": {"content": "15M (Giriş) / 1H (Trend)"}}]},
            "Skor":     {"rich_text": [{"text": {"content": str(r["score"])}}]},
            "RR":       {"rich_text": [{"text": {"content": f'1:{r["rr"]}'}}]},
            "Giriş":    {"rich_text": [{"text": {"content": f'{r["entry"]:.6g}'}}]},
            "SL":       {"rich_text": [{"text": {"content": f'{r["sl"]:.6g}'}}]},
            "TP":       {"rich_text": [{"text": {"content": f'{r["tp"]:.6g}'}}]},
            "Pozisyon": {"rich_text": [{"text": {"content": f'{qty:.4g} adet @ %{risk_pct} risk'}}]},
        },
        "children": [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content":
                "✔ " + " | ".join(r["confluences"]) +
                f"\nÇIKIŞ PLANI: %50 kâr @1R → SL girişe (BE) → kalan %50 @TP {r['tp']:.6g}"}}]}
        }],
    }

def write_signals(res, top=10, balance=1000.0, risk_pct=1.0):
    """Sinyal listesini Notion'a yazar. Aynı gün içinde aynı (coin+strateji+yön)
    zaten varsa ATLANIR — taze tarihle tekrar yazılmaz."""
    if not res:
        print("Notion: yazılacak sinyal yok.")
        return 0
    mevcut = _existing_today()
    ok, atlanan = 0, 0
    for r in res[:top]:
        key = (r["symbol"], r["strategy"],
               "LONG" if r["direction"] == "LONG" else "SHORT")
        if key in mevcut:
            atlanan += 1
            continue
        try:
            resp = requests.post("https://api.notion.com/v1/pages",
                                 headers=_headers(),
                                 json=_page(r, risk_pct, balance), timeout=15)
            if resp.status_code in (200, 201):
                ok += 1
            else:
                print(f"  ⚠ Notion {r['symbol']}: HTTP {resp.status_code} — {resp.text[:150]}")
        except Exception as e:
            print(f"  ⚠ Notion {r['symbol']}: {e!r}")
    print(f"✅ Notion: {ok} yeni sinyal yazıldı" +
          (f", {atlanan} kopya atlandı (bugün zaten vardı)." if atlanan else "."))
    return ok

def send_signals(top=10, balance=1000.0, risk_pct=1.0):
    """Bağımsız mod: kendisi tarar ve yazar"""
    res = scanner.scan(100)
    write_signals(res, top, balance, risk_pct)

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    send_signals(top=n)