"""시간별 실황 수집(teukbo/hourly.py): 무엇을 어떤 순서로 받고, 어떻게 쌓고, 언제 멈추는지. 네트워크는 쓰지 않는다."""

import dataclasses
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from teukbo import hourly, storage
from teukbo.http import FetchError
from teukbo.sources import datagokr, kma_ultra

from .helpers import config, kst

FULL = config()
# 격자 6곳이면 충분하다. 가장 최근 시각을 미루는 설정(skip_latest_hours)은 꺼 두고 따로 확인한다 — 아래 검사들이 '07시까지 받는다'로 읽히게
CFG = dataclasses.replace(FULL, regions=FULL.regions[:6], settings=dataclasses.replace(FULL.settings, hourly={**FULL.settings.hourly, "skip_latest_hours": 0}))
GRIDS = sorted({(r.nx, r.ny) for r in CFG.regions})
KEYS = [hourly.grid_key(g) for g in GRIDS]
ENV = {"DATA_GO_KR_SERVICE_KEY": "test-key"}
NOW = kst(2026, 11, 26, 7, 41)  # 가장 최근 기준시각은 07:00
LATEST = kst(2026, 11, 26, 7)


def cfg_with(**hourly_opts):
    return dataclasses.replace(CFG, settings=dataclasses.replace(CFG.settings, hourly={**CFG.settings.hourly, **hourly_opts}))


class FakeNcst:
    """조회 함수 대신: 부른 것을 적어 두고 값을 돌려준다. behave(기준시각, 격자) 가 예외를 던지거나 값을 바꿀 수 있다."""

    def __init__(self, behave=None):
        self.calls, self.behave = [], behave

    def __call__(self, base, grid):
        self.calls.append((base, grid))
        if self.behave is not None:
            out = self.behave(base, grid)
            if out is not None:
                return out
        return {"temp": -1.234 + grid[0] / 100, "rain": 0.0, "wind": 2.84, "wdir": 184, "rh": 80.6, "pty": 0}


