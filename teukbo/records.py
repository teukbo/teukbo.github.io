"""지역별 기록: 특보가 발효된 날과 그날의 관측값을 지역마다 한 파일로 묶는다(배포 전용 — 저장소에는 커밋하지 않는다).

    _site/data/records/<지역 id>.json     teukbo.site 가 사이트를 조립할 때 만든다

재료(둘 다 저장소에 쌓여 있다)
- data/rail/events/연-월.json  특보 발표·해제 이력(teukbo/rail_run.py 가 받아 둔다) → 지역의 특보구역이 발효 중이던 구간
- data/obs/연-월.json          관측 일자료(teukbo/obs.py) → 가까운 관측소의 그날 값

문서 꼴
    {"schema": 1, "id": "beolgyo", "name": "보성(벌교)",
     "station": {"stn": "258", "name": "보성군", "km": 11.3},   // 관측소가 max_station_km 안에 없으면 null
     "from": "2026-09-15", "to": "2026-11-25",                  // 특보 이력이 있는 기간(받아 둔 이력의 첫날~마지막 날). 이력이 없으면 null
     "obs_to": "2026-11-24",                                     // 관측값이 있는 마지막 날. 없으면 null
     "vars": ["temp_min", "temp_max", "feel_min", "feel_max", "rain", "snow_new", "wind_max", "gust_max"],
     "feel_basis": {"min": "최저기온·평균풍속", "max": "최고기온·최저습도"},
     "days": {"2026-11-18": {"o": [vars 순서의 값…],             // 그날 관측이 없으면 키가 없다
                              "w": [["strong_wind", "advisory", "2026-11-18T04:00+09:00", "2026-11-18T15:00+09:00"]]}}}   // 특보가 없으면 키가 없다

- w: 그날(한국 시각 0~24시)에 한순간이라도 발효 중이던 특보 [종류 id, 단계 id, 발효 시각, 해제 시각]. 아직 해제되지 않았으면 해제 시각이 null.
  thresholds.json 의 types 에 없는 종류(건조·풍랑 등)는 싣지 않는다. 이력을 받기 시작하기 전에 발표된 특보는 알 수 없다.
- o: 값은 소수 1자리, 없으면 null. 강수량(rain)·신적설(snow_new)은 그날 관측이 있는데 값이 비어 있으면 0(기록되지 않음 = 없음).
- 체감온도는 하루 단위 관측값으로 어림한 값이다: feel_min 은 10~4월에 겨울철 식(최저기온·평균풍속), feel_max 는 5~9월에 여름철 식(최고기온·최저습도).
  시간별 관측으로 구한 그날의 실제 최저·최고 체감온도와는 다를 수 있다(그래서 feel_basis 에 재료를 적어 둔다). 철이 아니거나 재료가 없으면 null.
- 날은 특보 이력이 있는 기간 안에서 최근 keep_days 일만 싣는다(이력이 없으면 관측값만).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import obs as obs_store
from . import rail, rail_run, storage
from .config import Config, Region
from .indicators import kma_summer_apparent_temp, kma_winter_apparent_temp
from .timeutil import KST, iso

log = logging.getLogger("teukbo.records")

SCHEMA = 1
VARS = ("temp_min", "temp_max", "feel_min", "feel_max", "rain", "snow_new", "wind_max", "gust_max")
FEEL_BASIS = {"min": "최저기온·평균풍속", "max": "최고기온·최저습도"}
ZERO_WHEN_BLANK = ("rain", "snow_new")  # 그날 관측이 있는데 비어 있으면 '없음'(0)
EARTH_RADIUS_KM = 6371.0

Span = tuple[str, str, datetime, "datetime | None"]  # (종류 id, 단계 id, 발효, 해제)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 지점 사이의 직선(대권) 거리."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def pick_station(region: Region, stations: Sequence[Mapping[str, Any]], max_km: float) -> dict[str, Any] | None:
    """그 지역의 관측소: 지역 안에 있는(region 이 이 지역인) 지점을 먼저, 없으면 가장 가까운 지점. max_km 보다 멀면 None."""
    def km(station: Mapping[str, Any]) -> float:
        return distance_km(region.lat, region.lon, float(station["lat"]), float(station["lon"]))

    usable = [s for s in stations if s.get("lat") is not None and s.get("lon") is not None]
    if not usable:
        return None
    inside = [s for s in usable if s.get("region") == region.id]
    best = min(inside or usable, key=km)
    dist = km(best)
    if dist > max_km:
        return None
    return {"stn": str(best["stn"]), "name": str(best.get("name", "")), "km": round(dist, 1)}


def events_span(data_dir: Path) -> tuple[date | None, date | None]:
    """받아 둔 특보 발표 이력의 첫날과 마지막 날."""
    days: list[str] = []
    folder = rail_run.rail_dir(data_dir) / "events"
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            days.extend((storage.read_json(path) or {}).get("days", {}).keys())
    parsed = []
    for ymd in days:
        try:
            parsed.append(datetime.strptime(ymd, "%Y%m%d").date())
        except ValueError:
            continue
    return (min(parsed), max(parsed)) if parsed else (None, None)


def spans_by_code(intervals: Sequence[rail.Interval], cfg: Config) -> dict[str, list[Span]]:
    """특보구역 코드 → 발효 구간들. 종류·단계 이름을 thresholds 의 id 로 바꾸고, 모르는 종류·단계는 뺀다."""
    th = cfg.thresholds
    type_ids = {str(info["name"]): tid for tid, info in th.types.items()}
    level_ids = {str(info["name"]): lid for lid, info in th.levels.items()}
    out: dict[str, list[Span]] = {}
    for iv in intervals:
        tid, lid = type_ids.get(iv.type_name), level_ids.get(iv.level_name)
        if tid is None or lid is None:
            continue
        if iv.end is not None and iv.end <= iv.start:
            continue  # 같은 시각에 단계가 바뀌며 생긴 길이 0 구간
        out.setdefault(iv.code, []).append((tid, lid, iv.start, iv.end))
    return out


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=KST)
    return start, start + timedelta(days=1)


def warnings_by_day(spans: Sequence[Span], first: date, last: date) -> dict[str, list[list[Any]]]:
    """날짜 → 그날 한순간이라도 발효 중이던 특보들. 해제되지 않은 특보는 last 까지 이어진 것으로 본다."""
    out: dict[str, list[list[Any]]] = {}
    for tid, lid, start, end in sorted(set(spans), key=lambda s: (s[2], s[0], s[1])):
        day = max(first, start.astimezone(KST).date())
        until = last if end is None else min(last, end.astimezone(KST).date())
        while day <= until:
            lo, hi = _day_bounds(day)
            if start < hi and (end is None or end > lo):
                out.setdefault(day.isoformat(), []).append([tid, lid, iso(start), iso(end) if end is not None else None])
            day += timedelta(days=1)
    return out


def _r1(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def day_values(row: Mapping[str, Any], day: date, summer_months: Sequence[int]) -> list[Any] | None:
    """관측 일자료 한 날 → vars 순서의 값. 그날 쓸 값이 하나도 없으면 None."""
    if all(row.get(name) is None for name in ("temp_min", "temp_max", "rain", "snow_new", "wind_max", "gust_max")):
        return None
    tmin, tmax = row.get("temp_min"), row.get("temp_max")
    feel_min = feel_max = None
    if day.month in summer_months:
        if tmax is not None and row.get("rh_min") is not None:
            feel_max = kma_summer_apparent_temp(float(tmax), float(row["rh_min"]))
    elif tmin is not None and row.get("wind_avg") is not None:
        feel_min = kma_winter_apparent_temp(float(tmin), float(row["wind_avg"]))
    values = {"temp_min": tmin, "temp_max": tmax, "feel_min": feel_min, "feel_max": feel_max,
              "rain": row.get("rain"), "snow_new": row.get("snow_new"), "wind_max": row.get("wind_max"), "gust_max": row.get("gust_max")}
    for name in ZERO_WHEN_BLANK:
        if values[name] is None:
            values[name] = 0.0
    return [_r1(values[name]) for name in VARS]


def build_region(region: Region, cfg: Config, spans: Mapping[str, Sequence[Span]], span: tuple[date | None, date | None],
                 stations: Sequence[Mapping[str, Any]], observed: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """한 지역의 기록 문서."""
    opts = cfg.settings.records
    first, last = span
    station = pick_station(region, stations, float(opts["max_station_km"]))
    rows = observed.get(station["stn"], {}) if station else {}
    obs_days = sorted(rows)
    obs_to = date.fromisoformat(obs_days[-1]) if obs_days else None

    end = max([d for d in (last, obs_to) if d is not None], default=None)
    days: dict[str, dict[str, Any]] = {}
    if end is not None:
        start = end - timedelta(days=int(opts["keep_days"]) - 1)
        if first is not None:
            start = max(start, first)  # 특보 이력이 없는 날은 '특보 없음'과 구별되지 않으므로 싣지 않는다
        for day_text in obs_days:
            day = date.fromisoformat(day_text)
            if start <= day <= end:
                values = day_values(rows[day_text], day, cfg.settings.feel_summer_months)
                if values is not None:
                    days.setdefault(day_text, {})["o"] = values
        if first is not None and last is not None:
            mine = [s for code in region.zone_codes for s in spans.get(code, [])]
            for day_text, items in warnings_by_day(mine, start, last).items():
                days.setdefault(day_text, {})["w"] = items
    have_obs = [d for d, entry in days.items() if "o" in entry]
    return {
        "schema": SCHEMA,
        "id": region.id,
        "name": region.name,
        "station": station,
        "from": first.isoformat() if first else None,
        "to": last.isoformat() if last else None,
        "obs_to": max(have_obs) if have_obs else None,
        "vars": list(VARS),
        "feel_basis": dict(FEEL_BASIS),
        "days": dict(sorted(days.items())),
    }


def build_all(cfg: Config, data_dir: Path, stations: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """모든 지역의 기록 문서."""
    stations = obs_store.load_stations() if stations is None else stations
    observed = obs_store.load_obs(data_dir)
    spans = spans_by_code(rail.build_intervals(rail_run.load_events(data_dir)), cfg)
    span = events_span(data_dir)
    return [build_region(region, cfg, spans, span, stations, observed) for region in cfg.regions]


def write_site_records(cfg: Config, data_dir: Path, out_dir: Path, stations: Sequence[Mapping[str, Any]] | None = None) -> int:
    """지역마다 out_dir/<지역 id>.json 을 쓴다. 반환: 쓴 파일 수."""
    docs = build_all(cfg, data_dir, stations)
    for doc in docs:
        storage.write_json(out_dir / f"{doc['id']}.json", doc)
    log.info("지역별 기록: %d곳 (특보가 있었던 날이 있는 지역 %d곳, 관측값이 있는 지역 %d곳)", len(docs),
             sum(1 for d in docs if any("w" in e for e in d["days"].values())), sum(1 for d in docs if d["obs_to"]))
    return len(docs)
