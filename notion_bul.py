"""Entegrasyonun erişebildiği tüm veritabanlarını listeler: python notion_bul.py"""
import requests, config

r = requests.post("https://api.notion.com/v1/search",
                  headers={"Authorization": f"Bearer {config.NOTION_TOKEN}",
                           "Notion-Version": "2022-06-28"},
                  json={"filter": {"property": "object", "value": "database"}})
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:300]); exit()

results = r.json().get("results", [])
if not results:
    print("❌ Entegrasyon hiçbir DB göremiyor → DB'ye Connections iznini yeniden ver.")
for db in results:
    title = "".join(t["plain_text"] for t in db.get("title", [])) or "(isimsiz)"
    print(f"  DB ID : {db['id']}")
    print(f"  Ad    : {title}")
    print(f"  URL   : {db.get('url','')}\n")