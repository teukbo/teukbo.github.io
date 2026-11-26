"""지역별 기록(teukbo/records.py): 특보가 발효된 날, 가까운 관측소의 그날 값, 사이트 조립에 싣는 꼴. 네트워크는 쓰지 않는다."""

import dataclasses
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from teukbo import hourly, obs, rail, rail_run, records, site, storage
from teukbo.indicators import kma_summer_apparent_temp, kma_winter_apparent_temp
from teukbo.sources import asos

from .helpers import config, kst, region

CFG = config()
ZONE = region("beolgyo").zone_codes[0]
STATIONS = [{"stn": "258", "name": "보성군", "region": "boseong", "lon": 127.2123, "lat": 34.7633},
            {"stn": "174", "name": "순천", "region": "suncheon", "lon": 127.3694, "lat": 35.0204},
            {"stn": "90", "name": "속초", "region": "L1022210", "lon": 128.5647, "lat": 38.2509}]


def obs_row(day: date, **values):
    base = {name: None for name in asos.DAILY_FIELDS.values()}
    base.update(values)
    return {"d": day, "stn": "", "name": "", **base}


def write_events(data_dir: Path, rows_by_day: dict[str, list[list]]) -> None:
    """data/rail/events/연-월.json 을 손으로 만든다. 행: [구역, 종류, 단계, 명령(1 발표·2 해제), 발표 시각, 발효 시각, 해제 시각]."""
    months: dict[str, dict] = {}
    for ymd, rows in rows_by_day.items():
        doc = months.setdefault(f"{ymd[:4]}-{ymd[4:6]}", {"schema": 1, "month": f"{ymd[:4]}-{ymd[4:6]}", "days": {}, "fetched": {}})
        doc["days"][ymd] = rows
    for month, doc in months.items():
        storage.write_json(rail_run.rail_dir(data_dir) / "events" / f"{month}.json", doc)


class StationTest(unittest.TestCase):
    def test_distance(self):
        self.assertAlmostEqual(records.distance_km(37.5665, 126.9780, 35.1796, 129.0756), 325, delta=3)  # 서울–부산
        self.assertEqual(records.distance_km(35.0, 127.0, 35.0, 127.0), 0.0)

    def test_station_inside_the_region_wins_over_a_nearer_one(self):
        beolgyo = region("beolgyo")
        near = {"stn": "1", "name": "가까운 곳", "region": "other", "lat": beolgyo.lat + 0.01, "lon": beolgyo.lon}
        inside = {"stn": "2", "name": "지역 안", "region": "beolgyo", "lat": beolgyo.lat + 0.1, "lon": beolgyo.lon}
        self.assertEqual(records.pick_station(beolgyo, [near, inside], 40)["stn"], "2")
        picked = records.pick_station(beolgyo, [near], 40)
        self.assertEqual((picked["stn"], picked["name"], picked["km"]), ("1", "가까운 곳", 1.1))

    def test_nearest_station_and_the_distance_limit(self):
        beolgyo = region("beolgyo")
        picked = records.pick_station(beolgyo, STATIONS, 40)
        self.assertEqual((picked["stn"], picked["name"]), ("258", "보성군"))
        self.assertTrue(5 < picked["km"] < 25)
        self.assertEqual(picked["km"], round(picked["km"], 1))
        self.assertIsNone(records.pick_station(beolgyo, STATIONS, picked["km"] - 0.1))  # 한도보다 멀면 쓰지 않는다
        self.assertIsNone(records.pick_station(beolgyo, [], 40))

    def test_most_regions_have_a_station_in_range(self):
        stations = obs.load_stations()
        limit = float(CFG.settings.records["max_station_km"])
        picked = [records.pick_station(r, stations, limit) for r in CFG.regions]
        self.assertGreater(sum(1 for p in picked if p), len(CFG.regions) * 0.9)
        self.assertTrue(all(p["km"] <= limit for p in picked if p))


