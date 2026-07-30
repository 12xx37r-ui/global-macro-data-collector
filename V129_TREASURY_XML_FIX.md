# V129 Treasury XML OOS Fix

- OOS validator no longer calls FRED.
- Card 8 transmission features use official U.S. Treasury annual XML feeds.
- Nominal 2Y and 10Y rates are read from Daily Treasury Par Yield Curve data.
- Real 10Y rates are read from Daily Treasury Par Real Yield Curve data.
- 10Y-2Y curve is derived inside the validator.
- Annual requests are parallelized and each failing year is isolated.
- Existing last-known-good and conservative fallback behavior remains.
