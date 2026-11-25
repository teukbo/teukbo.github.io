"""관측 일자료 저장(teukbo/obs.py): 언제 무엇을 요청하는지, 어떻게 쌓는지, 실패해도 수집을 막지 않는지. 네트워크는 쓰지 않는다."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from teukbo import obs
from teukbo.config import ROOT
from teukbo.http import FetchError
from teukbo.sources import asos

from .helpers import config, kst

CFG = config()
STATIONS = [{"stn": "258", "name": "보성군", "region": "boseong", "lon": 127.21, "lat": 34.76},
            {"stn": "174", "name": "순천", "region": "suncheon", "lon": 127.37, "lat": 35.02},
            {"stn": "90", "name": "속초", "region": "L1022210", "lon": 128.56, "lat": 38.25}]
KEY = {"DATA_GO_KR_SERVICE_KEY": "test-key"}


def row(day: date, stn: str = "258", **values):
    base = {name: None for name in asos.DAILY_FIELDS.values()}
    base.update({"temp_min": -3.2, "temp_max": 6.4, "wind_avg": 2.1, "wind_max": 5.5, "gust_max": 9.9, "rh_avg": 55.0, "rh_min": 31.0})
    base.update(values)
    return {"d": day, "stn": stn, "name": "", **base}


class FakeAsos:
    """asos.fetch_daily 대신: 요청을 적어 두고, 요청한 기간의 날마다 한 행씩 돌려준다(available 까지만)."""

    def __init__(self, available: date, fail: set[str] | None = None):
        self.available, self.fail, self.calls = available, fail or set(), []

    def __call__(self, key, station, start, end, http_opts=None):
        self.calls.append((station, start, end))
        if station in self.fail:
            raise FetchError(f"ASOS 일자료 {station}: HTTP 403 등록되지 않은 서비스키")
        out, day = [], start
        while day <= min(end, self.available):
            out.append(row(day, station))
            day += timedelta(days=1)
        return out


class TargetDayTest(unittest.TestCase):
    def test_yesterday_only_after_the_data_is_published(self):
        self.assertEqual(obs.target_end(kst(2026, 11, 25, 10, 59), 11), date(2026, 11, 23))  # 11시 전: 그제까지
        self.assertEqual(obs.target_end(kst(2026, 11, 25, 11, 0), 11), date(2026, 11, 24))
        self.assertEqual(obs.target_end(kst(2026, 11, 25, 23, 59), 11), date(2026, 11, 24))


class ObsRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_obs(self, now, fake, stations=STATIONS, env=KEY):
        with mock.patch.object(asos, "fetch_daily", fake):
            return obs.run(CFG, now, self.dir, env, stations)

    def test_first_run_fills_from_since_and_splits_by_month(self):
        fake = FakeAsos(date(2026, 11, 24))
        self.assertEqual(self.run_obs(kst(2026, 11, 25, 11, 25), fake), 0)
        since = date.fromisoformat(CFG.settings.records["since"])
        self.assertEqual(fake.calls, [(s["stn"], since, date(2026, 11, 24)) for s in STATIONS])  # 지점마다 한 번
        self.assertEqual(sorted(p.name for p in obs.obs_dir(self.dir).glob("*.json")), ["2026-09.json", "2026-10.json", "2026-11.json"])
        doc = json.loads((obs.obs_dir(self.dir) / "2026-11.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["schema"], doc["month"], doc["vars"]), (1, "2026-11", list(asos.DAILY_FIELDS.values())))
        self.assertEqual(list(doc["stations"]), ["90", "174", "258"])
        station = doc["stations"]["258"]
        self.assertEqual(station["name"], "보성군")  # 이름은 설정의 것을 쓴다
        self.assertEqual((min(station["days"]), max(station["days"]), len(station["days"])), ("2026-11-01", "2026-11-24", 24))
        values = dict(zip(doc["vars"], station["days"]["2026-11-24"]))
        self.assertEqual((values["temp_min"], values["rh_min"], values["wind_avg"], values["rain"]), (-3.2, 31.0, 2.1, None))
        self.assertNotIn("test-key", (obs.obs_dir(self.dir) / "2026-11.json").read_text(encoding="utf-8"))

    def test_nothing_to_fetch_means_no_calls(self):
        self.run_obs(kst(2026, 11, 25, 11, 25), FakeAsos(date(2026, 11, 24)))
        again = FakeAsos(date(2026, 11, 24))
        self.run_obs(kst(2026, 11, 25, 14, 25), again)
        self.assertEqual(again.calls, [])
        early = FakeAsos(date(2026, 11, 25))
        self.run_obs(kst(2026, 11, 26, 8, 25), early)  # 다음 날 11시 전: 어제(25일) 것은 아직 요청하지 않는다
        self.assertEqual(early.calls, [])

    def test_next_day_continues_from_the_last_stored_day(self):
        self.run_obs(kst(2026, 11, 25, 11, 25), FakeAsos(date(2026, 11, 24)))
        fake = FakeAsos(date(2026, 11, 25))
        self.run_obs(kst(2026, 11, 26, 11, 25), fake)
        self.assertEqual(fake.calls, [(s["stn"], date(2026, 11, 25), date(2026, 11, 25)) for s in STATIONS])
        self.assertIn("2026-11-25", obs.load_obs(self.dir)["174"])

    def test_late_data_is_requested_again_next_time(self):
        # 어제 것이 아직 안 올라왔으면(그제까지만 옴) 받은 만큼만 쌓고, 다음 실행이 어제부터 다시 요청한다
        self.run_obs(kst(2026, 11, 25, 11, 25), FakeAsos(date(2026, 11, 23)))
        self.assertEqual(max(obs.load_obs(self.dir)["258"]), "2026-11-23")
        fake = FakeAsos(date(2026, 11, 24))
        self.run_obs(kst(2026, 11, 25, 14, 25), fake)
        self.assertEqual(fake.calls[0], ("258", date(2026, 11, 24), date(2026, 11, 24)))
        self.assertEqual(max(obs.load_obs(self.dir)["258"]), "2026-11-24")

    def test_without_a_key_nothing_is_called(self):
        fake = FakeAsos(date(2026, 11, 24))
        self.assertEqual(self.run_obs(kst(2026, 11, 25, 11, 25), fake, env={}), 0)
        self.assertEqual(fake.calls, [])
        self.assertFalse(obs.obs_dir(self.dir).exists())

    def test_one_failed_station_does_not_lose_the_others(self):
        fake = FakeAsos(date(2026, 11, 24), fail={"174"})
        self.assertEqual(self.run_obs(kst(2026, 11, 25, 11, 25), fake), 0)
        have = obs.load_obs(self.dir)
        self.assertEqual(set(have), {"258", "90"})
        retry = FakeAsos(date(2026, 11, 24))
        self.run_obs(kst(2026, 11, 25, 14, 25), retry)
        self.assertEqual([c[0] for c in retry.calls], ["174"])  # 못 받은 지점만 다시

    def test_gives_up_for_this_run_after_repeated_failures(self):
        many = [{"stn": str(100 + i), "name": f"지점{i}"} for i in range(10)]
        fake = FakeAsos(date(2026, 11, 24), fail={s["stn"] for s in many})
        self.assertEqual(self.run_obs(kst(2026, 11, 25, 11, 25), fake, stations=many), 0)  # 활용신청이 안 된 키: 몇 번만 해 보고 그만둔다
        self.assertEqual(len(fake.calls), obs.STOP_AFTER_FAILURES)

    def test_time_budget_stops_early_and_keeps_what_it_got(self):
        fake = FakeAsos(date(2026, 11, 24))
        ticks = iter([0.0, 0.0, 1.0, 10_000.0, 10_000.0, 10_000.0])
        with mock.patch.object(obs.time, "monotonic", lambda: next(ticks)):
            self.run_obs(kst(2026, 11, 25, 11, 25), fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(set(obs.load_obs(self.dir)), {"258", "174"})

    def test_rows_without_any_value_do_not_count_as_stored(self):
        def empty(key, station, start, end, http_opts=None):
            return [{"d": start, "stn": station, "name": "", **{name: None for name in asos.DAILY_FIELDS.values()}}]
        self.run_obs(kst(2026, 11, 25, 11, 25), empty, stations=STATIONS[:1])
        self.assertEqual(obs.load_obs(self.dir), {})

    def test_older_files_with_fewer_variables_are_read_and_upgraded(self):
        old_vars = ["rain", "temp_min", "temp_max"]
        path = obs.obs_dir(self.dir) / "2026-11.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 1, "month": "2026-11", "vars": old_vars, "stations": {"258": {"name": "보성군", "days": {"2026-11-23": [1.5, -2.0, 7.0]}}}}), encoding="utf-8")
        self.assertEqual(obs.load_obs(self.dir)["258"]["2026-11-23"], {"rain": 1.5, "temp_min": -2.0, "temp_max": 7.0})
        self.run_obs(kst(2026, 11, 25, 11, 25), FakeAsos(date(2026, 11, 24)), stations=STATIONS[:1])
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["vars"], list(asos.DAILY_FIELDS.values()))
        have = obs.load_obs(self.dir)["258"]
        self.assertEqual((have["2026-11-23"]["rain"], have["2026-11-23"]["temp_min"], have["2026-11-23"]["wind_avg"]), (1.5, -2.0, None))
        self.assertEqual(have["2026-11-24"]["wind_avg"], 2.1)

    def test_main_never_fails_the_workflow(self):
        with mock.patch.object(obs, "run", side_effect=RuntimeError("뜻밖의 오류")) as run:
            self.assertEqual(obs.main(["--data-dir", str(self.dir)]), 0)
        self.assertEqual(run.call_count, 1)


class ObsWiringTest(unittest.TestCase):
    def test_daily_fields_cover_what_the_records_need(self):
        for raw, name in (("minTa", "temp_min"), ("maxTa", "temp_max"), ("sumRn", "rain"), ("ddMefs", "snow_new"), ("maxWs", "wind_max"),
                          ("maxInsWs", "gust_max"), ("minRhm", "rh_min"), ("avgWs", "wind_avg")):
            self.assertEqual(asos.DAILY_FIELDS[raw], name)

    def test_station_list_is_the_configured_one(self):
        stations = obs.load_stations()
        self.assertGreater(len(stations), 80)
        self.assertTrue(all(s.get("lat") is not None and s.get("lon") is not None for s in stations))

    def test_collect_workflow_runs_it_without_blocking_the_deploy(self):
        text = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        at = text.index("python3 -m teukbo.obs")
        step = text[text.rindex("- name:", 0, at):at]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("timeout-minutes: 5", step)
        self.assertIn("steps.due.outputs.run == 'true'", step)
        self.assertIn("DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATA_GO_KR_SERVICE_KEY }}", step)
        self.assertLess(text.index("python3 -m teukbo.rail_run"), at)
        self.assertLess(at, text.index("git add data/history"))
        self.assertIn("git add data/obs", text)
        self.assertLess(text.index("git add data/obs"), text.index("python3 -m teukbo.site"))  # 쌓은 관측값으로 기록을 만든다

    def test_settings_document_the_records_block(self):
        raw = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))["records"]
        self.assertTrue(raw["_doc"])
        for key in ("since", "obs_ready_hour", "obs_time_budget_seconds", "max_station_km", "keep_days"):
            self.assertIn(key, raw)
            self.assertTrue(any(key in line for line in raw["_doc"]), key)


if __name__ == "__main__":
    unittest.main()
