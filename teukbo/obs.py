"""관측 일자료 저장: 기상청 종관기상관측(ASOS) 지점의 하루 단위 관측값을 날마다 이어 받아 쌓는다.

    python3 -m teukbo.obs            (GitHub Actions 가 수집 뒤에 실행)

- data/obs/연-월.json   지점별 날짜별 관측값 (커밋, 지우지 않음) — 지역별 기록(teukbo/records.py)의 '그날의 날씨'가 된다

지점 목록은 config/asos_stations.json. 지점마다 '저장된 마지막 날의 다음 날 ~ 어제'를 한 번의 호출로 받는다
(처음에는 settings.json 의 records.since 부터). 전날 자료는 오전 11시(records.obs_ready_hour) 뒤에야 조회되므로
그 전에는 어제 것을 요청하지 않는다. 채울 날이 없으면 한 번도 호출하지 않는다.

공공데이터포털 「기상청_지상(종관, ASOS) 일자료 조회서비스」의 활용신청이 되어 있어야 한다(시간자료와는 별개 서비스).
인증키가 없거나, 신청이 안 되어 있거나, 일부 지점을 받지 못해도 정상 종료한다 — 기상 수집·배포를 막지 않으려는 것이다.
받은 만큼은 저장하고, 못 받은 지점은 다음 실행이 이어 받는다.

저장 형식
    {"schema": 1, "month": "2026-11", "vars": ["rain", "rain_1h_max", …],        // 값의 순서(asos.DAILY_FIELDS)
     "stations": {"258": {"name": "보성군", "days": {"2026-11-24": [0.0, null, …]}}}}
    값이 없으면 null. 강수량·신적설의 null 은 '그날 기록되지 않음(없음)'이다(관측 자체가 빠진 날과는 다른 값들로 가늠한다).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import storage
from .config import ROOT, Config, load_config
from .http import FetchError, normalize_service_key
from .sources import asos
from .timeutil import now_kst, to_kst

log = logging.getLogger("teukbo.obs")

SCHEMA = 1
STATIONS_FILE = ROOT / "config" / "asos_stations.json"
VARS: tuple[str, ...] = tuple(asos.DAILY_FIELDS.values())
MAX_ROWS_PER_CALL = asos.PAGE_SIZE  # 한 번의 호출로 받는 날 수의 한도
STOP_AFTER_FAILURES = 3  # 연달아 이만큼 실패하면(활용신청 누락·하루 한도 초과 등) 이번 실행은 그만둔다

Obs = dict[str, dict[str, dict[str, Any]]]  # 지점번호 → 날짜(YYYY-MM-DD) → {변수: 값}


def obs_dir(data_dir: Path) -> Path:
    return data_dir / "obs"


def month_path(data_dir: Path, day: date) -> Path:
    return obs_dir(data_dir) / f"{day:%Y-%m}.json"


def load_stations(path: Path = STATIONS_FILE) -> list[dict[str, Any]]:
    """관측 지점 목록: [{stn, name, region, lon, lat, …}]. 파일이 없으면 빈 목록."""
    doc = storage.read_json(path) or {}
    return [s for s in doc.get("stations", []) if s.get("stn")]


def load_obs(data_dir: Path) -> Obs:
    """쌓아 둔 관측 일자료 전부. 달마다 변수 순서(vars)가 달라도 이름으로 맞춰 읽는다."""
    out: Obs = {}
    folder = obs_dir(data_dir)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        doc = storage.read_json(path) or {}
        names = [str(v) for v in doc.get("vars", [])]
        for stn, info in (doc.get("stations") or {}).items():
            days = out.setdefault(str(stn), {})
            for day, values in (info.get("days") or {}).items():
                days[day] = dict(zip(names, values))
    return out


def last_day(days: Mapping[str, Any]) -> date | None:
    return date.fromisoformat(max(days)) if days else None


def target_end(now: datetime, ready_hour: int) -> date:
    """지금 요청할 수 있는 마지막 날: 어제. 전날 자료가 아직 올라오지 않는 시각이면 그제."""
    local = to_kst(now)
    return local.date() - timedelta(days=1 if local.hour >= ready_hour else 2)


def _has_values(row: Mapping[str, Any]) -> bool:
    return any(row.get(name) is not None for name in VARS)


def _pack(row: Mapping[str, Any]) -> list[Any]:
    return [None if row.get(name) is None else round(float(row[name]), 1) for name in VARS]


def save_rows(data_dir: Path, stn: str, name: str, rows: Sequence[Mapping[str, Any]]) -> int:
    """받은 행들을 달별 파일에 넣는다(이미 있는 날은 새 값으로 바꾼다). 반환: 저장한 날 수."""
    by_month: dict[Path, list[Mapping[str, Any]]] = {}
    for row in rows:
        if _has_values(row):
            by_month.setdefault(month_path(data_dir, row["d"]), []).append(row)
    saved = 0
    for path, month_rows in by_month.items():
        doc = storage.read_json(path) or {"schema": SCHEMA, "month": path.stem, "vars": list(VARS), "stations": {}}
        old_vars = [str(v) for v in doc.get("vars", [])]
        if old_vars != list(VARS):  # 변수가 늘었으면 이미 있는 날의 값을 새 순서로 옮긴다(없던 변수는 null)
            for info in doc.get("stations", {}).values():
                info["days"] = {day: [dict(zip(old_vars, values)).get(v) for v in VARS] for day, values in (info.get("days") or {}).items()}
            doc["vars"] = list(VARS)
        station = doc.setdefault("stations", {}).setdefault(stn, {"name": name, "days": {}})
        station["name"] = name
        for row in month_rows:
            station.setdefault("days", {})[row["d"].isoformat()] = _pack(row)
            saved += 1
        station["days"] = dict(sorted(station["days"].items()))
        doc["stations"] = dict(sorted(doc["stations"].items(), key=lambda kv: (len(kv[0]), kv[0])))
        storage.write_json(path, doc)
    return saved


def run(cfg: Config, now: datetime, data_dir: Path, env: Mapping[str, str], stations: Sequence[Mapping[str, Any]] | None = None) -> int:
    """관측 일자료를 이어 받는다. 늘 0 을 돌려준다(실패해도 수집·배포는 계속되어야 한다)."""
    key = normalize_service_key(env.get("DATA_GO_KR_SERVICE_KEY"))
    if not key:
        log.info("인증키가 없어 관측 일자료를 받지 않습니다.")
        return 0
    opts = cfg.settings.records
    since = date.fromisoformat(str(opts["since"]))
    end = target_end(now, int(opts["obs_ready_hour"]))
    budget = float(opts["obs_time_budget_seconds"])
    stations = load_stations() if stations is None else stations
    have = load_obs(data_dir)
    todo = []
    for station in stations:
        last = last_day(have.get(str(station["stn"]), {}))
        start = max(since, last + timedelta(days=1)) if last else since
        if start <= end:
            todo.append((station, start))
    if not todo:
        log.info("관측 일자료: 새로 받을 날이 없습니다(%s 까지 저장됨).", end)
        return 0

    started = time.monotonic()
    calls = saved = failed = streak = 0
    for station, start in todo:
        if time.monotonic() - started > budget:
            log.info("관측 일자료: 이번 실행에 쓸 시간(%d초)을 다 써서 나머지 지점은 다음 실행에 받습니다.", budget)
            break
        stn, name = str(station["stn"]), str(station.get("name", ""))
        stop = min(end, start + timedelta(days=MAX_ROWS_PER_CALL - 1))  # 남은 날은 다음 실행이 이어 받는다
        calls += 1
        try:
            rows = asos.fetch_daily(key, stn, start, stop, cfg.settings.http)
        except FetchError as e:
            failed += 1
            streak += 1
            log.warning("관측 일자료 %s %s (%s~%s) 받지 못함: %s", stn, name, start, stop, str(e)[:160])
            if streak >= STOP_AFTER_FAILURES:
                log.error("관측 일자료: 연달아 %d곳을 받지 못해 이번 실행은 그만둡니다. 공공데이터포털에서 "
                          "'기상청_지상(종관, ASOS) 일자료 조회서비스' 활용신청과 하루 호출 한도를 확인해 주세요.", streak)
                break
            continue
        streak = 0
        saved += save_rows(data_dir, stn, name, [r for r in rows if start <= r["d"] <= stop])
    log.info("관측 일자료: %d곳 조회(%d곳 실패), %d일치 저장 — %s 까지 요청", calls, failed, saved, end)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="관측 일자료(기상청 ASOS) 이어 받기")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.WARNING)  # 요청마다 찍으면 백 줄 가까이 된다
    try:
        return run(load_config(), now_kst().replace(second=0, microsecond=0), args.data_dir, os.environ)
    except Exception:  # noqa: BLE001 — 관측 자료 때문에 수집·배포가 멈추면 안 된다
        log.exception("관측 일자료를 받는 중 오류 — 이번 실행은 건너뜁니다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
