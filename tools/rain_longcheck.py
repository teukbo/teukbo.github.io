"""호우주의보 가능성의 긴 기간 확인: 2년치 과거 예보로 다시 계산한 가능성 대 실제로 관측된 비.

    python3 tools/rain_longcheck.py                                   (네트워크 없이, tools/cache 의 저장본으로 — 표만 찍는다)
    python3 tools/rain_longcheck.py --json data/rain_longcheck.json   (첫 화면의 검증 칸에 싣는 요약도 저장)

특보 발표 기록은 60일 전까지만 조회되어, 서비스의 과거 검증(teukbo/backtest.py)은 가을 두 달뿐이다. 여기서는 '실제 결과'를
기상청 관측(ASOS 일자료)으로 바꿔 2024-07 부터 — 장마철을 두 번 넘겨 — 같은 계산식을 다시 돌려 본다.

무엇을 어떻게 맞춰 보나
- 예보: tools/cache/rain_<모델>_<기간>.json.gz + rain_base.json.gz (Open-Meteo Previous Runs, 시간별 강수량의 '하루 전·이틀 전에 예측한 값').
- 계산: 매일 0시(한국 시각)에 그때까지의 예보로 호우주의보 가능성을 다시 계산한다 — 서비스와 같은 식
  (teukbo/scoring.py 의 계산식과 teukbo/area.py 의 주변 30km 보정). 기상청 단기예보는 과거분이 없어 해외 모델 4개만 쓴다.
- 실제: 그 0시부터 이틀(달력으로 그날과 다음 날) 동안 그 지역 관측소의 일강수량. 관측소가 있는 지역만 본다.
- 하루 단위 자료로는 특보 기준(3시간 60mm·12시간 110mm)에 닿았는지를 그대로 알 수 없다. 그래서 '큰비'를 일강수량으로 정한다:
  80mm 이상(기상청이 호우일수를 셀 때 쓰는 값)을 기본으로, 60mm·110mm 도 함께 적는다.
  또 관측소 한 점의 비는 그 지역에서 가장 많이 온 곳보다 적을 수 있다 — 특보는 구역 어디든 기준에 닿을 것 같으면 나므로,
  여기의 '맞은 비율'은 실제 특보 기준으로 셈한 것보다 낮게 나온다(불리한 쪽으로 치우침).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teukbo import area, backtest, rail, rail_run, scoring  # noqa: E402
from teukbo.config import load_config  # noqa: E402
from teukbo.timeutil import iso, now_kst  # noqa: E402

CACHE = ROOT / "tools" / "cache"
MODELS = ("ecmwf_ifs025", "ncep_gfs_seamless", "dwd_icon_seamless", "jma_seamless")
HEAVY_MM = (60.0, 80.0, 110.0)  # '큰비'로 보는 일강수량 — 가운데 값이 기본
BUCKETS = (("0%", 0, 0), ("1~29% (낮음)", 1, 29), ("30~59% (주의)", 30, 59), ("60~100% (높음)", 60, 100))


def load_forecasts(cache: Path = CACHE) -> tuple[datetime, int, dict[str, dict[str, dict[int, list[float | None]]]]]:
    """기간별로 나뉜 저장본을 이어 붙인다 → (첫 시각, 시간 수, 모델 → 지역 → {1: 하루 전 예측, 2: 이틀 전 예측})."""
    parts: list[tuple[datetime, int, dict[str, Any]]] = []
    for path in sorted(cache.glob("rain_*_20*.json.gz")) + [cache / "rain_base.json.gz"]:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        parts.append((datetime.fromisoformat(doc["first"]), int(doc["hours"]), doc["models"]))
    first = min(p[0] for p in parts)
    last = max(p[0] + timedelta(hours=p[1]) for p in parts)
    hours = int((last - first).total_seconds() // 3600)
    out: dict[str, dict[str, dict[int, list[float | None]]]] = {m: {} for m in MODELS}
    for start, n, models in parts:
        offset = int((start - first).total_seconds() // 3600)
        for model, regions in models.items():
            if model not in out:
                continue
            for rid, leads in regions.items():
                slot = out[model].setdefault(rid, {1: [None] * hours, 2: [None] * hours})
                for lead, series in leads.items():
                    slot[int(lead)][offset:offset + n] = series[:n]
    return first, hours, out


def load_observations(path: Path) -> tuple[date, int, dict[str, dict[str, list[float | None]]]]:
    """관측 일자료 저장본 → (첫날, 날 수, 지역 → {"rain": 그날 관측소들 가운데 가장 많이 온 양, "ok": 자료가 있던 날})."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    first, days = date.fromisoformat(doc["first"]), int(doc["days"])
    out: dict[str, dict[str, list[float | None]]] = {}
    for station in doc["stations"].values():
        slot = out.setdefault(station["region"], {"rain": [None] * days, "ok": [False] * days})
        for i in range(days):
            if station["temp_min"][i] is None and station["rain"][i] is None:
                continue  # 그날 자료 자체가 없다
            slot["ok"][i] = True
            value = station["rain"][i] or 0.0  # 빈 값은 '비 없음'
            if slot["rain"][i] is None or value > slot["rain"][i]:
                slot["rain"][i] = value
    return first, days, out


