"""기상청 종관기상관측(ASOS) 시간자료·일자료 — 실제로 관측된 기온·강수량·바람·습도·적설.

공공데이터포털의 두 서비스(둘 다 getWthrDataList, 전국 약 100개 지점, 전날까지의 자료). **활용신청은 서비스마다 따로** 해야 한다 —
한쪽만 승인된 인증키로 다른 쪽을 부르면 "등록되지 않은 서비스키"(HTTP 403)가 온다.
    「기상청_지상(종관, ASOS) 시간자료 조회서비스」 AsosHourlyInfoService — 시간별 값. 3·12시간 강수량처럼 특보 기준 그대로 따질 수 있다.
    「기상청_지상(종관, ASOS) 일자료 조회서비스」   AsosDalyInfoService   — 하루 단위 값(일강수량, 1시간 최다강수량, 최대풍속, 최저·최고기온 …).
이 서비스의 쓰임: 과거 검증(백테스트)의 '실제 결과'. 특보 발표 이력은 60일 전까지만 조회되지만 관측 자료는 오래전까지 있으므로,
"그때의 예보로 계산한 가능성" 대 "실제 관측이 특보 기준에 닿았는지"를 여러 해·여러 계절에 걸쳐 맞춰 볼 수 있다.

시간자료 한 행(시각 하나)에서 쓰는 값
    tm 시각(YYYY-MM-DD HH:MM, 한국 시각) · stnId 지점번호 · stnNm 지점 이름
    ta 기온(℃) · rn 강수량(mm, 직전 1시간) · ws 풍속(m/s) · hm 습도(%) · hr3Fhsc 3시간 신적설(cm) · dsnw 적설(cm)
    값이 없으면 빈 문자열이다(강수량의 빈 값은 '비 없음'으로 본다 — 관측 자체가 빠진 시각과는 구분되지 않는다).
일자료 한 행(하루)에서 쓰는 값 — 활용가이드(2019-12-20 판)의 항목 이름
    tm 날짜(YYYY-MM-DD) · sumRn 일강수량(mm) · hr1MaxRn 1시간 최다강수량(mm) · maxWs 최대 풍속(m/s) · maxInsWs 최대 순간풍속(m/s)
    minTa·maxTa 최저·최고 기온(℃) · avgRhm 평균 습도(%) · ddMefs 일 최심신적설(cm)
    minRhm 최소 습도(%) · avgWs 평균 풍속(m/s) — 지역별 기록(teukbo/records.py)의 체감온도 추정에 쓴다
    전날 자료는 오전 11시 뒤에 조회된다. 강수량·신적설의 빈 값은 '없음'으로 본다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from ..http import get_json
from ..timeutil import KST
from .datagokr import NoData, extract_items

ENDPOINT = "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"
PAGE_SIZE = 999  # 한 번에 받을 수 있는 최대 행 수
FIELDS = {"ta": "temp", "rn": "precip", "ws": "wind", "hm": "rh", "hr3Fhsc": "snow3h", "dsnw": "snow_depth"}
DAILY_ENDPOINT = "https://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"
DAILY_FIELDS = {"sumRn": "rain", "hr1MaxRn": "rain_1h_max", "maxWs": "wind_max", "maxInsWs": "gust_max",
                "minTa": "temp_min", "maxTa": "temp_max", "avgRhm": "rh_avg", "ddMefs": "snow_new",
                "minRhm": "rh_min", "avgWs": "wind_avg"}


def _number(value: Any) -> float | None:
    try:
        return float(value) if str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def parse_rows(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """응답 행들 → [{t: 시각, stn: 지점번호, name: 지점 이름, temp, precip, wind, rh, snow3h, snow_depth}] (값이 없으면 None)."""
    out = []
    for row in items:
        try:
            when = datetime.strptime(str(row.get("tm", "")), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            continue
        rec: dict[str, Any] = {"t": when, "stn": str(row.get("stnId", "")), "name": str(row.get("stnNm", ""))}
        for raw, name in FIELDS.items():
            rec[name] = _number(row.get(raw))
        out.append(rec)
    return out


def fetch_hourly(key: str, station: str, start: datetime, end: datetime, http_opts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """한 지점의 start ~ end(시각 포함, 한국 시각) 시간자료. 자료가 없으면 빈 목록. end 는 어제 이전이어야 한다."""
    opts = http_opts or {}
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = get_json(
            ENDPOINT,
            {"serviceKey": key, "pageNo": page, "numOfRows": PAGE_SIZE, "dataType": "JSON", "dataCd": "ASOS", "dateCd": "HR",
             "startDt": start.strftime("%Y%m%d"), "startHh": start.strftime("%H"), "endDt": end.strftime("%Y%m%d"), "endHh": end.strftime("%H"), "stnIds": station},
            timeout=float(opts.get("timeout_seconds", 30)),
            retries=int(opts.get("retries", 2)),
            backoff=float(opts.get("backoff_seconds", 3)),
            label=f"ASOS {station}",
        )
        try:
            items, total = extract_items(payload, f"ASOS {station}")
        except NoData:
            break
        out.extend(parse_rows(items))
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return out


def parse_daily_rows(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """일자료 응답 행들 → [{d: 날짜, stn, name, rain, rain_1h_max, wind_max, gust_max, temp_min, temp_max, rh_avg, snow_new, rh_min, wind_avg}] (값이 없으면 None)."""
    out = []
    for row in items:
        try:
            day = datetime.strptime(str(row.get("tm", "")).strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        rec: dict[str, Any] = {"d": day, "stn": str(row.get("stnId", "")), "name": str(row.get("stnNm", ""))}
        for raw, name in DAILY_FIELDS.items():
            rec[name] = _number(row.get(raw))
        out.append(rec)
    return out


def fetch_daily(key: str, station: str, start: date, end: date, http_opts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """한 지점의 start ~ end(날짜 포함) 일자료. 자료가 없으면 빈 목록. end 는 어제 이전이어야 한다(어제 것은 오전 11시 뒤)."""
    opts = http_opts or {}
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = get_json(
            DAILY_ENDPOINT,
            {"serviceKey": key, "pageNo": page, "numOfRows": PAGE_SIZE, "dataType": "JSON", "dataCd": "ASOS", "dateCd": "DAY",
             "startDt": start.strftime("%Y%m%d"), "endDt": end.strftime("%Y%m%d"), "stnIds": station},
            timeout=float(opts.get("timeout_seconds", 30)),
            retries=int(opts.get("retries", 2)),
            backoff=float(opts.get("backoff_seconds", 3)),
            label=f"ASOS 일자료 {station}",
        )
        try:
            items, total = extract_items(payload, f"ASOS 일자료 {station}")
        except NoData:
            break
        out.extend(parse_daily_rows(items))
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return out
