"""시간별 예측값 → 특보 판단 지표(이동 누적, 일 최고·최저 등) 계산.

지표의 종류와 창 길이·시간대는 config/thresholds.json 의 indicators 에서 읽는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from .config import DEFAULT_SUMMER_MONTHS, Indicator
from .timeutil import KST, floor_hour, to_kst

HOUR = timedelta(hours=1)


@dataclass
class ModelSeries:
    """한 지점·한 모델의 시간별 예측값.

    values 의 키(표준 변수명): precip(mm/1h), snow(cm/1h 신적설), temp(℃), rh(%), wind(m/s),
    gust(m/s), apparent(℃, 여름철 체감온도), feel(℃, 철에 맞는 체감온도 — add_feel_temp). 없는 변수는 키가 없거나 전부 None.
    """

    model_id: str
    times: list[datetime]
    values: dict[str, list[Optional[float]]]
    _maps: dict[str, dict[datetime, float]] = field(default_factory=dict, repr=False, compare=False)

    def lookup(self, variable: str) -> dict[datetime, float]:
        """시각 → 값 사전 (결측 제외)."""
        if variable not in self._maps:
            series = self.values.get(variable) or []
            self._maps[variable] = {t: float(v) for t, v in zip(self.times, series) if v is not None and not math.isnan(v)}
        return self._maps[variable]

    def has(self, variable: str) -> bool:
        return bool(self.lookup(variable))


@dataclass(frozen=True)
class Point:
    """지표의 한 값. slot 은 정렬·결합용 키(시간별: 시각, 일별: 날짜)."""

    slot: str
    time: datetime  # 대표 시각 (누적: 창의 끝, 일별: 극값이 나타난 시각)
    value: float
    start: datetime  # 값이 대표하는 구간의 시작
    end: datetime  # 구간의 끝


def kma_summer_apparent_temp(temp_c: float, rh_percent: float) -> float:
    """기상청 여름철 체감온도(℃).

    체감온도 = -0.2442 + 0.55399·Tw + 0.45535·Ta − 0.0022·Tw² + 0.00278·Tw·Ta + 3.0
    Tw(습구온도)는 Stull(2011) 추정식. 폭염특보 기준이 이 체감온도를 사용한다.
    """
    ta = temp_c
    rh = max(0.0, min(100.0, rh_percent))
    tw = (
        ta * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(ta + rh)
        - math.atan(rh - 1.67633)
        + 0.00391838 * rh**1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )
    return -0.2442 + 0.55399 * tw + 0.45535 * ta - 0.0022 * tw * tw + 0.00278 * tw * ta + 3.0


def add_apparent_temp(ms: ModelSeries) -> None:
    """기온·습도가 있으면 기상청식 체감온도 계열(apparent)을 채운다."""
    if "apparent" in ms.values:
        return
    temp, rh = ms.values.get("temp"), ms.values.get("rh")
    if not temp or not rh:
        return
    ms.values["apparent"] = [
        round(kma_summer_apparent_temp(t, h), 2) if t is not None and h is not None else None for t, h in zip(temp, rh)
    ]
    ms._maps.pop("apparent", None)


# 기상청 겨울철 체감온도 식을 쓰는 범위: 기온 10℃ 이하, 풍속 1.3m/s 이상. 그 밖에서는 체감온도 = 기온.
WINTER_FEEL_MAX_TEMP = 10.0
WINTER_FEEL_MIN_WIND = 1.3


def kma_winter_apparent_temp(temp_c: float, wind_ms: float) -> float:
    """기상청 겨울철 체감온도(℃).

    체감온도 = 13.12 + 0.6215·T − 11.37·V^0.16 + 0.3965·V^0.16·T  (T 기온 ℃, V 풍속 km/h = m/s × 3.6)
    기온이 10℃를 넘거나 풍속이 1.3m/s 에 못 미치면 식을 쓰지 않고 기온을 그대로 돌려준다.
    """
    if temp_c > WINTER_FEEL_MAX_TEMP or wind_ms < WINTER_FEEL_MIN_WIND:
        return temp_c
    v = (wind_ms * 3.6) ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * v * temp_c


def _number(value: Optional[float]) -> Optional[float]:
    return None if value is None or math.isnan(value) else float(value)


def feel_temp(month: int, temp_c: Optional[float], rh_percent: Optional[float], wind_ms: Optional[float],
              summer_months: Iterable[int] = DEFAULT_SUMMER_MONTHS) -> Optional[float]:
    """그 달에 맞는 체감온도(℃): summer_months 의 달이면 여름철 식(기온·습도), 아니면 겨울철 식(기온·풍속). 재료가 없으면 None."""
    ta = _number(temp_c)
    if ta is None:
        return None
    if month in summer_months:
        rh = _number(rh_percent)
        return None if rh is None else kma_summer_apparent_temp(ta, rh)
    wind = _number(wind_ms)
    return None if wind is None else kma_winter_apparent_temp(ta, wind)


def add_feel_temp(ms: ModelSeries, summer_months: Iterable[int] = DEFAULT_SUMMER_MONTHS) -> None:
    """철에 맞는 체감온도 계열(feel)을 채운다 — 시각마다 그 달(한국 시각)이 여름철이면 기온·습도, 아니면 기온·풍속으로.

    폭염특보 판단에 쓰는 apparent(늘 여름철 식)와 달리, 사람이 느끼는 온도를 한 해 내내 한 줄로 보여 주려는 값이다.
    재료(기온과, 여름에는 습도·겨울에는 풍속)가 없는 시각은 None.
    """
    if "feel" in ms.values:
        return
    temp = ms.values.get("temp")
    if not temp:
        return
    months = frozenset(int(m) for m in summer_months)
    rh, wind = ms.values.get("rh") or [], ms.values.get("wind") or []
    at = lambda row, i: row[i] if i < len(row) else None  # noqa: E731
    out: list[Optional[float]] = []
    for i, t in enumerate(ms.times):
        value = feel_temp(to_kst(t).month, at(temp, i), at(rh, i), at(wind, i), months)
        out.append(None if value is None else round(value, 2))
    if any(v is not None for v in out):
        ms.values["feel"] = out
        ms._maps.pop("feel", None)


def _slot_hour(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H")


def _hourly(ind: Indicator, ms: ModelSeries) -> list[Point]:
    data = ms.lookup(ind.variable or "")
    return [Point(_slot_hour(t), t, v, t, t) for t, v in sorted(data.items())]


def _rolling_sum(ind: Indicator, ms: ModelSeries) -> list[Point]:
    data = ms.lookup(ind.variable or "")
    window = int(ind.window_hours or 1)
    out: list[Point] = []
    for t in sorted(data):
        total = 0.0
        for k in range(window):
            v = data.get(t - k * HOUR)
            if v is None:  # 창 안에 결측이 있으면 그 시각의 누적값은 만들지 않는다
                break
            total += v
        else:
            out.append(Point(_slot_hour(t), t, total, t - window * HOUR, t))
    return out


def _period_extreme(ind: Indicator, ms: ModelSeries, now: datetime) -> list[Point]:
    """하루 중 정해진 시간대(start_hour 초과 ~ end_hour 이하)의 최고/최저값.

    end_hour 가 start_hour 이하이면 다음 날로 넘어가는 시간대(예: 18→9 = 18시 초과 ~ 다음 날 9시).
    시간대의 일부가 이미 지났고 그 부분 자료가 없으면 남은 시간만으로 계산한다.
    앞으로 남은 시각에 결측이 있으면 그날 값은 만들지 않는다.
    """
    data = ms.lookup(ind.variable or "")
    if not data:
        return []
    start_h, end_h = int(ind.start_hour or 0), int(ind.end_hour or 0)
    if end_h <= start_h:
        end_h += 24
    now_floor = floor_hour(now)
    first: date = min(data).date() - timedelta(days=1)
    last: date = max(data).date()
    pick = max if ind.stat == "max" else min

    out: list[Point] = []
    day = first
    while day <= last:
        base = datetime.combine(day, time(0), tzinfo=KST)
        stamps = [base + h * HOUR for h in range(start_h + 1, end_h + 1)]
        present = [(data[t], t) for t in stamps if t in data]
        missing = [t for t in stamps if t not in data]
        future = [t for t in stamps if t >= now_floor]
        ok = bool(present) and all(t < now_floor for t in missing) and (bool(future) or not missing)
        if ok:
            best_value = pick(v for v, _ in present)
            best_time = next(t for v, t in present if v == best_value)
            out.append(Point(day.isoformat(), best_time, best_value, base + start_h * HOUR, base + end_h * HOUR))
        day += timedelta(days=1)
    return out


def _period_change(ind: Indicator, indicators: dict[str, Indicator], ms: ModelSeries, now: datetime) -> list[Point]:
    """전날 같은 시간대 값과의 차이 (음수 = 하강)."""
    base_points = {p.slot: p for p in _period_extreme(indicators[ind.of or ""], ms, now)}
    out: list[Point] = []
    for slot, p in sorted(base_points.items()):
        prev = base_points.get((date.fromisoformat(slot) - timedelta(days=1)).isoformat())
        if prev is not None:
            out.append(Point(slot, p.time, p.value - prev.value, p.start, p.end))
    return out


def indicator_points(ind: Indicator, indicators: dict[str, Indicator], ms: ModelSeries, now: datetime) -> list[Point]:
    """한 모델의 예측값에서 지표 값 목록을 만든다 (시간순). 자료가 없으면 빈 목록."""
    if ind.kind == "hourly":
        return _hourly(ind, ms)
    if ind.kind == "rolling_sum":
        return _rolling_sum(ind, ms)
    if ind.kind == "period_extreme":
        return _period_extreme(ind, ms, now)
    if ind.kind == "period_change":
        return _period_change(ind, indicators, ms, now)
    raise ValueError(f"알 수 없는 지표 종류: {ind.kind}")


def in_horizon(p: Point, now: datetime, horizon_end: datetime) -> bool:
    """예측 대상 구간(지금 ~ horizon_end)에 속하는 값인가.

    구간이 아직 끝나지 않았고(end > now) 대표 시각이 예측 범위 안(time <= horizon_end)이어야 한다.
    """
    return p.end > now and p.time <= horizon_end
