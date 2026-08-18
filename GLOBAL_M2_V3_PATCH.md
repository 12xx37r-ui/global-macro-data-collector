# Global M2 V3 additive patch

- Adds `global_m2.py`.
- Full daily build now emits `public/data/global_m2.json` and embeds the same object in `cards_8_12_bundle.json` as `global_m2` and `macro.global_m2`.
- Sources attempted: FRED US M2, ECB euro-area M2, PBC China M2, BOJ Japan M2.
- Per-region `LIVE` / `LAST-GOOD` / `UNAVAILABLE`, dates, source URLs, errors and coverage are explicit.
- A missing region is reweighted. Fewer than 2 regions or <50% strategic coverage causes abstention (`available:false`) instead of inventing a global value or failing the entire workflow.
- Output contains `current`, `forecast`, `changePct`, `directionScore`, plus legacy `value` compatibility.
- Existing Cards 8–12 and fast-market refresh remain intact. Fast refresh preserves the embedded Global M2 block because it mutates only selected bundle fields/cards.
- Added unit tests for composite direction, partial-source reweighting, last-good visibility, and insufficient-coverage abstention.
