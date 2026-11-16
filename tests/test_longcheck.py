"""호우를 긴 기간으로 본 확인(tools/rain_longcheck.py): 관측을 읽는 법, 세는 법, 첫 화면에 싣는 요약."""

import gzip
import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from teukbo.timeutil import KST

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rain_longcheck", ROOT / "tools" / "rain_longcheck.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)


class LongCheckTest(unittest.TestCase):
    def test_rolling_reach_uses_the_advisory_criteria(self):
        series = [0, 0, 20, 20, 20, 0] + [0] * 10
        reach = lc.rolling_reach(series)
        self.assertAlmostEqual(reach[4], 1.0)  # 3시간 60mm
        self.assertAlmostEqual(lc.rolling_reach([10] * 12)[11], 120 / 110)  # 12시간 110mm 쪽이 더 크다
        self.assertEqual(lc.rolling_reach([None, None]), [0.0, 0.0])  # 빈 값은 0

    def test_observations_take_the_wettest_station_of_a_region(self):
        doc = {"daily": True, "first": "2025-07-16", "days": 3, "stations": {
            "1": {"name": "가", "region": "R", "rain": [10.0, None, None], "temp_min": [20.0, 21.0, None]},
            "2": {"name": "나", "region": "R", "rain": [90.5, None, None], "temp_min": [20.0, 21.0, None]}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "obs.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(doc, f)
            first, days, obs = lc.load_observations(path)
        self.assertEqual((first, days), (date(2025, 7, 16), 3))
        self.assertEqual(obs["R"]["rain"], [90.5, 0.0, None])  # 비 기록이 없는 날은 0, 자료 자체가 없는 날은 None
        self.assertEqual(obs["R"]["ok"], [True, True, False])

    def _summary(self, **kw):
        first = datetime(2025, 7, 15, 0, tzinfo=KST)
        starts = [0, 24, 48, 72]  # 15·16·17·18일 0시
        # (보정까지 한 가능성, 보정 전 가능성, 주변 값): 16일 0시에만 보정 덕에 '주의', 17일 0시에는 둘 다 '높음'
        scores = {"R": [(0, 0, 0.1), (40, 10, 0.6), (70, 65, 1.0), (0, 0, 0.0)], "X": [(90, 90, 2.0)] * 4}
        obs = {"R": {"rain": [0.0, 5.0, 120.0, 0.0, 0.0], "ok": [True] * 5}}  # 17일에 큰비
        return lc.summarize(first, starts, scores, date(2025, 7, 15), 5, obs, 80.0, **kw)

    def test_counts_alerts_and_day_before_capture(self):
        result = self._summary()
        self.assertEqual((result["regions"], result["event_days"]), (1, 1))  # 관측이 없는 지역 X 는 세지 않는다
        cap = result["capture"]
        self.assertEqual(cap["with_area"], {"events": 1, "day_before": 1, "same_day": 1})  # 전날(16일) 0시에 40% = '주의'
        self.assertEqual(cap["base"], {"events": 1, "day_before": 0, "same_day": 1})  # 보정 전에는 전날 10%
        table = result["tables"]["with_area"]
        self.assertEqual([row["cases"] for row in table], [2, 0, 1, 1])
        self.assertEqual([row["hits"] for row in table], [0, 0, 1, 1])  # 16일·17일 0시의 표시는 이틀 안(17일)에 큰비
        self.assertEqual(self._summary(until=date(2025, 7, 16))["capture"]["with_area"]["events"], 0)  # 기간을 좁히면 그 안의 날만

    def test_published_summary_is_consistent(self):
        """저장소의 data/rain_longcheck.json: 숫자들이 서로 맞고, 무엇을 센 것인지(특보가 아니라 관측)가 적혀 있다."""
        doc = json.loads((ROOT / "data" / "rain_longcheck.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["schema"], doc["hazard"], doc["heavy_mm"]), (1, "heavy_rain_advisory", 80.0))
        self.assertEqual(doc["caution_or_higher"]["alerts"], sum(row["cases"] for row in doc["calibration"][2:]))
        self.assertEqual(doc["high"], {"alerts": doc["calibration"][3]["cases"], "hits": doc["calibration"][3]["hits"]})
        for part in (doc, doc["before_area_score"], doc["unseen"], *doc["by_period"], *doc["by_heavy_mm"].values()):
            self.assertLessEqual(part["day_before"], part["events"])
            self.assertLessEqual(part["caution_or_higher"]["hits"], part["caution_or_higher"]["alerts"])
        self.assertEqual(doc["by_heavy_mm"]["80"]["events"], doc["events"])
        self.assertLessEqual(doc["unseen"]["events"], doc["events"])
        self.assertGreater(doc["day_before"], doc["before_area_score"]["day_before"])  # 주변 보정이 전날 포착을 늘렸다
        self.assertTrue(any("관측" in note and "특보" in note for note in doc["notes"]))
        days = (date.fromisoformat(doc["period"]["to"]) - date.fromisoformat(doc["period"]["from"])).days
        self.assertLessEqual(doc["period"]["replays"], days + 1)

    def test_summary_document_shape(self):
        result = self._summary()
        results = {key: result for key in ("all_60", "all_80", "all_110", "unseen", "y1", "y2", "summer", "other")}
        doc = lc.build_document(datetime(2025, 7, 15, 0, tzinfo=KST), 96, 4, results, {"warnings": 28, "reached": 3, "median_mm": 43.1, "from": "a", "to": "b"}, datetime(2026, 11, 17, 3, tzinfo=KST))
        self.assertEqual((doc["period"], doc["events"], doc["day_before"]), ({"from": "2025-07-15", "to": "2025-07-18", "replays": 4}, 1, 1))
        self.assertEqual(doc["before_area_score"]["day_before"], 0)
        self.assertIn("28건", doc["notes"][1])
        self.assertEqual([row["name"] for row in doc["by_period"]][:2], ["2024년 7월 ~ 2025년 6월", "2025년 7월 ~ 2026년 6월"])

    def test_the_site_ships_and_shows_it(self):
        self.assertIn('"rain_longcheck.json"', (ROOT / "teukbo" / "site.py").read_text(encoding="utf-8"))
        home = (ROOT / "web" / "home.js").read_text(encoding="utf-8")
        self.assertIn('getJson("data/rain_longcheck.json").catch(() => null)', home)  # 없어도 화면은 그대로 뜬다
        self.assertIn("위의 숫자와 잣대가 다릅니다", home)  # 특보 기준 숫자와 섞어 읽지 않게
        self.assertIn('fold("long-rain"', home)  # 접는 칸 하나에 따로 담는다


if __name__ == "__main__":
    unittest.main()