def rolling_reach(series: Sequence[float | None]) -> list[float]:
    """시각 i 에서 끝나는 3시간·12시간 누적을 호우주의보 기준(60·110mm)으로 나눈 값 가운데 큰 쪽. 빈 값은 0으로 본다."""
    n = len(series)
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(series):
        prefix[i + 1] = prefix[i] + (v or 0.0)
    return [max((prefix[i + 1] - prefix[max(0, i - 2)]) / 60.0, (prefix[i + 1] - prefix[max(0, i - 11)]) / 110.0) for i in range(n)]


def replay(cfg, first: datetime, hours: int, forecasts: dict[str, dict[str, dict[int, list[float | None]]]]) -> tuple[list[int], dict[str, list[tuple[int, int, float]]]]:
    """매일 0시의 가능성 → (재현한 시각 번호들, 지역 → [(주변 보정까지 한 가능성, 보정 전 가능성, 주변에서 가장 큰 '상위 모델 평균 도달률')] — 재현 순서대로)."""
    conf = cfg.thresholds.types["heavy_rain"]["area_score"]
    near = area.neighbors(cfg.regions, float(conf["radius_km"]))
    top_models, points = int(conf["top_models"]), conf["points"]
    reach: dict[str, dict[str, dict[int, list[float]]]] = {m: {rid: {lead: rolling_reach(s) for lead, s in leads.items()} for rid, leads in regions.items()} for m, regions in forecasts.items()}
    starts = [r for r in range(0, hours - 48, 24) if (first + timedelta(hours=r)).hour == 0]
    out: dict[str, list[tuple[int, int, float]]] = {region.id: [] for region in cfg.regions}
    for r in starts:
        own: dict[str, float] = {}
        base: dict[str, int] = {}
        for region in cfg.regions:
            values = []
            for model in MODELS:
                raw = forecasts[model].get(region.id)
                if raw is None or all(v is None for v in raw[1][r + 1:r + 25]):
                    continue  # 그때 이 모델의 예보가 없다
                leads = reach[model][region.id]
                far = leads[2] if any(v is not None for v in raw[2][r + 25:r + 49]) else leads[1]
                values.append(max(max(leads[1][r + 1:r + 25]), max(far[r + 25:r + 49])))
            if not values:
                continue
            base[region.id] = scoring.compute_probability(values, [v >= 1.0 for v in values], cfg.settings.scoring).percent
            score = area.model_score(values, top_models)
            if score is not None:
                own[region.id] = score
        for region in cfg.regions:
            if region.id not in base:
                out[region.id].append((-1, -1, 0.0))
                continue
            best = max((own[n] for n in near.get(region.id, [region.id]) if n in own), default=None)
            lifted = area.interpolate(points, best) if best is not None else 0
            out[region.id].append((max(base[region.id], lifted), base[region.id], best or 0.0))
    return starts, out


def bucket_of(percent: int) -> int:
    return next(i for i, (_, lo, hi) in enumerate(BUCKETS) if lo <= percent <= hi)


