"""GitHub Actions giriş noktası — bulutta günde 6 tur.
Durumsuz akış: MCAP-50 tarama → canlı skor → Notion'a yazım.
Paper trade/backtest ev PC'sinde (paper.json bulutta yaşayamaz).
Kopya koruması Notion'da (gün bazlı) — çift koşu defteri kirletmez."""
import notion
import scanner

def main():
    print("== SMC Bulut Taraması ==")
    res = scanner.scan()          # v8: MCAP ilk 50
    print(f"\nTarama bitti: {len(res)} sinyal")
    if not res:
        print("Bu turda barajı geçen sinyal yok — normal.")
        return
    yazilan = notion.write_signals(res, top=15)
    print("\n--- İlk 10 sinyal özeti ---")
    for r in res[:10]:
        print(f"  {r['symbol']:14s} {r['direction']:5s} {r['strategy']:28s} "
              f"skor {r['score']:3d}  RR 1:{r['rr']}")

if __name__ == "__main__":
    main()
