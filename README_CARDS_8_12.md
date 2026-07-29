# Global Macro Cards 8–12

## Fixed order
8. 미국채 금리·실질금리·수익률곡선
9. 글로벌 고용·소비 경기
10. 원자재·에너지·공급망 압력
11. 글로벌 경기국면 최종판정·투자환경
12. 선물시장 종합신호

## Validation policy
- Expanding walk-forward OOS
- Persistence benchmark
- RMSE skill, active direction accuracy, 80% interval coverage
- Approximate Diebold–Mariano significance diagnostic
- Automatic abstention/fallback when a model fails
- A horizon is labelled `준기관급` only when its objective gate passes

## Outputs
- `public/data/us_treasury_card8.json`
- `public/data/card9.json`
- `public/data/card10.json`
- `public/data/card11.json`
- `public/data/card12.json`
- `public/data/cards_8_12_bundle.json`

## Important
The US policy-rate path is read from the existing US engine and is not recalculated here.
Treasury futures are used as delayed/free direction and volatility cross-checks; they cannot promote a failed OOS horizon.