class PlanTest(unittest.TestCase):
    def test_latest_base_waits_for_the_publication_delay(self):
        self.assertEqual(hourly.wanted_hours(NOW, 23)[-1], LATEST)
        self.assertEqual(hourly.wanted_hours(kst(2026, 11, 26, 7, 5), 23)[-1], kst(2026, 11, 26, 6))  # 07:10 전에는 07시 것이 아직 없다

    def test_hours_run_oldest_first_and_stay_inside_one_day(self):
        hours = hourly.wanted_hours(NOW, 23)
        self.assertEqual(len(hours), 24)
        self.assertEqual(hours[0], LATEST - timedelta(hours=23))  # 23시간 전까지(24시간 전은 조회되지 않는다)
        self.assertEqual(hours, sorted(hours))
        self.assertEqual(hourly.wanted_hours(NOW, 0), [LATEST])

    def test_the_latest_hour_waits_for_the_next_run_by_default(self):
        # 진행 중인 시각의 값이 뒤에 바뀌는지 기상청 문서에 없다 — 한 번 받은 파일은 다시 쓰지 않으므로 값이 굳은 뒤에 받는다
        self.assertEqual(FULL.settings.hourly["skip_latest_hours"], 1)
        hours = hourly.wanted_hours(NOW, 23, 1)
        self.assertEqual((hours[0], hours[-1], len(hours)), (LATEST - timedelta(hours=23), LATEST - timedelta(hours=1), 23))
        fetch = FakeNcst()
        hourly.run(dataclasses.replace(CFG, settings=FULL.settings), NOW, Path(tempfile.mkdtemp()), ENV, fetch)
        self.assertNotIn(LATEST, {base for base, _ in fetch.calls})
        later = hourly.wanted_hours(NOW + timedelta(hours=3), 23, 1)
        self.assertIn(LATEST, later)  # 3시간 뒤 실행이 받는다(조회 가능 기간 안)

    def test_settings_refuse_a_window_the_api_does_not_serve(self):
        from teukbo.config import ConfigError, load_settings

        raw = json.loads(json.dumps(FULL.settings.raw))
        raw["hourly"]["max_back_hours"] = 24
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_settings(path)
        raw["hourly"]["max_back_hours"] = 23
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(load_settings(path).hourly["max_back_hours"], 23)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_once(self, fetch, cfg=CFG, now=NOW, env=ENV):
        return hourly.run(cfg, now, self.dir, env, fetch)

    def test_first_run_fills_every_hour_oldest_first(self):
        fetch = FakeNcst()
        self.assertEqual(self.run_once(fetch), 0)
        self.assertEqual(len(fetch.calls), 24 * len(GRIDS))
        self.assertEqual([base for base, _ in fetch.calls][:: len(GRIDS)], hourly.wanted_hours(NOW, 23))  # 한 시각을 끝내고 다음 시각으로
        self.assertEqual(hourly.stored_days(self.dir), ["2026-11-25", "2026-11-26"])
        self.assertEqual(sorted(hourly.load_day(self.dir, "2026-11-25")), list(range(8, 24)))  # 25일 08시부터
        self.assertEqual(sorted(hourly.load_day(self.dir, "2026-11-26")), list(range(0, 8)))

    def test_file_shape_and_rounding(self):
        self.run_once(FakeNcst())
        doc = json.loads(hourly.hour_path(self.dir, LATEST).read_text(encoding="utf-8"))
        self.assertEqual(sorted(doc), ["grids", "schema", "t", "vars"])  # 다 받았으면 missing·tries 가 없다
        self.assertEqual((doc["schema"], doc["t"], doc["vars"]), (1, "2026-11-26T07:00+09:00", ["temp", "rh", "wind", "rain", "pty"]))
        self.assertEqual(list(doc["grids"]), KEYS)
        temp, rh, wind, rain, pty = doc["grids"][KEYS[0]]
        self.assertEqual((temp, rh, wind, rain, pty), (round(-1.234 + GRIDS[0][0] / 100, 1), 81, 2.8, 0.0, 0))
        self.assertIsInstance(rh, int)
        self.assertIsInstance(pty, int)
        self.assertNotIn("wdir", json.dumps(doc))  # 풍향은 쌓지 않는다
        self.assertEqual(hourly.hour_path(self.dir, LATEST), self.dir / "hourly" / "2026-11-26" / "07.json")

    def test_complete_hours_are_never_fetched_or_rewritten_again(self):
        self.run_once(FakeNcst())
        path = hourly.hour_path(self.dir, LATEST)
        before = path.stat().st_mtime_ns
        again = FakeNcst()
        self.run_once(again)
        self.assertEqual(again.calls, [])
        self.assertEqual(path.stat().st_mtime_ns, before)
        later = FakeNcst()
        self.run_once(later, now=NOW + timedelta(hours=3))  # 3시간 뒤: 새 정시 셋만
        self.assertEqual(sorted({base for base, _ in later.calls}), [LATEST + timedelta(hours=k) for k in (1, 2, 3)])

    def test_partial_hour_keeps_what_came_and_refills_only_the_missing(self):
        hole = GRIDS[2]
        first = FakeNcst(lambda base, grid: (_ for _ in ()).throw(datagokr.NoData("자료 없음")) if grid == hole and base == LATEST else None)
        self.run_once(first)
        doc = hourly.load_hour(hourly.hour_path(self.dir, LATEST))
        self.assertEqual((doc["missing"], doc["tries"]), ([hourly.grid_key(hole)], 0))
        self.assertEqual(len(doc["grids"]), len(GRIDS) - 1)
        second = FakeNcst()
        self.run_once(second)
        self.assertEqual(second.calls, [(LATEST, hole)])  # 못 받은 격자만 다시
        doc = hourly.load_hour(hourly.hour_path(self.dir, LATEST))
        self.assertNotIn("missing", doc)
        self.assertNotIn("tries", doc)
        self.assertEqual(list(doc["grids"]), KEYS)  # 격자 순서는 늘 같다

    def test_a_grid_that_never_answers_is_not_asked_forever(self):
        hole = GRIDS[0]
        never = lambda base, grid: (_ for _ in ()).throw(datagokr.NoData("자료 없음")) if grid == hole else None  # noqa: E731
        asked = []
        for _ in range(5):
            fetch = FakeNcst(never)
            self.run_once(fetch)
            asked.append(sum(1 for base, grid in fetch.calls if grid == hole and base == LATEST))
        self.assertEqual(asked, [1, 1, 1, 0, 0])  # 처음 한 번 + refill_tries(2)번
        self.assertEqual(hourly.load_hour(hourly.hour_path(self.dir, LATEST))["tries"], 2)

    def test_runs_cut_short_do_not_use_up_the_refill_tries(self):
        # 거절·연속 실패·하루 상한으로 끊긴 실행이 '채워 본 횟수'를 깎아 먹으면, 서버가 멀쩡해진 뒤에도 그 시각을 영영 채우지 않게 된다
        half = set(GRIDS[:3])
        one_hour = cfg_with(max_back_hours=0, workers=1)
        hourly.run(one_hour, NOW, self.dir, ENV, FakeNcst(lambda base, grid: None if grid in half else (_ for _ in ()).throw(datagokr.NoData("자료 없음"))))
        path = hourly.hour_path(self.dir, LATEST)
        self.assertEqual((len(hourly.load_hour(path)["missing"]), hourly.load_hour(path)["tries"]), (3, 0))
        before = path.stat().st_mtime_ns
        for _ in range(4):  # 한 번 부르자마자 거절되는 실행이 이어진다
            hourly.run(one_hour, NOW, self.dir, ENV, FakeNcst(lambda base, grid: (_ for _ in ()).throw(datagokr.Rejected("오류 코드 22"))))
        for _ in range(4):  # 연달아 실패해 멈추는 실행
            hourly.run(cfg_with(max_back_hours=0, workers=1, stop_after_failures=2), NOW, self.dir, ENV, FakeNcst(lambda base, grid: (_ for _ in ()).throw(FetchError("TimeoutError"))))
        self.assertEqual(hourly.load_hour(path)["tries"], 0)
        self.assertEqual(path.stat().st_mtime_ns, before)  # 달라진 것이 없으면 파일도 다시 쓰지 않는다
        capped = cfg_with(max_back_hours=0, workers=1, daily_call_cap=hourly._calls_today(self.dir, "2026-11-26") + 1)
        hourly.run(capped, NOW, self.dir, ENV, FakeNcst())  # 상한 때문에 한 곳만 묻는 실행: 값은 늘지만 횟수는 그대로
        self.assertEqual((len(hourly.load_hour(path)["missing"]), hourly.load_hour(path)["tries"]), (2, 0))
        healthy = FakeNcst()
        hourly.run(cfg_with(max_back_hours=0, workers=1, daily_call_cap=10**6), NOW, self.dir, ENV, healthy)
        self.assertEqual(len(healthy.calls), 2)  # 서버가 멀쩡해지면 남은 곳을 마저 받는다
        self.assertNotIn("missing", hourly.load_hour(path))

    def test_values_all_missing_count_as_no_data(self):
        self.run_once(FakeNcst(lambda base, grid: {"temp": None, "rain": None, "wind": None, "wdir": None, "rh": None, "pty": None} if grid == GRIDS[1] else None))
        doc = hourly.load_hour(hourly.hour_path(self.dir, LATEST))
        self.assertEqual(doc["missing"], [hourly.grid_key(GRIDS[1])])
        self.assertIsNone(hourly.pack(None))
        self.assertEqual(hourly.pack({"temp": 1.26, "rh": None, "wind": None, "rain": None, "pty": None}), [1.3, None, None, None, None])  # 일부만 있어도 싣는다

    def test_out_of_range_hour_is_skipped_and_the_rest_still_come(self):
        oldest = LATEST - timedelta(hours=23)
        fetch = FakeNcst(lambda base, grid: (_ for _ in ()).throw(datagokr.OutOfRange("오류 코드 10 최근 1일 간의 자료만 제공합니다.")) if base == oldest else None)
        self.assertEqual(hourly.run(cfg_with(workers=1), NOW, self.dir, ENV, fetch), 0)
        self.assertFalse(hourly.hour_path(self.dir, oldest).exists())
        self.assertEqual(sum(1 for base, _ in fetch.calls if base == oldest), 1)  # 첫 응답을 보고 그 시각은 그만둔다
        self.assertTrue(hourly.hour_path(self.dir, LATEST).exists())

    def test_rejection_stops_the_whole_run(self):
        for error in (FetchError("시간별 실황: HTTP 403 등록되지 않은 서비스키", status=403), datagokr.Rejected("오류 코드 22 LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")):
            with self.subTest(error=type(error).__name__):
                self.dir = Path(tempfile.mkdtemp())
                fetch = FakeNcst(lambda base, grid, error=error: (_ for _ in ()).throw(error))
                with mock.patch.object(hourly, "log") as log:  # 테스트에서는 로그를 꺼 두므로(tests/__init__.py) 부른 것을 직접 본다
                    self.assertEqual(hourly.run(cfg_with(workers=1), NOW, self.dir, ENV, fetch), 0)
                self.assertEqual(len(fetch.calls), 1)  # 한 번 거절되면 더 부르지 않는다
                self.assertEqual(hourly.stored_days(self.dir), [])
                self.assertIn("거절", str(log.error.call_args))
                self.assertNotIn(ENV["DATA_GO_KR_SERVICE_KEY"], str(log.mock_calls))  # 인증키는 로그에 남지 않는다

    def test_failure_streak_stops_the_run(self):
        fetch = FakeNcst(lambda base, grid: (_ for _ in ()).throw(FetchError("시간별 실황: TimeoutError")))
        with mock.patch.object(hourly, "log") as log:
            self.assertEqual(hourly.run(cfg_with(workers=1, stop_after_failures=4), NOW, self.dir, ENV, fetch), 0)
        self.assertEqual(len(fetch.calls), 4)
        self.assertIn("연달아", str(log.error.call_args))

    def test_unexpected_errors_do_not_escape(self):
        fetch = FakeNcst(lambda base, grid: (_ for _ in ()).throw(RuntimeError("뜻밖의 오류")))
        with mock.patch.object(hourly, "log") as log:
            self.assertEqual(hourly.run(cfg_with(workers=1, stop_after_failures=3), NOW, self.dir, ENV, fetch), 0)
        self.assertEqual(log.exception.call_count, 3)
        with mock.patch.object(hourly, "run", side_effect=RuntimeError("boom")), mock.patch.object(hourly, "log") as log:
            self.assertEqual(hourly.main(["--data-dir", str(self.dir)]), 0)  # 어떤 경우에도 수집·배포를 막지 않는다
        log.exception.assert_called_once()

    def test_time_budget_saves_what_it_has_and_resumes_next_time(self):
        clock = [0.0]
        fetch = FakeNcst(lambda base, grid: clock.__setitem__(0, clock[0] + 1.0))  # 한 번 부를 때마다 1초가 흐른 것으로
        with mock.patch.object(hourly.time, "monotonic", lambda: clock[0]):
            hourly.run(cfg_with(workers=1, time_budget_seconds=len(GRIDS) + 2.5), NOW, self.dir, ENV, fetch)
        oldest = LATEST - timedelta(hours=23)
        self.assertNotIn("missing", hourly.load_hour(hourly.hour_path(self.dir, oldest)))  # 첫 시각은 다 받았다
        second = hourly.load_hour(hourly.hour_path(self.dir, oldest + timedelta(hours=1)))
        self.assertEqual(len(second["grids"]), 3)  # 둘째 시각은 받은 데까지만
        self.assertEqual(len(second["missing"]), len(GRIDS) - 3)
        self.assertFalse(hourly.hour_path(self.dir, LATEST).exists())
        rest = FakeNcst()
        self.run_once(rest)
        self.assertEqual(len(rest.calls), 24 * len(GRIDS) - len(GRIDS) - 3)  # 다음 실행이 나머지를 잇는다

    def test_daily_call_cap_is_shared_across_runs_of_the_same_day(self):
        capped = cfg_with(daily_call_cap=len(GRIDS) + 2)
        first = FakeNcst()
        hourly.run(capped, NOW, self.dir, ENV, first)
        self.assertEqual(len(first.calls), len(GRIDS) + 2)
        self.assertEqual(json.loads((self.dir / "hourly" / "calls.json").read_text(encoding="utf-8")), {"date": "2026-11-26", "calls": len(GRIDS) + 2})
        second = FakeNcst()
        hourly.run(capped, NOW + timedelta(hours=3), self.dir, ENV, second)
        self.assertEqual(second.calls, [])  # 같은 날에는 더 부르지 않는다
        tomorrow = FakeNcst()
        hourly.run(capped, NOW + timedelta(hours=20), self.dir, ENV, tomorrow)  # 날이 바뀌면 다시 센다
        self.assertEqual(len(tomorrow.calls), len(GRIDS) + 2)

    def test_no_key_or_disabled_means_no_calls(self):
        fetch = FakeNcst()
        with mock.patch.object(hourly, "make_fetch", wraps=hourly.make_fetch) as made:
            self.assertEqual(hourly.run(CFG, NOW, self.dir, {}, None), 0)
            made.assert_called_once()
        self.assertIsNone(hourly.make_fetch({}, CFG.settings.hourly))
        self.assertIsNone(hourly.make_fetch({"DATA_GO_KR_SERVICE_KEY": "  "}, CFG.settings.hourly))
        self.assertEqual(hourly.run(cfg_with(enabled=False), NOW, self.dir, ENV, fetch), 0)
        self.assertEqual(fetch.calls, [])
        self.assertEqual(hourly.stored_days(self.dir), [])

    def test_broken_files_are_fetched_again_and_readers_skip_them(self):
        path = hourly.hour_path(self.dir, LATEST)
        path.parent.mkdir(parents=True)
        path.write_text('{"schema": 1, "grids"', encoding="utf-8")  # 쓰다 만 파일
        self.assertIsNone(hourly.load_hour(path))
        self.assertEqual(hourly.load_day(self.dir, "2026-11-26"), {})
        storage.write_json(path, {"schema": 1, "t": "x", "vars": ["temp"], "grids": {}})  # 꼴이 다른 파일
        self.assertIsNone(hourly.load_hour(path))
        self.run_once(FakeNcst(), cfg=cfg_with(max_back_hours=0))
        self.assertEqual(list(hourly.load_day(self.dir, "2026-11-26")), [7])
        (self.dir / "hourly" / "calls.json").write_text("not json", encoding="utf-8")
        self.assertEqual(hourly._calls_today(self.dir, "2026-11-26"), 0)


