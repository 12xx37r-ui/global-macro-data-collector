# V2.4 objective validation

- Candidate models are selected prequentially: each forecast origin may use only losses known before that origin.
- Diebold-Mariano diagnostics use a horizon-aware Newey-West/Bartlett HAC variance and a 5% gate.
- Asset signals with non-positive benchmark skill always receive zero production weight.
- A/B grades require positive RMSE skill, hit-rate confidence bounds, correlation, and adequate OOS samples.
- C/D/insufficient signals are research or shadow results and receive zero production weight.
- Stale and fallback validation snapshots are forced to abstain in the Apps Script dashboard.

Passing these gates means statistical evidence under the declared free-data test. It is not a guarantee of future returns.
