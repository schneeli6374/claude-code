# Aktien-Dashboard

Live-Aktienkurse mehrerer Firmen mit ~40 technischen Indikatoren und automatischen
Kauf-/Verkaufstipps. Aktualisiert sich alle 10 Sekunden, solange die Seite geöffnet und sichtbar ist.

## Start

```bash
python aktien_dashboard.py          # echte Kurse (Yahoo Finance)
python aktien_dashboard.py --demo   # simulierte Kurse, ohne Internet
```

Benötigt nur Python 3.8+ (keine Zusatzpakete). Der Browser öffnet sich unter http://localhost:8765.

## Funktionen

- Beliebige Symbole hinzufügen/entfernen (z. B. `AAPL`, `SAP.DE`, `BAS.DE`, `7203.T`), Liste wird gespeichert
- Karte anklicken → Kerzenchart mit SMA 20/50/200 + Bollinger, Volumen, RSI/MACD/Stochastik
- Indikatoren: SMA/EMA 10–200, Hull MA, VWMA, Golden/Death Cross, RSI, Stochastik, Stoch RSI, MACD,
  Williams %R, CCI, Momentum, ROC, Awesome Oscillator, Ultimate Oscillator, TRIX, Elder Power,
  ADX/DMI, Parabolic SAR, Supertrend, Ichimoku, Aroon, Donchian, Bollinger, Keltner, ATR,
  OBV, MFI, Chaikin Money Flow, VWAP, Volumen-Ratio, Pivot-Punkte
- Gesamtbewertung (Starker Kauf … Starker Verkauf) je Gruppe und insgesamt,
  Tipp mit Begründung, Stop-Loss und Kursziel (ATR-basiert)

⚠️ Keine Anlageberatung – die Signale sind rein technisch berechnet.
