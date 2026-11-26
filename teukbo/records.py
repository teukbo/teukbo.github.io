"""지역별 기록: 특보가 발효된 날, 그날의 관측값, 시간대별 실황을 지역마다 묶는다(배포 전용 — 저장소에는 커밋하지 않는다).

    _site/data/records/<지역 id>.json          teukbo.site 가 사이트를 조립할 때 만든다(달력이 읽는다)
    _site/data/hours/<지역 id>/<연-월>.json    같은 때 만든다(달력에서 날짜를 누르면 그 달 것을 읽는다)

재료(모두 저장소에 쌓여 있다)
- data/rail/events/연-월.json  특보 발표·해제 이력(teukbo/rail_run.py 가 받아 둔다) → 지역의 특보구역이 발효 중이던 구간
- data/obs/연-월.json          관측 일자료(teukbo/obs.py) → 가까운 관측소의 그날 값
- data/hourly/날짜/시.json     시간별 실황(teukbo/hourly.py) → 지역 격자의 정시 값

기록 문서(records/<지역 id>.json)
    {"schema": 1, "id": "beolgyo", "name": "보성(벌교)",
     "station": {"stn": "258", "name": "보성군", "km": 11.3},   // 관측소가 max_station_km 안에 없으면 null
     "from": "2026-09-15", "to": "2026-11-25",                  // 특보 이력이 있는 기간(받아 둔 이력의 첫날~마지막 날). 이력이 없으면 null
     "obs_to": "2026-11-24",                                     // 관측값이 있는 마지막 날. 없으면 null
     "hours": {"from": "2026-11-25", "to": "2026-11-26"},        // 시간별 값이 있는 첫날~마지막 날. 없으면 null
     "vars": ["temp_min", "temp_max", "rain", "snow_new", "wind_max", "gust_max"],
     "days": {"2026-11-26": {"o": [vars 순서의 값…],             // 그날 관측(관측소 일자료)이 없으면 키가 없다
                              "w": [["strong_wind", "advisory", "2026-11-26T04:00+09:00", "2026-11-26T15:00+09:00"]],   // 특보가 없으면 키가 없다
                              "h": 24,                             // 시간별 값이 있는 시각 수. 없으면 키가 없다
                              "f": [-2.1, 6, 9.8, 14]}}}           // 체감온도 [최저, 그 시, 최고, 그 시] — 시간별 값이 feel_min_hours 시각 이상일 때만

- w: 그날(한국 시각 0~24시)에 한순간이라도 발효 중이던 특보 [종류 id, 단계 id, 발효 시각, 해제 시각]. 아직 해제되지 않았으면 해제 시각이 null.
  thresholds.json 의 types 에 없는 종류(건조·풍랑 등)는 싣지 않는다. 이력을 받기 시작하기 전에 발표된 특보는 알 수 없다.
- o: 값은 소수 1자리, 없으면 null. 강수량(rain)·신적설(snow_new)은 그날 관측이 있는데 값이 비어 있으면 0(기록되지 않음 = 없음).
- f: 그날의 체감온도 최저·최고는 시간별 실황으로만 구한다. 하루 단위 관측값(최저기온·평균풍속)으로 어림하던 값은 싣지 않는다 —
  가장 추운 새벽에는 바람이 약한데 하루 평균 풍속을 넣으면 실제보다 낮게 나온다(보성군 2026-11-24: 어림값 -4.1℃, 정시별 실제 최저 -1.5℃).
- 날은 특보 이력이 있는 기간 안에서 최근 keep_days 일만 싣는다(이력이 없으면 관측값·시간별 값만).

시간별 한 달 문서(hours/<지역 id>/<연-월>.json)
    {"schema": 1, "id": "beolgyo", "name": "보성(벌교)", "month": "2026-11", "grid": [69, 71],
     "source": "…", "vars": ["temp", "feel", "rain", "wind", "rh", "pty"],
     "feel_stat": "min",                                          // 이 달에 눈여겨볼 쪽: 겨울철 식을 쓰는 달은 min, 여름철 식을 쓰는 달은 max
     "days": {"2026-11-26": {"h": [0, 1, 2, …], "v": [[0.8, -2.1, 0.0, 2.8, 81, 0], …]}}}   // h: 값이 있는 시(오름차순), v[i]: h[i] 시의 값(vars 순서)
- 체감온도(feel)는 조립할 때 시각마다 기상청 식으로 계산한 값이다(가공값): 그 달이 feel.summer_months 면 여름철 식(기온·습도),
  아니면 겨울철 식(기온·풍속). 재료가 없으면 null. 초단기실황의 풍속이 몇 분 평균인지는 기상청 문서에 없어, 기상청이 표출하는 체감온도
  (10분 평균 풍속 기준)와 조금 다를 수 있다.
- 값은 지역 격자에 짝지어진 대표 관측지점의 것이라, 위 관측소 일자료(o)와는 지점이 다르다.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import hourly as hourly_store
from . import obs as obs_store
from . import rail, rail_run, storage
from .config import Config, Region
from .indicators import kma_summer_apparent_temp, kma_winter_apparent_temp
from .timeutil import KST, iso

log = logging.getLogger("teukbo.records")

SCHEMA = 1
VARS = ("temp_min", "temp_max", "rain", "snow_new", "wind_max", "gust_max")
HOUR_VARS = ("temp", "feel", "rain", "wind", "rh", "pty")
HOURS_SOURCE = "기상청 초단기실황(이 지역 격자의 대표 관측지점 정시 값). 체감온도는 기상청 식으로 계산한 값"
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


def day_values(row: Mapping[str, Any]) -> list[Any] | None:
    """관측 일자료 한 날 → vars 순서의 값. 그날 쓸 값이 하나도 없으면 None."""
    if all(row.get(name) is None for name in VARS):
        return None
    values = {name: row.get(name) for name in VARS}
    for name in ZERO_WHEN_BLANK:
        if values[name] is None:
            values[name] = 0.0
    return [_r1(values[name]) for name in VARS]


# ---------------------------------------------------------------- 시간별 실황 → 지역별 한 달 파일


def hour_feel(temp: Any, rh: Any, wind: Any, summer: bool) -> float | None:
    """한 시각의 체감온도(기상청 식). 여름철 식은 기온·습도, 겨울철 식은 기온·풍속이 있어야 한다."""
    if temp is None:
        return None
    if summer:
        return None if rh is None else round(kma_summer_apparent_temp(float(temp), float(rh)), 1)
    return None if wind is None else round(kma_winter_apparent_temp(float(temp), float(wind)), 1)


def day_hours(by_hour: Mapping[int, Mapping[str, Sequence[Any]]], key: str, summer: bool) -> tuple[list[int], list[list[Any]]]:
    """한 격자의 하루치 → (값이 있는 시들, 시마다 HOUR_VARS 순서의 값)."""
    at = {name: i for i, name in enumerate(hourly_store.VARS)}
    hours: list[int] = []
    values: list[list[Any]] = []
    for hour in sorted(by_hour):
        row = by_hour[hour].get(key)
        if not row or len(row) != len(hourly_store.VARS) or all(v is None for v in row):
            continue
        temp, rh, wind = row[at["temp"]], row[at["rh"]], row[at["wind"]]
        hours.append(hour)
        values.append([temp, hour_feel(temp, rh, wind, summer), row[at["rain"]], wind, rh, row[at["pty"]]])
    return hours, values


def feel_extremes(hours: Sequence[int], values: Sequence[Sequence[Any]], min_hours: int) -> list[Any] | None:
    """그날의 체감온도 [최저, 그 시, 최고, 그 시]. 체감온도가 있는 시각이 min_hours 에 못 미치면 None."""
    at = HOUR_VARS.index("feel")
    have = [(row[at], hour) for hour, row in zip(hours, values) if row[at] is not None]
    if len(have) < max(1, int(min_hours)):
        return None
    low, high = min(have), max(have, key=lambda item: (item[0], -item[1]))
    return [low[0], low[1], high[0], high[1]]


def build_hours(cfg: Config, data_dir: Path, out_dir: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """시간별 실황을 지역별 한 달 문서로 묶어 out_dir/<지역 id>/<연-월>.json 에 쓰고(out_dir 가 없으면 쓰지 않고),
    기록에 실을 요약을 돌려준다: 지역 id → 날짜 → {"h": 시각 수, "f": [체감 최저, 시, 최고, 시]}.

    한 달씩 처리한다 — 400일치를 한꺼번에 메모리에 올리지 않는다.
    """
    opts = cfg.settings.records
    days = hourly_store.stored_days(data_dir)
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    if not days:
        return summary
    oldest = (date.fromisoformat(days[-1]) - timedelta(days=int(opts["keep_days"]) - 1)).isoformat()
    days = [day for day in days if day >= oldest]
    regions = [(region, hourly_store.grid_key((region.nx, region.ny))) for region in cfg.regions]
    files = 0
    for month in sorted({day[:7] for day in days}):
        summer = int(month[5:7]) in cfg.settings.feel_summer_months
        docs: dict[str, dict[str, Any]] = {}
        for day in (d for d in days if d.startswith(month)):
            by_hour = hourly_store.load_day(data_dir, day)
            if not by_hour:
                continue
            for region, key in regions:
                hours, values = day_hours(by_hour, key, summer)
                if not hours:
                    continue
                docs.setdefault(region.id, {})[day] = {"h": hours, "v": values}
                entry: dict[str, Any] = {"h": len(hours)}
                extremes = feel_extremes(hours, values, int(opts["feel_min_hours"]))
                if extremes is not None:
                    entry["f"] = extremes
                summary.setdefault(region.id, {})[day] = entry
        if out_dir is None:
            continue
        for region, _ in regions:
            if region.id not in docs:
                continue
            storage.write_json(out_dir / region.id / f"{month}.json", {
                "schema": SCHEMA, "id": region.id, "name": region.name, "month": month, "grid": [region.nx, region.ny],
                "source": HOURS_SOURCE, "vars": list(HOUR_VARS), "feel_stat": "max" if summer else "min", "days": docs[region.id]})
            files += 1
    log.info("시간별 한 달 파일: %d개 (%s ~ %s, 시간별 값이 있는 지역 %d곳)", files, days[0], days[-1], len(summary))
    return summary


def build_region(region: Region, cfg: Config, spans: Mapping[str, Sequence[Span]], span: tuple[date | None, date | None],
                 stations: Sequence[Mapping[str, Any]], observed: Mapping[str, Mapping[str, Mapping[str, Any]]],
                 hours: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """한 지역의 기록 문서. hours: 이 지역의 시간별 요약(날짜 → {"h", "f"})."""
    opts = cfg.settings.records
    first, last = span
    station = pick_station(region, stations, float(opts["max_station_km"]))
    rows = observed.get(station["stn"], {}) if station else {}
    obs_days = sorted(rows)
    obs_to = date.fromisoformat(obs_days[-1]) if obs_days else None

    hours = hours or {}
    hours_to = date.fromisoformat(max(hours)) if hours else None
    end = max([d for d in (last, obs_to, hours_to) if d is not None], default=None)
    days: dict[str, dict[str, Any]] = {}
    if end is not None:
        start = end - timedelta(days=int(opts["keep_days"]) - 1)
        if first is not None:
            start = max(start, first)  # 특보 이력이 없는 날은 '특보 없음'과 구별되지 않으므로 싣지 않는다
        for day_text in obs_days:
            day = date.fromisoformat(day_text)
            if start <= day <= end:
                values = day_values(rows[day_text])
                if values is not None:
                    days.setdefault(day_text, {})["o"] = values
        for day_text, entry in hours.items():
            if start <= date.fromisoformat(day_text) <= end:
                days.setdefault(day_text, {}).update(entry)
        if first is not None and last is not None:
            mine = [s for code in region.zone_codes for s in spans.get(code, [])]
            for day_text, items in warnings_by_day(mine, start, last).items():
                days.setdefault(day_text, {})["w"] = items
    have_obs = [d for d, entry in days.items() if "o" in entry]
    have_hours = [d for d, entry in days.items() if "h" in entry]
    return {
        "schema": SCHEMA,
        "id": region.id,
        "name": region.name,
        "station": station,
        "from": first.isoformat() if first else None,
        "to": last.isoformat() if last else None,
        "obs_to": max(have_obs) if have_obs else None,
        "hours": {"from": min(have_hours), "to": max(have_hours)} if have_hours else None,
        "vars": list(VARS),
        "days": dict(sorted(days.items())),
    }


def build_all(cfg: Config, data_dir: Path, stations: Sequence[Mapping[str, Any]] | None = None,
              hours: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """모든 지역의 기록 문서. hours: build_hours 가 돌려준 시간별 요약(없으면 여기서 구한다 — 파일은 쓰지 않는다)."""
    stations = obs_store.load_stations() if stations is None else stations
    observed = obs_store.load_obs(data_dir)
    spans = spans_by_code(rail.build_intervals(rail_run.load_events(data_dir)), cfg)
    span = events_span(data_dir)
    if hours is None:
        hours = build_hours(cfg, data_dir)
    return [build_region(region, cfg, spans, span, stations, observed, hours.get(region.id)) for region in cfg.regions]


def write_site_records(cfg: Config, data_dir: Path, out_dir: Path, stations: Sequence[Mapping[str, Any]] | None = None,
                       hours_dir: Path | None = None) -> int:
    """지역마다 out_dir/<지역 id>.json 을 쓴다. hours_dir 를 주면 시간별 한 달 파일도 그 아래에 쓴다. 반환: 쓴 기록 파일 수.

    시간별 파일을 만들다 실패해도 기록(특보·하루 관측값)은 만든다 — 시간별 요약만 빠진다.
    """
    try:
        hours = build_hours(cfg, data_dir, hours_dir)
    except Exception:  # noqa: BLE001 — 시간별 값 때문에 달력까지 비면 안 된다
        log.exception("시간별 한 달 파일을 만들지 못했습니다 — 기록은 시간별 요약 없이 만듭니다.")
        hours = {}
    docs = build_all(cfg, data_dir, stations, hours)
    for doc in docs:
        storage.write_json(out_dir / f"{doc['id']}.json", doc)
    log.info("지역별 기록: %d곳 (특보가 있었던 날이 있는 지역 %d곳, 관측값이 있는 지역 %d곳, 시간별 값이 있는 지역 %d곳)", len(docs),
             sum(1 for d in docs if any("w" in e for e in d["days"].values())), sum(1 for d in docs if d["obs_to"]), sum(1 for d in docs if d["hours"]))
    return len(docs)
