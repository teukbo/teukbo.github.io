"""기상청 관측(ASOS) 시간자료·일자료: 응답을 읽는 법과 요청에 싣는 값. 두 서비스는 주소도 활용신청도 따로다."""

import unittest
from datetime import date, datetime

from teukbo.sources import asos
from teukbo.timeutil import KST


def reply(items, total=None):
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                         "body": {"dataType": "JSON", "items": {"item": items}, "pageNo": 1, "numOfRows": 999, "totalCount": len(items) if total is None else total}}}


class AsosTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = asos.get_json
        self.addCleanup(lambda: setattr(asos, "get_json", self.original))

    def serve(self, items):
        def fake(url, params, **kw):
            self.calls.append((url, dict(params)))
            return reply(items)
        asos.get_json = fake

    def test_hourly_rows_and_request(self):
        self.serve([{"tm": "2025-07-17 14:00", "stnId": "174", "stnNm": "순천", "ta": "24.1", "rn": "31.5", "ws": "3.2", "hm": "97", "hr3Fhsc": "", "dsnw": ""},
                    {"tm": "깨진 값", "stnId": "174"}])
        rows = asos.fetch_hourly("key", "174", datetime(2025, 7, 17, 0, tzinfo=KST), datetime(2025, 7, 17, 23, tzinfo=KST))
        self.assertEqual(len(rows), 1)  # 시각을 못 읽는 행은 버린다
        self.assertEqual((rows[0]["t"], rows[0]["precip"], rows[0]["snow3h"]), (datetime(2025, 7, 17, 14, tzinfo=KST), 31.5, None))
        url, params = self.calls[0]
        self.assertIn("AsosHourlyInfoService", url)
        self.assertEqual((params["dateCd"], params["startDt"], params["startHh"], params["endHh"], params["stnIds"]), ("HR", "20250717", "00", "23", "174"))

    def test_daily_rows_and_request(self):
        self.serve([{"tm": "2025-07-17", "stnId": "156", "stnNm": "광주", "sumRn": "426.4", "hr1MaxRn": "80.0", "maxWs": "6.1", "maxInsWs": "12.3",
                     "minTa": "23.0", "maxTa": "26.4", "avgRhm": "96.5", "ddMefs": ""},
                    {"tm": "2025-07-18", "stnId": "156", "stnNm": "광주", "sumRn": "", "hr1MaxRn": "", "maxWs": "4.0", "maxInsWs": "8.8", "minTa": "22.1", "maxTa": "30.2", "avgRhm": "80", "ddMefs": ""}])
        rows = asos.fetch_daily("key", "156", date(2025, 7, 17), date(2025, 7, 18))
        self.assertEqual([r["d"] for r in rows], [date(2025, 7, 17), date(2025, 7, 18)])
        self.assertEqual((rows[0]["rain"], rows[0]["rain_1h_max"], rows[0]["gust_max"], rows[0]["temp_min"]), (426.4, 80.0, 12.3, 23.0))
        self.assertIsNone(rows[1]["rain"])  # 빈 값은 None — '비 없음'으로 볼지는 쓰는 쪽이 정한다
        self.assertEqual(set(rows[0]) - {"d", "stn", "name"}, set(asos.DAILY_FIELDS.values()))
        url, params = self.calls[0]
        self.assertIn("AsosDalyInfoService", url)  # 시간자료와 다른 서비스(활용신청도 따로)
        self.assertEqual((params["dateCd"], params["startDt"], params["endDt"], params["stnIds"]), ("DAY", "20250717", "20250718", "156"))
        self.assertNotIn("startHh", params)

    def test_the_two_services_are_different_endpoints(self):
        self.assertNotEqual(asos.ENDPOINT, asos.DAILY_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
