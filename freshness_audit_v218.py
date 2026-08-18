from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("FRESHNESS_DATA_DIR", str(ROOT / "public" / "data")))


def read(name: str) -> dict[str, Any]:
    try:
        x = json.loads((DATA / name).read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def parse_dt(v: Any) -> datetime | None:
    t = str(v or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        pass
    # 월간/일간 통계의 observation key도 처리
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y%m"):
        try:
            return datetime.strptime(t[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def age_hours(v: Any) -> float | None:
    d = parse_dt(v)
    return None if d is None else max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 3600.0)


def latest_obs(obj: Any) -> str | None:
    """Find the newest already-present observation timestamp without any network access."""
    found: list[datetime] = []
    raw: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for key in ("market_time_utc", "observed_at", "observation_at", "date", "latest_date", "TIME_PERIOD"):
                if x.get(key):
                    d = parse_dt(x.get(key))
                    if d is not None:
                        found.append(d); raw.append(str(x.get(key)))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            # last rows usually contain latest observation; scan tail to cap CPU/JSON size.
            for v in x[-8:]:
                walk(v)
    walk(obj)
    if not found:
        return None
    idx = max(range(len(found)), key=lambda i: found[i])
    return raw[idx]


def state_for_file(name: str, obj: dict[str, Any], obs: Any) -> tuple[str, str, float]:
    age = age_hours(obs)
    low = name.lower()
    if "card12" in low or "futures" in low or "equity" in low or "asset" in low:
        cadence, max_age = "market/trading-day", 96.0
    elif "ism" in low or "card9" in low or "card10" in low or "macro" in low:
        cadence, max_age = "monthly/mixed-release", 24.0 * 45.0
    else:
        cadence, max_age = "mixed", 24.0 * 45.0
    if not obj:
        return "UNAVAILABLE", cadence, max_age
    state = "LIVE" if age is not None and age <= max_age else "CACHE"
    return state, cadence, max_age


def main() -> None:
    status = read("source_status.json")
    card12 = read("card12.json")
    bundle = read("cards_8_12_bundle.json")
    items: list[dict[str, Any]] = []

    # A. ISM은 발표주기 최적화가 이미 구현되어 있으므로 skip-fresh를 CACHE로 명확히 표시.
    ism_latest = status.get("latest_date")
    if status.get("last_good_reused"):
        ism_state = "LKG"
    elif status.get("network_skipped_fresh"):
        ism_state = "CACHE"
    elif status.get("ok"):
        ism_state = "LIVE"
    else:
        ism_state = "UNAVAILABLE"
    items.append({
        "source": "ism_manufacturing",
        "status": ism_state,
        "cadence": "monthly-release",
        "observation_at": ism_latest,
        "network_skipped_fresh": bool(status.get("network_skipped_fresh")),
        "last_good_reused": bool(status.get("last_good_reused")),
    })

    # B. Card12 시장 snapshot. 기존 Yahoo 요청 meta만 재사용한다.
    snaps = card12.get("market_snapshots") if isinstance(card12.get("market_snapshots"), dict) else {}
    for sym, s in snaps.items():
        s = s if isinstance(s, dict) else {}
        obs = s.get("market_time_utc")
        age = age_hours(obs)
        mstate = str(s.get("market_state") or "").upper()
        if s.get("price") is None:
            state = "UNAVAILABLE"
        elif mstate in {"CLOSED", "CLOSE", "POST", "PRE"}:
            state = "CACHE"
        else:
            state = "LIVE" if age is not None and age <= 6.0 else "CACHE"
        items.append({
            "source": f"card12:{sym}",
            "status": state,
            "cadence": "intraday-market",
            "observation_at": obs,
            "age_hours": round(age, 2) if age is not None else None,
            "market_state": mstate or None,
            "value": s.get("price"),
            "provider": s.get("source"),
        })

    # C. public/data의 모든 JSON 산출물을 감사. 파일을 읽을 뿐 API 요청은 0회.
    file_rows: list[dict[str, Any]] = []
    for path in sorted(DATA.glob("*.json")):
        if path.name == "freshness_status.json":
            continue
        obj = read(path.name)
        obs = latest_obs(obj) or obj.get("generated_at_utc") or obj.get("generated_at")
        state, cadence, max_age = state_for_file(path.name, obj, obs)
        age = age_hours(obs)
        row = {
            "source": f"file:{path.name}",
            "status": state,
            "cadence": cadence,
            "observation_at": obs,
            "age_hours": round(age, 2) if age is not None else None,
            "max_age_hours": max_age,
            "generated_at": obj.get("generated_at_utc") or obj.get("generated_at"),
        }
        file_rows.append(row)
    items.extend(file_rows)

    payload = {
        "schema_version": "1.1.0",
        "patch_version": "V228-zero-call-freshness-unification",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "bundle_generated_at_utc": bundle.get("generated_at_utc"),
        "compatibility": {
            "existing_keys_removed": False,
            "existing_field_semantics_changed": False,
            "new_network_calls_added_by_patch": 0,
            "collector_network_policy_changed": False,
            "model_formulas_changed": False,
            "output_values_changed": False,
        },
        "items": items,
        "summary": {k: sum(x.get("status") == k for x in items) for k in ("LIVE", "CACHE", "LKG", "FALLBACK", "UNAVAILABLE")},
        "note": "월간/주간 자료는 발표주기 기준, 시장자료는 시장 timestamp 기준으로 판정합니다. 모든 판정은 이미 수집된 JSON만 읽으며 외부 호출을 추가하지 않습니다.",
    }
    (DATA / "freshness_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