def summarize(first: datetime, starts: Sequence[int], scores: dict[str, list[tuple[int, int, float]]], obs_first: date, obs_days: int,
              obs: dict[str, dict[str, list[float | None]]], heavy_mm: float = 80.0, since: date | None = None, until: date | None = None,
              months: Sequence[int] | None = None) -> dict[str, Any]:
    """가능성 구간별로 '이틀 안에 큰비가 왔는가'와, 큰비가 온 날을 하루 전·당일 0시에 '주의' 이상으로 표시했는가.

    since·until·months 로 기간을 좁힌다(재현한 날 기준)."""
    tables = {which: [{"cases": 0, "hits": 0, "days": set()} for _ in BUCKETS] for which in ("with_area", "base")}
    capture = {which: {"events": 0, "day_before": 0, "same_day": 0} for which in ("with_area", "base")}
    replay_days = [(first + timedelta(hours=r)).date() for r in starts]
    index_of = {d: k for k, d in enumerate(replay_days)}
    event_days: set[date] = set()
    regions_used = 0
    for rid, record in obs.items():
        series = scores.get(rid)
        if not series:
            continue
        regions_used += 1
        rain, ok = record["rain"], record["ok"]
        for k, day in enumerate(replay_days):
            if (since and day < since) or (until and day > until) or (months and day.month not in months):
                continue
            i = (day - obs_first).days
            if i < 0 or i + 1 >= obs_days or not (ok[i] and ok[i + 1]) or series[k][0] < 0:
                continue
            hit = max(rain[i] or 0.0, rain[i + 1] or 0.0) >= heavy_mm
            for which, percent in (("with_area", series[k][0]), ("base", series[k][1])):
                row = tables[which][bucket_of(percent)]
                row["cases"] += 1
                if hit:
                    row["hits"] += 1
                    row["days"].add(day)
            # 큰비가 온 날(그 지역의 그날 일강수량) 하나하나: 전날 0시와 당일 0시의 표시
            if (rain[i] or 0.0) >= heavy_mm:
                event_days.add(day)
                before = index_of.get(day - timedelta(days=1))
                for which, col in (("with_area", 0), ("base", 1)):
                    capture[which]["events"] += 1
                    if before is not None and series[before][col] >= 30:
                        capture[which]["day_before"] += 1
                    if series[k][col] >= 30:
                        capture[which]["same_day"] += 1
    for which in tables:
        for row in tables[which]:
            row["days"] = len(row["days"])
    return {"heavy_mm": heavy_mm, "regions": regions_used, "event_days": len(event_days), "tables": tables, "capture": capture}


def show(title: str, result: dict[str, Any], out: list[str]) -> None:
    out.append(f"\n== {title} — 큰비 = 일강수량 {result['heavy_mm']:g}mm 이상, 관측소가 있는 {result['regions']}개 지역, 큰비가 온 날 {result['event_days']}일 ==")
    for which, label in (("with_area", "지금의 계산(주변 30km 보정 포함)"), ("base", "보정 전 계산식")):
        cap = result["capture"][which]
        rate = lambda a, b: f"{100 * a / b:.0f}%" if b else "–"  # noqa: E731
        out.append(f"  [{label}] 큰비 {cap['events']}건 가운데 전날 0시에 '주의' 이상 {cap['day_before']}건({rate(cap['day_before'], cap['events'])}) · 당일 0시에 {cap['same_day']}건({rate(cap['same_day'], cap['events'])})")
        for (name, _, _), row in zip(BUCKETS, result["tables"][which]):
            out.append(f"      {name:<14} {row['cases']:>7,}번 가운데 이틀 안에 큰비 {row['hits']:>5,}번 ({100 * row['hits'] / row['cases']:.1f}%) · 서로 다른 날 {row['days']}일" if row["cases"] else f"      {name:<14} 없음")
        alerts = sum(row["cases"] for row in result["tables"][which][2:])
        hits = sum(row["hits"] for row in result["tables"][which][2:])
        out.append(f"      '주의' 이상 {alerts:,}번 가운데 {hits:,}번({rate(hits, alerts)})이 큰비로 이어짐")


