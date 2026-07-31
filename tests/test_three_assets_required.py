from pathlib import Path


def test_three_assets_are_configured():
    text = Path("asset_oos_validation.py").read_text(encoding="utf-8")
    for key, ticker in [("oil", "CL=F"), ("emerging", "EEM"), ("investmentGrade", "LQD")]:
        assert f'"{key}": {{"ticker": "{ticker}"' in text
        assert f'"{key}": [' in text
