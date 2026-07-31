from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_high_yield_asset_is_required():
    text=(ROOT / "asset_oos_validation.py").read_text(encoding="utf-8")
    assert '"highYield": {"ticker": "HYG"' in text
    assert '"highYield": ["ES=F", "ZN=F"]' in text