class FetchTest(unittest.TestCase):
    def test_real_fetch_asks_one_grid_at_one_base_time_with_short_waits(self):
        seen = {}

        def fake_fetch_grid(url, key_param, key, base, grid, http_opts, retries, label):
            seen.update(url=url, key_param=key_param, key=key, base=base, grid=grid, http_opts=dict(http_opts), retries=retries)
            return [{"category": "T1H", "obsrValue": "0.8"}, {"category": "WSD", "obsrValue": "2.8"}, {"category": "REH", "obsrValue": "81"},
                    {"category": "RN1", "obsrValue": "0"}, {"category": "PTY", "obsrValue": "0"}, {"category": "VEC", "obsrValue": "184"}]

        with mock.patch.object(kma_ultra, "_fetch_grid", fake_fetch_grid):
            fetch = hourly.make_fetch({"DATA_GO_KR_SERVICE_KEY": "abc%2Bdef"}, CFG.settings.hourly)
            row = fetch(kst(2026, 11, 25, 8), (70, 70))
        self.assertEqual(hourly.pack(row), [0.8, 81, 2.8, 0.0, 0])
        self.assertEqual((seen["url"], seen["key_param"], seen["key"]), (f"{kma_ultra.DATA_GO_KR_BASE}/getUltraSrtNcst", "serviceKey", "abc+def"))
        self.assertEqual((seen["base"], seen["grid"]), (("20261125", "0800"), (70, 70)))
        self.assertEqual((seen["http_opts"]["timeout_seconds"], seen["retries"]), (6.0, 1))  # 짧게 기다리고 한 번만 다시
        with mock.patch.object(kma_ultra, "_fetch_grid", fake_fetch_grid):
            hourly.make_fetch({"KMA_APIHUB_KEY": "hub"}, CFG.settings.hourly)(kst(2026, 11, 25, 8), (70, 70))
        self.assertEqual((seen["url"], seen["key_param"]), (f"{kma_ultra.APIHUB_BASE}/getUltraSrtNcst", "authKey"))

    def test_answers_are_sorted_into_refusal_out_of_range_and_no_data(self):
        def header(code, msg=""):
            return {"response": {"header": {"resultCode": code, "resultMsg": msg}}}

        with self.assertRaises(datagokr.OutOfRange):
            datagokr.extract_items(header("10", "최근 1일 간의 자료만 제공합니다."), "초단기실황")
        with self.assertRaises(datagokr.NoData):
            datagokr.extract_items(header("03", "NO_DATA"), "초단기실황")
        for code in ("22", "30"):
            with self.assertRaises(datagokr.Rejected):
                datagokr.extract_items(header(code), "초단기실황")
        def gateway(code, msg):
            return {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE ERROR", "returnAuthMsg": msg, "returnReasonCode": code}}}

        for code, msg in (("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"), ("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")):
            with self.assertRaises(datagokr.Rejected):
                datagokr.extract_items(gateway(code, msg), "초단기실황")
        for code, msg in (("04", "HTTP ROUTING ERROR"), ("05", "SERVICE TIME OUT"), ("4", "HTTP ROUTING ERROR")):  # 게이트웨이의 일시 오류는 다시 부를 만하다
            try:
                datagokr.extract_items(gateway(code, msg), "초단기실황")
                self.fail("오류가 나야 한다")
            except FetchError as e:
                self.assertFalse(datagokr.is_rejected(e), code)
        try:
            datagokr.extract_items(header("99", "기타"), "초단기실황")
        except FetchError as e:
            self.assertFalse(datagokr.is_rejected(e))  # 그 밖의 오류는 다음에 다시 불러 볼 만하다
            self.assertNotIsInstance(e, (datagokr.OutOfRange, datagokr.NoData))
        self.assertTrue(datagokr.is_rejected(FetchError("HTTP 403", status=403)))
        self.assertFalse(datagokr.is_rejected(FetchError("HTTP 429", status=429)))  # 초당 한도일 수 있다 — 한 번으로 실행을 멈추지 않는다
        self.assertFalse(datagokr.is_rejected(FetchError("TimeoutError")))
        for kind in (datagokr.OutOfRange, datagokr.Rejected, datagokr.NoData):
            self.assertTrue(issubclass(kind, FetchError))  # 기존 호출부는 FetchError 로 잡는다

    def test_one_transient_gateway_error_does_not_stop_the_run(self):
        calls = []

        def fetch(base, grid):
            calls.append(grid)
            if len(calls) == 3:
                datagokr.extract_items({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE ERROR", "returnAuthMsg": "HTTP ROUTING ERROR", "returnReasonCode": "04"}}}, "초단기실황")
            return {"temp": 1.0, "rain": 0.0, "wind": 1.0, "wdir": 0, "rh": 50, "pty": 0}

        out = Path(tempfile.mkdtemp())
        hourly.run(cfg_with(max_back_hours=1, workers=1), NOW, out, ENV, fetch)
        self.assertEqual(len(calls), 2 * len(GRIDS))  # 한 번의 일시 오류 뒤에도 끝까지 받는다
        self.assertEqual(len(hourly.load_day(out, "2026-11-26")), 2)

    def test_local_mode_does_not_collect_unless_asked(self):
        from teukbo import local

        self.assertIs(FULL.settings.hourly["local"], False)  # 같은 인증키로 PC 에서도 받으면 하루 호출 한도를 두 배로 쓴다
        source = (Path(local.__file__)).read_text(encoding="utf-8")
        block = source[source.index("def records_once("):source.index("# ---------------------------------------------------------------- 공개 사이트 갱신 신호")]
        self.assertIn('if cfg.settings.hourly.get("local"):', block)
        self.assertLess(block.index('if cfg.settings.hourly.get("local"):'), block.index("hourly.run(cfg, now, data_dir, env)"))

    def test_workflow_runs_the_step_without_blocking_the_main_collection(self):
        from teukbo.config import ROOT

        text = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        step = text[text.index("시간별 실황 이어 받기"):text.index("- name: 이력 커밋")]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("if: ${{ !cancelled() && steps.due.outputs.run == 'true' }}", step)  # 주 수집이 실패한 회차에도 받는다(23시간이 지나면 되찾을 수 없다)
        commit = text[text.index("- name: 이력 커밋"):text.index("- name: 사이트 조립")]
        self.assertIn("if: ${{ !cancelled() && steps.due.outputs.run == 'true' }}", commit)
        self.assertIn("run: python3 -m teukbo.hourly", step)
        self.assertIn("DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATA_GO_KR_SERVICE_KEY }}", step)
        self.assertLess(text.index("run: python3 -m teukbo.run"), text.index("run: python3 -m teukbo.hourly"))  # 주 수집이 먼저
        self.assertIn("if [ -d data/hourly ]; then git add data/hourly; fi", text)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/data/hours/", ignore)  # 배포 때만 만드는 파일
        self.assertNotIn("/data/hourly/", ignore)  # 쌓는 원자료는 커밋한다


if __name__ == "__main__":
    unittest.main()
