# V135 equity output guarantee

`collector.py` now invokes `equity_fundamentals.main()` directly. Therefore `public/data/equity_fundamentals.json` is generated even if an older GitHub workflow only executes `python collector.py`. The workflow also verifies the file exists and is valid JSON before tests and commit.
