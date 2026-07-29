# Card 8 V1.2

- FRED multi-series CSV batch removed; official single-series requests run concurrently (8 workers).
- CSV/API fallback retained; exact T10Y2Y reconstruction only when direct series is unavailable.
- Direction validation split into active direction accuracy, active coverage, and abstention rate.
- Added approximate Diebold-Mariano significance diagnostic against persistence.
- Added material skill thresholds by horizon; tiny positive skill no longer passes.
- Added long-trend and 6-month mean-reversion candidates for longer horizons.
- Persistence fallback can never receive a 준기관급 gate.
- GitHub Actions uses checkout@v5 and setup-python@v6.