def warning_overlap(cfg, data_dir: Path, obs_first: date, obs_days: int, obs: dict[str, dict[str, list[float | None]]], heavy_mm: float) -> dict[str, Any] | None:
    """특보 기록이 있는 최근 기간에, 실제 호우특보가 시작된 날 전후(전날~다음 날) 그 지역 관측소의 일강수량이 heavy_mm 를 넘었는가.

    '관측소 일강수량'이 '특보 발표'와 얼마나 다른 잣대인지 보여 주는 숫자다. 특보 기록이 없으면 None."""
    events = rail_run.load_events(data_dir)
    if not events:
        return None
    truth = backtest.region_intervals(cfg, rail.build_intervals(events))
    rows = [e for e in backtest.truth_events(cfg, truth) if e["type"] == "heavy_rain" and e["region"] in obs]
    if not rows:
        return None
    peaks = []
    for e in rows:
        i = (e["time"].date() - obs_first).days
        rain = obs[e["region"]]["rain"]
        peaks.append(max((rain[j] or 0.0) for j in (i - 1, i, i + 1) if 0 <= j < obs_days))
    peaks.sort()
    return {"from": min(e["time"] for e in rows).date().isoformat(), "to": max(e["time"] for e in rows).date().isoformat(),
            "warnings": len(rows), "reached": sum(1 for v in peaks if v >= heavy_mm), "median_mm": round(peaks[len(peaks) // 2], 1)}


def _counts(result: dict[str, Any], which: str) -> dict[str, Any]:
    table = result["tables"][which]
    return {"events": result["capture"][which]["events"], "event_days": result["event_days"],
            "day_before": result["capture"][which]["day_before"], "same_day": result["capture"][which]["same_day"],
            "caution_or_higher": {"alerts": sum(r["cases"] for r in table[2:]), "hits": sum(r["hits"] for r in table[2:])},
            "high": {"alerts": table[3]["cases"], "hits": table[3]["hits"]},
            "calibration": [{"bucket": name, "from": lo, "to": hi, "cases": r["cases"], "hits": r["hits"]} for (name, lo, hi), r in zip(BUCKETS, table)]}


def build_document(first: datetime, hours: int, replays: int, results: dict[str, dict[str, Any]], overlap: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """첫 화면에 싣는 요약(data/rain_longcheck.json). results: summarize() 결과들 — main() 이 만드는 이름 그대로."""
    main_result = results["all_80"]
    last_day = (first + timedelta(hours=hours - 1)).date()
    notes = [
        "특보 발표 기록은 60일 전까지만 조회되어, 더 오래된 기간은 '실제 결과'를 기상청 관측(ASOS 일자료)으로 바꿔 확인했습니다. 그래서 이 표는 '특보가 발표됐는가'가 아니라 '관측소 한 곳에 하루 80mm 이상 내렸는가'를 봅니다.",
        "특보는 구역 어디든 기준(3시간 60mm·12시간 110mm)에 닿을 것 같으면 발표되지만, 관측소는 그 구역의 한 점이고 하루 합계만 압니다. 두 잣대는 다릅니다" + (
            f" — 특보 기록이 있는 최근 기간의 호우특보 {overlap['warnings']}건 가운데 그 전후로 관측소 일강수량이 80mm 를 넘은 것은 {overlap['reached']}건뿐이었습니다(중간값 {overlap['median_mm']:g}mm)." if overlap else "."),
        "따라서 여기의 비율은 위의 특보 기준 표와 바로 견줄 수 없습니다. '가능성이 높을수록 실제로 큰비가 잦았는가', '큰비를 미리 가려냈는가'를 긴 기간으로 보는 보조 확인입니다.",
        "매일 0시(한국 시각)에 그때까지의 예보로 다시 계산했고, '이틀 안'은 그날과 다음 날입니다. 기상청 단기예보는 과거분이 없어 해외 수치모델 4개만 썼습니다.",
        "주변 30km 의 예보까지 보는 기준은 2026년 9~11월 자료로 맞춘 것입니다. 그 기간을 뺀 나머지(기준을 맞출 때 보지 않은 자료)만으로 셈한 값을 따로 적었습니다.",
        "해마다 차이가 큽니다(아래 '기간별'). 한 번의 큰비가 여러 지역·여러 날에 걸치므로 표본은 서로 독립이 아닙니다 — 큰비가 온 날 수를 함께 보세요.",
    ]
    doc = {"schema": 1, "updated_at": iso(now), "hazard": "heavy_rain_advisory", "heavy_mm": main_result["heavy_mm"],
           "period": {"from": first.date().isoformat(), "to": last_day.isoformat(), "replays": replays},
           "regions": main_result["regions"], "models": list(MODELS),
           **_counts(main_result, "with_area"), "before_area_score": _counts(main_result, "base"),
           "unseen": {"until": "2026-09-11", **_counts(results["unseen"], "with_area"), "before_area_score": _counts(results["unseen"], "base")},
           "by_period": [{"name": name, **{k: v for k, v in _counts(results[key], "with_area").items() if k != "calibration"}}
                         for key, name in (("y1", "2024년 7월 ~ 2025년 6월"), ("y2", "2025년 7월 ~ 2026년 6월"), ("summer", "여름(6~9월)"), ("other", "그 밖의 달"))],
           "by_heavy_mm": {f"{mm:g}": {k: v for k, v in _counts(results[f"all_{mm:g}"], "with_area").items() if k != "calibration"} for mm in HEAVY_MM},
           "warning_overlap": overlap, "notes": notes}
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="호우주의보 가능성의 긴 기간 확인(관측 일자료 기준)")
    parser.add_argument("--obs", type=Path, default=CACHE / "asos_daily.json.gz")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="실제 특보 기록(rail/events)이 있는 곳 — 두 잣대의 차이를 적는 데 쓴다")
    parser.add_argument("--json", type=Path, default=None, help="첫 화면에 싣는 요약을 저장할 파일(data/rain_longcheck.json)")
    args = parser.parse_args(argv)
    cfg = load_config()
    first, hours, forecasts = load_forecasts()
    obs_first, obs_days, obs = load_observations(args.obs)
    starts, scores = replay(cfg, first, hours, forecasts)
    lines = [f"예보 {first:%Y-%m-%d} ~ {(first + timedelta(hours=hours - 1)):%Y-%m-%d} · 매일 0시 재현 {len(starts)}회 · 관측 {obs_first} ~ {obs_first + timedelta(days=obs_days - 1)}, {len(obs)}개 지역"]
    results = {}
    for mm in HEAVY_MM:
        results[f"all_{mm:g}"] = summarize(first, starts, scores, obs_first, obs_days, obs, mm)
        show("전체 기간", results[f"all_{mm:g}"], lines)
    tuned_from = date(2026, 9, 12)  # 주변 보정의 기준을 맞출 때 쓴 기간의 시작
    for key, title, kw in (("unseen", "기준을 맞출 때 쓰지 않은 기간(2024-07 ~ 2026-09-11)", {"until": tuned_from - timedelta(days=1)}),
                           ("tuned", "기준을 맞춘 기간(2026-09-12 ~)", {"since": tuned_from}),
                           ("summer", "여름(6~9월)만", {"months": (6, 7, 8, 9)}),
                           ("other", "그 밖의 달", {"months": (1, 2, 3, 4, 5, 10, 11, 12)}),
                           ("y1", "2024-07 ~ 2025-06", {"until": date(2025, 6, 30)}),
                           ("y2", "2025-07 ~ 2026-06", {"since": date(2025, 7, 1), "until": date(2026, 6, 30)})):
        results[key] = summarize(first, starts, scores, obs_first, obs_days, obs, 80.0, **kw)
        show(title, results[key], lines)
    overlap = warning_overlap(cfg, args.data_dir, obs_first, obs_days, obs, 80.0)
    if overlap:
        lines.append(f"\n두 잣대의 차이: {overlap['from']} ~ {overlap['to']} 의 실제 호우특보 {overlap['warnings']}건(관측소가 있는 지역) 가운데 전후 하루 안에 관측소 일강수량이 80mm 이상이었던 것 {overlap['reached']}건 · 중간값 {overlap['median_mm']:g}mm")
    print("\n".join(lines))
    if args.json:
        doc = build_document(first, hours, len(starts), results, overlap, now_kst())
        args.json.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
