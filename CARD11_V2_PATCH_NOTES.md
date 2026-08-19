# Card11 V2 same-scale continuous state patch

## Core correction
- Replaces the old comparison of current market-signal averages vs future change-direction averages.
- Current and 3-month future scores now use the same continuous state functions, identical base weights, and identical denominator.
- Forecasts that fail their own 3-month gate are conservatively held at the current state instead of being dropped or used directionally.

## Horizon-consistent validation
- Card11 future validation is strictly 3-month matched.
- Card8: at least 2 of DGS2/DGS10/DFII10 3m target gates.
- Card9/Card10: their own 3m quality gate.
- Card12: at least 2 of equity/rates/commodities 3m group gates.
- Future composite promotion requires >=3 validated input axes AND >=70% of base weight.
- Registry-level published-snapshot OOS remains an additional separate promotion gate.

## Continuous state definitions
- Card9: native composite scale `(index-50)/10`.
- Card10: pressure semantics inverse `(50-index)/10`.
- Card12: native predictive group-index scale `(index-50)/10`, same transformation for current and forecast.
- Card8: transparent rate-burden state using Card8's published real-rate/10Y regime anchors plus 10Y-2Y curve.

## Verification on repository's bundled latest JSON
- old displayed design could compare `13.3` to `-20.0` despite incompatible definitions.
- V2 local re-evaluation: current `-17.2`, future `-22.6` on the bundled snapshot.
- Only Card10 currently passes the strict matched 3m input gate in that bundled snapshot, so `future_quality_gate.passed=false` and the other failed axes are held at current state.
- Card9 3m: skill ~5.32%, direction accuracy ~69.23%, but DM p ~0.1097, so the strict 5% significance gate correctly remains failed. This is not overridden.

## Tests
`PYTHONPATH=. pytest -q` -> `72 passed`
