"""Notion bağlantı testi — çalıştır: python test_notion.py"""
import requests
import config

r = requests.get("https://api.notion.com/v1/users/me",
                 headers={"Authorization": f"Bearer {config.NOTION_TOKEN}",
                          "Notion-Version": "2022-06-28"})
print("HTTP", r.status_code)
if r.status_code == 200:
    print("✅ Token geçerli — Notion bağlantısı OK")
else:
    print("❌ Sorun:", r.text[:200])