class WarningDaysTest(unittest.TestCase):
    def test_a_warning_counts_on_every_day_it_touches(self):
        spans = [("cold_wave", "advisory", kst(2026, 11, 6, 21), kst(2026, 11, 7, 9))]
        days = records.warnings_by_day(spans, date(2026, 11, 1), date(2026, 11, 30))
        item = ["cold_wave", "advisory", "2026-11-06T21:00+09:00", "2026-11-07T09:00+09:00"]
        self.assertEqual(days, {"2026-11-06": [item], "2026-11-07": [item]})

    def test_ending_at_midnight_does_not_reach_the_next_day(self):
        spans = [("strong_wind", "advisory", kst(2026, 11, 18, 4), kst(2026, 11, 19, 0))]
        self.assertEqual(list(records.warnings_by_day(spans, date(2026, 11, 1), date(2026, 11, 30))), ["2026-11-18"])

    def test_open_warning_runs_to_the_last_day_with_a_null_end(self):
        spans = [("heavy_snow", "warning", kst(2026, 11, 23, 15), None)]
        days = records.warnings_by_day(spans, date(2026, 11, 1), date(2026, 11, 25))
        self.assertEqual(list(days), ["2026-11-23", "2026-11-24", "2026-11-25"])
        self.assertEqual(days["2026-11-25"], [["heavy_snow", "warning", "2026-11-23T15:00+09:00", None]])

    def test_window_clips_and_same_day_spans_stay_separate_and_duplicates_merge(self):
        spans = [("strong_wind", "advisory", kst(2026, 11, 18, 14), kst(2026, 11, 18, 20)),
                 ("strong_wind", "advisory", kst(2026, 11, 18, 2), kst(2026, 11, 18, 6)),
                 ("strong_wind", "advisory", kst(2026, 11, 18, 2), kst(2026, 11, 18, 6)),  # 구역이 둘인 지역: 같은 구간이 두 번
                 ("heavy_rain", "advisory", kst(2026, 10, 30, 2), kst(2026, 11, 1, 6))]
        days = records.warnings_by_day(spans, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(list(days), ["2026-11-01", "2026-11-18"])  # 창 밖(10월)의 날은 싣지 않는다
        self.assertEqual([item[2] for item in days["2026-11-18"]], ["2026-11-18T02:00+09:00", "2026-11-18T14:00+09:00"])

    def test_names_become_ids_and_unknown_kinds_are_dropped(self):
        intervals = [rail.Interval(ZONE, "강풍", "주의보", kst(2026, 11, 18, 4), kst(2026, 11, 18, 15)),
                     rail.Interval(ZONE, "한파", "경보", kst(2026, 11, 20, 21), None),
                     rail.Interval(ZONE, "건조", "주의보", kst(2026, 11, 10, 10), kst(2026, 11, 12, 10)),  # 이 서비스가 다루지 않는 종류
                     rail.Interval(ZONE, "호우", "주의보", kst(2026, 11, 9, 10), kst(2026, 11, 9, 10)),  # 길이 0
                     rail.Interval("S1312010", "풍랑", "주의보", kst(2026, 11, 9, 10), kst(2026, 11, 9, 16))]
        spans = records.spans_by_code(intervals, CFG)
        self.assertEqual(list(spans), [ZONE])
        self.assertEqual([(s[0], s[1]) for s in spans[ZONE]], [("strong_wind", "advisory"), ("cold_wave", "warning")])


class DayValuesTest(unittest.TestCase):
    def test_values_follow_the_vars_order_with_one_decimal(self):
        row = obs_row(date(2026, 12, 10), temp_min=-12.34, temp_max=-2.06, wind_avg=3.1, rh_min=30.0, rain=None, snow_new=3.24, wind_max=7.4, gust_max=13.0)
        values = dict(zip(records.VARS, records.day_values(row)))
        self.assertEqual((values["temp_min"], values["temp_max"], values["rain"], values["snow_new"], values["wind_max"], values["gust_max"]), (-12.3, -2.1, 0.0, 3.2, 7.4, 13.0))

    def test_daily_values_carry_no_feel_temperature_estimate(self):
        # 하루 값(최저기온·평균풍속)으로 어림한 체감온도는 싣지 않는다 — 새벽에는 바람이 약해 실제보다 낮게 나왔다(보성군 2026-11-24: -4.1℃ 대 실제 -1.5℃)
        self.assertEqual(records.VARS, ("temp_min", "temp_max", "rain", "snow_new", "wind_max", "gust_max"))
        self.assertFalse(hasattr(records, "FEEL_BASIS"))

    def test_missing_ingredients(self):
        row = obs_row(date(2026, 12, 10), temp_min=-5.0, temp_max=2.0)
        values = dict(zip(records.VARS, records.day_values(row)))
        self.assertIsNone(values["wind_max"])
        self.assertEqual((values["rain"], values["snow_new"]), (0.0, 0.0))  # 그날 관측이 있는데 비어 있으면 '없음'
        self.assertIsNone(records.day_values(obs_row(date(2026, 12, 10), rh_avg=50.0)))  # 쓸 값이 하나도 없는 날


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        write_events(self.dir, {
            "20261115": [],
            "20261118": [[ZONE, "강풍", "주의보", "1", "202611180300", "202611180400", None],
                         [ZONE, "건조", "주의보", "1", "202611180300", "202611180400", None],
                         [ZONE, "강풍", "주의보", "2", "202611181400", None, "202611181500"]],
            "20261120": [[ZONE, "한파", "주의보", "1", "202611201600", "202611202100", None]],
            "20261121": [[ZONE, "한파", "주의보", "2", "202611210900", None, "202611211000"]],
            "20261125": [[ZONE, "대설", "경보", "1", "202611250500", "202611250600", None]],
        })
        obs.save_rows(self.dir, "258", "보성군", [
            obs_row(date(2026, 11, 10), temp_min=4.0, temp_max=15.0, wind_avg=2.0),  # 특보 이력이 시작되기 전
            obs_row(date(2026, 11, 18), temp_min=3.1, temp_max=9.8, wind_avg=6.2, wind_max=11.3, gust_max=21.4, rain=2.5),
            obs_row(date(2026, 11, 21), temp_min=-6.4, temp_max=1.2, wind_avg=3.0, wind_max=6.0, gust_max=10.1),
            obs_row(date(2026, 11, 24), temp_min=-1.0, temp_max=5.0, wind_avg=1.0),
        ])

    def doc(self, region_id="beolgyo", cfg=CFG):
        return next(d for d in records.build_all(cfg, self.dir, STATIONS) if d["id"] == region_id)

    def test_document_shape(self):
        doc = self.doc()
        self.assertEqual((doc["schema"], doc["id"], doc["name"]), (1, "beolgyo", "보성(벌교)"))
        self.assertEqual((doc["station"]["stn"], doc["station"]["name"]), ("258", "보성군"))
        self.assertEqual((doc["from"], doc["to"], doc["obs_to"]), ("2026-11-15", "2026-11-25", "2026-11-24"))
        self.assertEqual(doc["vars"], ["temp_min", "temp_max", "rain", "snow_new", "wind_max", "gust_max"])
        self.assertNotIn("feel_basis", doc)
        self.assertIsNone(doc["hours"])  # 시간별 값이 아직 없다
        self.assertEqual(list(doc["days"]), ["2026-11-18", "2026-11-20", "2026-11-21", "2026-11-24", "2026-11-25"])
        json.dumps(doc)  # JSON 으로 쓸 수 있는 값만

    def test_days_carry_warnings_and_observations(self):
        days = self.doc()["days"]
        self.assertEqual(days["2026-11-18"]["w"], [["strong_wind", "advisory", "2026-11-18T04:00+09:00", "2026-11-18T15:00+09:00"]])  # 건조는 뺀다
        values = dict(zip(records.VARS, days["2026-11-18"]["o"]))
        self.assertEqual((values["temp_min"], values["gust_max"], values["rain"], values["snow_new"]), (3.1, 21.4, 2.5, 0.0))
        cold = ["cold_wave", "advisory", "2026-11-20T21:00+09:00", "2026-11-21T10:00+09:00"]
        self.assertEqual(days["2026-11-20"], {"w": [cold]})  # 관측이 없는 날: o 키가 없다
        self.assertEqual(days["2026-11-21"]["w"], [cold])
        self.assertEqual(dict(zip(records.VARS, days["2026-11-21"]["o"]))["temp_min"], -6.4)
        self.assertEqual(list(days["2026-11-24"]), ["o"])  # 특보가 없는 날: w 키가 없다
        self.assertEqual(days["2026-11-25"], {"w": [["heavy_snow", "warning", "2026-11-25T06:00+09:00", None]]})  # 아직 해제되지 않음

    def test_days_before_the_warning_history_are_left_out(self):
        self.assertNotIn("2026-11-10", self.doc()["days"])

    def test_region_without_a_station_in_range_has_no_observations(self):
        cfg = dataclasses.replace(CFG, settings=dataclasses.replace(CFG.settings, records={**CFG.settings.records, "max_station_km": 1}))
        doc = self.doc(cfg=cfg)
        self.assertIsNone(doc["station"])
        self.assertIsNone(doc["obs_to"])
        self.assertTrue(all("o" not in day for day in doc["days"].values()))
        self.assertIn("2026-11-18", doc["days"])  # 특보 기록은 그대로

    def test_keep_days_limits_the_window(self):
        cfg = dataclasses.replace(CFG, settings=dataclasses.replace(CFG.settings, records={**CFG.settings.records, "keep_days": 5}))
        self.assertEqual(list(self.doc(cfg=cfg)["days"]), ["2026-11-21", "2026-11-24", "2026-11-25"])  # 25일까지 닷새: 21~25일

    def test_other_regions_and_multi_zone_regions(self):
        sokcho = self.doc("L1022210")
        self.assertEqual(sokcho["station"]["stn"], "90")  # 지역 안의 지점
        self.assertEqual(sokcho["days"], {})
        multi = next(r for r in CFG.regions if len(r.zone_codes) > 1)
        write_events(self.dir, {"20261119": [[code, "호우", "주의보", "1", "202611190100", "202611190200", None] for code in multi.zone_codes]
                                + [[code, "호우", "주의보", "2", "202611190800", None, "202611190900"] for code in multi.zone_codes]})
        doc = self.doc(multi.id)
        self.assertEqual(doc["days"]["2026-11-19"]["w"], [["heavy_rain", "advisory", "2026-11-19T02:00+09:00", "2026-11-19T09:00+09:00"]])  # 구역이 여럿이어도 한 번만

    def test_without_any_history_only_observations_are_listed(self):
        empty = Path(tempfile.mkdtemp())
        obs.save_rows(empty, "258", "보성군", [obs_row(date(2026, 11, 24), temp_min=-1.0, temp_max=5.0, wind_avg=2.0)])
        doc = next(d for d in records.build_all(CFG, empty, STATIONS) if d["id"] == "beolgyo")
        self.assertEqual((doc["from"], doc["to"], doc["obs_to"]), (None, None, "2026-11-24"))
        self.assertEqual(list(doc["days"]), ["2026-11-24"])
        nothing = next(d for d in records.build_all(CFG, Path(tempfile.mkdtemp()), STATIONS) if d["id"] == "beolgyo")
        self.assertEqual((nothing["from"], nothing["obs_to"], nothing["days"]), (None, None, {}))

    def test_real_stored_history_builds_for_every_region(self):
        from teukbo.config import ROOT

        docs = records.build_all(CFG, ROOT / "data")
        self.assertEqual([d["id"] for d in docs], [r.id for r in CFG.regions])
        for doc in docs:
            for day, entry in doc["days"].items():
                last = max(d for d in (doc["to"], doc["obs_to"], (doc["hours"] or {}).get("to")) if d)
                self.assertTrue(doc["from"] <= day <= last, (doc["id"], day))
                for kind, level, start, end in entry.get("w", []):
                    self.assertIn(kind, CFG.thresholds.types)
                    self.assertIn(level, CFG.thresholds.levels)
                    self.assertLessEqual(start[:10], day)
                    self.assertTrue(end is None or end[:10] >= day)


def write_hour(data_dir: Path, t, rows: dict) -> None:
    """data/hourly/날짜/시.json 을 손으로 만든다. rows: 격자 키 → [기온, 습도, 풍속, 강수량, 강수형태]."""
    storage.write_json(hourly.hour_path(data_dir, t), {"schema": 1, "t": t.isoformat(timespec="minutes"), "vars": list(hourly.VARS), "grids": rows})


class HoursTest(unittest.TestCase):
    """시간별 실황 → 지역별 한 달 파일, 그리고 기록에 싣는 그날의 요약."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.out = Path(tempfile.mkdtemp()) / "hours"
        self.region = region("beolgyo")
        self.key = hourly.grid_key((self.region.nx, self.region.ny))

    def fill_day(self, year, month, day, hours=range(24), temp=lambda h: -4.0 + abs(h - 14) * -0.5, rh=60, wind=3.0, rain=0.0, pty=0):
        for h in hours:
            write_hour(self.dir, kst(year, month, day, h), {self.key: [round(temp(h), 1), rh, wind, rain, pty], "999,999": [9.9, 50, 1.0, 0.0, 0]})

    def month(self, name="2026-11", region_id="beolgyo"):
        return json.loads((self.out / region_id / f"{name}.json").read_text(encoding="utf-8"))

    def test_month_file_shape(self):
        self.fill_day(2026, 11, 26)
        summary = records.build_hours(CFG, self.dir, self.out)
        doc = self.month()
        self.assertEqual((doc["schema"], doc["id"], doc["name"], doc["month"], doc["grid"]), (1, "beolgyo", "보성(벌교)", "2026-11", [self.region.nx, self.region.ny]))
        self.assertEqual(doc["vars"], ["temp", "feel", "rain", "wind", "rh", "pty"])
        self.assertEqual(doc["feel_stat"], "min")  # 겨울철 식을 쓰는 달
        self.assertIn("초단기실황", doc["source"])
        self.assertIn("계산", doc["source"])  # 체감온도가 가공값임을 밝힌다
        day = doc["days"]["2026-11-26"]
        self.assertEqual(day["h"], list(range(24)))
        self.assertEqual(len(day["v"]), 24)
        temp, feel, rain, wind, rh, pty = day["v"][14]
        self.assertEqual((temp, rain, wind, rh, pty), (-4.0, 0.0, 3.0, 60, 0))
        self.assertEqual(feel, round(kma_winter_apparent_temp(-4.0, 3.0), 1))
        self.assertEqual(summary["beolgyo"]["2026-11-26"]["h"], 24)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(r.id for r in CFG.regions if hourly.grid_key((r.nx, r.ny)) == self.key))  # 값이 있는 지역만

    def test_feel_follows_the_season_and_needs_its_ingredients(self):
        self.assertEqual(records.hour_feel(-4.0, 60, 3.0, summer=False), round(kma_winter_apparent_temp(-4.0, 3.0), 1))
        self.assertEqual(records.hour_feel(31.0, 70, 3.0, summer=True), round(kma_summer_apparent_temp(31.0, 70), 1))
        self.assertEqual(records.hour_feel(0.2, 60, 1.3, summer=False), 0.2)  # 바람이 약하면 기온 그대로
        self.assertIsNone(records.hour_feel(-4.0, 60, None, summer=False))  # 겨울철 식은 풍속이 있어야
        self.assertIsNone(records.hour_feel(31.0, None, 3.0, summer=True))  # 여름철 식은 습도가 있어야
        self.assertIsNone(records.hour_feel(None, 60, 3.0, summer=False))
        self.fill_day(2027, 7, 20, temp=lambda h: 24.0 + (10 - abs(h - 15)) * 0.8, rh=70)
        records.build_hours(CFG, self.dir, self.out)
        doc = self.month("2027-07")
        self.assertEqual(doc["feel_stat"], "max")  # 여름철 식을 쓰는 달
        temp, feel = doc["days"]["2027-07-20"]["v"][15][:2]
        self.assertEqual(feel, round(kma_summer_apparent_temp(temp, 70), 1))

    def test_day_summary_gives_the_extremes_and_when(self):
        self.fill_day(2026, 11, 26)
        entry = records.build_hours(CFG, self.dir)["beolgyo"]["2026-11-26"]
        feels = [round(kma_winter_apparent_temp(round(-4.0 + abs(h - 14) * -0.5, 1), 3.0), 1) for h in range(24)]
        self.assertEqual(entry, {"h": 24, "f": [min(feels), feels.index(min(feels)), max(feels), feels.index(max(feels))]})
        self.assertEqual((entry["f"][1], entry["f"][3]), (0, 14))  # 가장 추운 시각 00시, 가장 덜 추운 시각 14시
        self.assertFalse((self.out).exists())  # out_dir 를 주지 않으면 파일은 쓰지 않는다

    def test_too_few_hours_give_no_daily_extremes(self):
        need = int(CFG.settings.records["feel_min_hours"])
        self.fill_day(2026, 11, 25, hours=range(24 - need + 1, 24))  # need - 1 시각뿐
        self.fill_day(2026, 11, 26, hours=range(0, need))
        summary = records.build_hours(CFG, self.dir, self.out)["beolgyo"]
        self.assertEqual(summary["2026-11-25"], {"h": need - 1})  # 일부 시각만으로 그날의 최저·최고를 내세우지 않는다
        self.assertEqual(summary["2026-11-26"]["h"], need)
        self.assertIn("f", summary["2026-11-26"])
        self.assertEqual(self.month()["days"]["2026-11-25"]["h"], list(range(24 - need + 1, 24)))  # 시각별 값은 있는 만큼 싣는다

    def test_hours_without_values_are_left_out(self):
        write_hour(self.dir, kst(2026, 11, 26, 3), {self.key: [None, None, None, None, None]})
        write_hour(self.dir, kst(2026, 11, 26, 4), {"1,1": [1.0, 50, 1.0, 0.0, 0]})  # 다른 격자만
        write_hour(self.dir, kst(2026, 11, 26, 5), {self.key: [1.5, None, None, 0.0, 0]})  # 풍속이 없다
        summary = records.build_hours(CFG, self.dir, self.out)
        day = self.month()["days"]["2026-11-26"]
        self.assertEqual(day["h"], [5])
        self.assertEqual(day["v"], [[1.5, None, 0.0, None, None, 0]])  # 체감온도는 재료가 없어 null
        self.assertEqual(summary["beolgyo"]["2026-11-26"], {"h": 1})
        self.assertEqual(records.build_hours(CFG, Path(tempfile.mkdtemp()), self.out), {})  # 시간별 값이 전혀 없으면 아무것도 만들지 않는다

    def test_regions_sharing_a_grid_each_get_their_own_file(self):
        by_grid: dict = {}
        for r in CFG.regions:
            by_grid.setdefault((r.nx, r.ny), []).append(r.id)
        grid, ids = next((g, v) for g, v in by_grid.items() if len(v) > 1)
        write_hour(self.dir, kst(2026, 11, 26, 6), {hourly.grid_key(grid): [1.0, 50, 2.0, 0.0, 0]})
        summary = records.build_hours(CFG, self.dir, self.out)
        self.assertEqual(sorted(summary), sorted(ids))
        for region_id in ids:
            self.assertEqual(self.month(region_id=region_id)["days"]["2026-11-26"]["h"], [6])

    def test_keep_days_limits_the_months_and_days(self):
        self.fill_day(2026, 9, 30, hours=[12])
        self.fill_day(2026, 11, 25, hours=[12])
        self.fill_day(2026, 11, 26, hours=[12])
        cfg = dataclasses.replace(CFG, settings=dataclasses.replace(CFG.settings, records={**CFG.settings.records, "keep_days": 2}))
        summary = records.build_hours(cfg, self.dir, self.out)
        self.assertEqual(sorted(summary["beolgyo"]), ["2026-11-25", "2026-11-26"])
        self.assertFalse((self.out / "beolgyo" / "2026-09.json").exists())
        self.assertEqual(sorted(hourly.stored_days(self.dir)), ["2026-09-30", "2026-11-25", "2026-11-26"])  # 원자료는 지우지 않는다

    def test_record_carries_hour_counts_extremes_and_span(self):
        write_events(self.dir, {"20261120": [], "20261126": []})
        self.fill_day(2026, 11, 25, hours=range(8, 24))
        self.fill_day(2026, 11, 26)
        obs.save_rows(self.dir, "258", "보성군", [obs_row(date(2026, 11, 25), temp_min=-1.0, temp_max=5.0)])
        docs = {d["id"]: d for d in records.build_all(CFG, self.dir, STATIONS)}
        doc = docs["beolgyo"]
        self.assertEqual(doc["hours"], {"from": "2026-11-25", "to": "2026-11-26", "source": records.HOURS_SOURCE})  # 출처 글은 화면이 그대로 적는다
        self.assertEqual(sorted(doc["days"]["2026-11-25"]), ["h", "o"])  # 16시각뿐이라 그날의 체감 최저·최고는 없다
        self.assertEqual(doc["days"]["2026-11-25"]["h"], 16)
        self.assertEqual(sorted(doc["days"]["2026-11-26"]), ["f", "h"])  # 관측소 일자료가 아직 없어도 시간별 값만으로 날을 싣는다
        self.assertEqual(len(doc["days"]["2026-11-26"]["f"]), 4)
        self.assertIsNone(docs["L1022210"]["hours"])  # 시간별 값이 없는 지역
        json.dumps(doc)

    def test_site_writes_month_files_next_to_the_records(self):
        (self.dir / "latest.json").write_text("{}", encoding="utf-8")
        write_events(self.dir, {"20261126": []})
        self.fill_day(2026, 11, 26)
        out = Path(tempfile.mkdtemp()) / "_site"
        site.build_site(self.dir, out)
        self.assertTrue((out / "data" / "hours" / "beolgyo" / "2026-11.json").is_file())
        self.assertEqual(json.loads((out / "data" / "records" / "beolgyo.json").read_text(encoding="utf-8"))["days"]["2026-11-26"]["h"], 24)
        self.assertFalse((self.dir / "hours").exists())  # 배포 전용: 자료 폴더에는 만들지 않는다

    def test_records_survive_a_failure_in_the_hourly_part(self):
        write_events(self.dir, {"20261118": [[ZONE, "강풍", "주의보", "1", "202611180300", "202611180400", None]]})
        self.fill_day(2026, 11, 26)
        out = Path(tempfile.mkdtemp()) / "records"
        with mock.patch.object(records, "build_hours", side_effect=RuntimeError("시간별 오류")):
            self.assertEqual(records.write_site_records(CFG, self.dir, out, STATIONS, hours_dir=self.out), len(CFG.regions))
        doc = json.loads((out / "beolgyo.json").read_text(encoding="utf-8"))
        self.assertIn("2026-11-18", doc["days"])  # 특보 기록은 그대로
        self.assertIsNone(doc["hours"])

    def test_one_broken_day_does_not_drop_the_other_days(self):
        self.fill_day(2026, 11, 25)
        self.fill_day(2026, 11, 26)
        storage.write_json(hourly.hour_path(self.dir, kst(2026, 11, 25, 3)), {"schema": 1, "t": "x", "vars": list(hourly.VARS), "grids": {self.key: 7}})  # 값이 목록이 아니다
        summary = records.build_hours(CFG, self.dir, self.out)["beolgyo"]
        self.assertEqual(list(summary), ["2026-11-26"])  # 어긋난 날만 빠진다
        self.assertEqual(list(self.month()["days"]), ["2026-11-26"])

    def test_failed_hourly_part_leaves_no_month_files_behind(self):
        write_events(self.dir, {"20261126": []})
        self.fill_day(2026, 11, 26)
        out = Path(tempfile.mkdtemp()) / "records"
        real = records.build_hours

        def half_done(cfg, data_dir, out_dir=None):
            real(cfg, data_dir, out_dir)
            raise RuntimeError("쓰다가 오류")

        with mock.patch.object(records, "build_hours", half_done):
            records.write_site_records(CFG, self.dir, out, STATIONS, hours_dir=self.out)
        self.assertFalse(self.out.exists())  # 요약 없이 남은 달 파일은 치운다
        self.assertTrue((out / "beolgyo.json").is_file())

    def test_local_mode_serves_month_files_only(self):
        from teukbo import local

        web, data = Path("/srv/web"), Path("/srv/data")
        self.assertEqual(local.resolve_path("/data/hours/beolgyo/2026-11.json", web, data), data / "hours" / "beolgyo" / "2026-11.json")
        self.assertIsNone(local.resolve_path("/data/hours/beolgyo/../../latest.json", web, data))
        self.assertIsNone(local.resolve_path("/data/hours/beolgyo/2026-11.txt", web, data))
        self.assertIsNone(local.resolve_path("/data/hourly/2026-11-26/07.json", web, data))  # 원자료는 내보내지 않는다


class SiteTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "latest.json").write_text("{}", encoding="utf-8")
        write_events(self.dir, {"20261118": [[ZONE, "강풍", "주의보", "1", "202611180300", "202611180400", None]]})
        self.out = Path(tempfile.mkdtemp()) / "_site"

    def test_site_carries_a_record_file_per_region(self):
        site.build_site(self.dir, self.out)
        files = sorted(p.stem for p in (self.out / "data" / "records").glob("*.json"))
        self.assertEqual(files, sorted(r.id for r in CFG.regions))
        doc = json.loads((self.out / "data" / "records" / "beolgyo.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["days"]["2026-11-18"]["w"][0][:2], ["strong_wind", "advisory"])
        self.assertFalse((self.dir / "records").exists())  # 배포 전용: 자료 폴더에는 만들지 않는다

    def test_site_survives_a_records_failure(self):
        with mock.patch.object(records, "write_site_records", side_effect=RuntimeError("기록 오류")):
            build = site.build_site(self.dir, self.out)
        self.assertEqual(len(build), 10)
        self.assertTrue((self.out / "index.html").is_file())
        self.assertFalse((self.out / "data" / "records").exists())

    def test_work_rules_are_copied_only_when_present(self):
        rules = Path(tempfile.mkdtemp()) / "work_rules.json"
        with mock.patch.object(site, "WORK_RULES_FILE", rules):
            site.build_site(self.dir, self.out)
            self.assertFalse((self.out / "data" / "work_rules.json").exists())
            rules.write_text('{"schema": 1}', encoding="utf-8")
            site.build_site(self.dir, self.out)
            self.assertEqual(json.loads((self.out / "data" / "work_rules.json").read_text(encoding="utf-8")), {"schema": 1})

    def test_deploy_only_outputs_are_ignored_by_git(self):
        from teukbo.config import ROOT

        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in ("/data/records/", "/data/hours/", "/data/work_rules.json"):
            self.assertIn(line, ignore)
        self.assertNotIn("/data/obs", ignore)  # 관측 일자료는 커밋해서 쌓는다


if __name__ == "__main__":
    unittest.main()
