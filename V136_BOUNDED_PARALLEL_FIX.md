# V136 bounded parallel collection

- Equity fallback calls run in parallel with 4s connect / 8s read limits.
- ISM candidate pages run in parallel with the same bounded timeout.
- Workflow hard limit reduced to 12 minutes; normal target is under 2 minutes.
- Last-good metric retention remains active when all public endpoints are unavailable.
