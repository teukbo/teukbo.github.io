"""체감온도: 기상청 겨울철 식, 철에 따라 식을 바꿔 쓰는 feel 계열, 지역별 자료(detail)의 v:feel."""

import json
import tempfile
import unittest
from pathlib import Path

from teukbo import storage
from teukbo.assess import assess_region
from teukbo.config import CONFIG_DIR, DEFAULT_SUMMER_MONTHS, ConfigError, load_settings
from teukbo.indicators import HOUR, ModelSeries, add_feel_temp, feel_temp, kma_summer_apparent_temp, kma_winter_apparent_temp

from .helpers import config, const, kst, no_official, region, series

CFG = config()


class WinterFormulaTest(unittest.TestCase):
    def test_known_value(self):
        # 기온 -10℃, 풍속 5m/s(18km/h): 13.12 + 0.6215·T − 11.37·V^0.16 + 0.3965·V^0.16·T
        v = 18 ** 0.16
        self.assertAlmostEqual(kma_winter_apparent_temp(-10, 5), 13.12 + 0.6215 * -10 - 11.37 * v + 0.3965 * v * -10, places=9)
        self.assertAlmostEqual(kma_winter_apparent_temp(-10, 5), -17.4, places=1)

    def test_wind_makes_it_colder_and_stronger_wind_colder_still(self):
        self.assertLess(kma_winter_apparent_temp(-5, 3), -5)
        self.assertLess(kma_winter_apparent_temp(-5, 10), kma_winter_apparent_temp(-5, 3))

    def test_outside_the_formula_range_it_is_the_air_temperature(self):
        self.assertEqual(kma_winter_apparent_temp(10.1, 8), 10.1)  # 기온 10℃ 초과
        self.assertEqual(kma_winter_apparent_temp(-10, 1.2), -10)  # 풍속 1.3m/s 미만
        self.assertLess(kma_winter_apparent_temp(10.0, 8), 10.0)  # 경계(10℃, 1.3m/s)에서는 식을 쓴다
        self.assertNotEqual(kma_winter_apparent_temp(-10, 1.3), -10)


class FeelSeriesTest(unittest.TestCase):
    def test_month_picks_the_formula(self):
        self.assertAlmostEqual(feel_temp(1, -8, 40, 4), kma_winter_apparent_temp(-8, 4))
        self.assertAlmostEqual(feel_temp(7, 33, 60, 4), kma_summer_apparent_temp(33, 60))
        self.assertAlmostEqual(feel_temp(7, 33, 60, 4, summer_months=(8,)), kma_winter_apparent_temp(33, 4))  # 달 목록은 설정에서 온다

    def test_missing_ingredients_give_none(self):
        self.assertIsNone(feel_temp(1, None, 40, 4))
        self.assertIsNone(feel_temp(1, -8, 40, None))  # 겨울철 식에는 풍속이 필요하다
        self.assertIsNone(feel_temp(7, 33, None, 4))  # 여름철 식에는 습도가 필요하다
        self.assertIsNone(feel_temp(1, float("nan"), 40, 4))

    def test_series_switches_formula_at_the_month_boundary(self):
        start = kst(2027, 4, 30, 22)  # 4월(겨울철 식) → 5월(여름철 식)
        times = [start + k * HOUR for k in range(4)]
        ms = ModelSeries("m", times, {"temp": [8.0, 8.0, 8.0, None], "rh": [50.0, 50.0, 50.0, 50.0], "wind": [5.0, 5.0, 5.0, 5.0]})
        add_feel_temp(ms, DEFAULT_SUMMER_MONTHS)
        feel = ms.values["feel"]
        self.assertAlmostEqual(feel[0], kma_winter_apparent_temp(8, 5), places=2)
        self.assertAlmostEqual(feel[1], kma_winter_apparent_temp(8, 5), places=2)
        self.assertAlmostEqual(feel[2], kma_summer_apparent_temp(8, 50), places=2)  # 5월 1일 0시부터
        self.assertIsNone(feel[3])
        self.assertEqual(ms.lookup("feel")[times[0]], feel[0])

    def test_series_without_wind_in_winter_has_no_feel(self):
        now = kst(2026, 12, 10, 9)
        ms = series("m", now, past=0, future=3, temp=const(-5.0), rh=const(40.0))
        add_feel_temp(ms)
        self.assertNotIn("feel", ms.values)

    def test_existing_feel_is_kept(self):
        now = kst(2026, 12, 10, 9)
        ms = series("m", now, past=0, future=1, temp=const(-5.0), wind=const(4.0), feel=const(1.0))
        add_feel_temp(ms)
        self.assertEqual(ms.values["feel"], [1.0, 1.0])


