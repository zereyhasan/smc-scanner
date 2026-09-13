"""Notion DB son kayıtları — çalıştır: python notion_kontrol.py"""
import requests, config
from datetime import datetime

r = requests.post(
    f"https://api.notion.com/v1/databases/{config.NOTION_DB_ID}/query",
    headers={"Authorization": f"Bearer {config.NOTION_TOKEN}",
             "Notion-Version": "2022-06-28"},
    json={"sorts": [{"timestamp": "created_time", "direction": "descending"}],
          "page_size": 15})
if r.status_code != 200:
    print("HTTP", r.status_code, r.text[:200]); exit()

def t(p, name):
    x = p.get(name, {}).get("rich_text", [])
    return x[0].get("plain_text", "") if x else "—"

rows = r.json().get("results", [])
print(f"DB'de bu sorguda {len(rows)} satır (en yeni önce):\n")
for page in rows:
    p = page["properties"]
    title = p.get("Ad", {}).get("title", [])
    title = title[0].get("plain_text", "?") if title else "?"
    created = page["created_time"][:16].replace("T", " ")
    print(f"  oluşturuldu: {created} UTC | Tarih: {p.get('Tarih',{}).get('date') or 'BOŞ'} | "
          f"{t(p,'Parite'):14s} {t(p,'Strateji'):28s} {t(p,'Yön')}")