class FeelSettingsTest(unittest.TestCase):
    def test_summer_months_come_from_settings(self):
        self.assertEqual(CFG.settings.feel_summer_months, (5, 6, 7, 8, 9))
        raw = json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["feel"]["summer_months"], [5, 6, 7, 8, 9])
        self.assertTrue(raw["feel"]["_doc"])

    def _settings(self, change):
        raw = json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
        change(raw)
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return path

    def test_defaults_when_missing(self):
        settings = load_settings(self._settings(lambda raw: (raw.pop("feel"), raw.pop("records"))))
        self.assertEqual(settings.feel_summer_months, DEFAULT_SUMMER_MONTHS)
        self.assertEqual(settings.records, CFG.settings.records)  # 파일의 값이 기본값과 같다

    def test_bad_values_are_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(self._settings(lambda raw: raw["feel"].update(summer_months=[0, 13])))
        with self.assertRaises(ConfigError):
            load_settings(self._settings(lambda raw: raw["records"].update(since="어제")))


class DetailFeelTest(unittest.TestCase):
    def _detail(self, now, **variables):
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=const(0.0), **variables)}
        ra = assess_region(region(), models, no_official(), CFG, now)
        return storage.build_detail(ra, models, CFG, now)

    def test_winter_detail_carries_wind_chill(self):
        now = kst(2026, 12, 10, 9)
        detail = self._detail(now, temp=const(-8.0), rh=const(40.0), wind=const(6.0))
        feel = detail["series"][storage.FEEL_KEY]["ecmwf_ifs025"]
        self.assertEqual(len(feel), CFG.settings.horizon_hours + 1)
        self.assertEqual(len(feel), len(detail["times"]))
        self.assertEqual(set(feel), {round(kma_winter_apparent_temp(-8, 6), 1)})
        self.assertLess(feel[0], -8)
        self.assertEqual(set(detail["series"]["v:temp"]["ecmwf_ifs025"]), {-8.0})  # 기존 계열은 그대로

    def test_summer_detail_matches_the_heat_warning_apparent_temperature(self):
        now = kst(2026, 7, 20, 9)
        detail = self._detail(now, temp=const(33.0), rh=const(60.0), wind=const(2.0))
        self.assertEqual(detail["series"][storage.FEEL_KEY]["ecmwf_ifs025"], detail["series"]["v:apparent"]["ecmwf_ifs025"])

    def test_model_without_the_ingredients_is_left_out(self):
        now = kst(2026, 12, 10, 9)
        models = {
            "ecmwf_ifs025": series("ecmwf_ifs025", now, precip=const(0.0), temp=const(-8.0), wind=const(6.0)),
            "ncep_gfs_seamless": series("ncep_gfs_seamless", now, precip=const(0.0), temp=const(-8.0)),  # 풍속 없음
        }
        ra = assess_region(region(), models, no_official(), CFG, now)
        detail = storage.build_detail(ra, models, CFG, now)
        self.assertEqual(list(detail["series"][storage.FEEL_KEY]), ["ecmwf_ifs025"])

    def test_no_feel_key_when_nothing_to_show(self):
        now = kst(2026, 12, 10, 9)
        detail = self._detail(now)  # 기온이 없다
        self.assertNotIn(storage.FEEL_KEY, detail["series"])

    def test_feel_is_only_a_display_series(self):
        # 특보 판단 지표에는 들어가지 않는다: 체감온도를 넣어도 가능성 계산은 같다
        now = kst(2026, 12, 10, 9)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=const(0.0), temp=const(-20.0), rh=const(40.0), wind=const(10.0))}
        ra = assess_region(region(), models, no_official(), CFG, now)
        used = {leaf.indicator for hz in CFG.thresholds.hazards for leaf in hz.leaves()}
        self.assertFalse(any(CFG.thresholds.indicators[key].variable == "feel" for key in used))
        self.assertTrue(any(a.hazard.type == "cold_wave" and a.probability for a in ra.hazards))


if __name__ == "__main__":
    unittest.main()
