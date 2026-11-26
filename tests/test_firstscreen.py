"""첫 화면 고도화(특보 종류 고르기 · 작업 기준 · 특보 기록 · 움직임)가 지키는 약속.

화면 파일(web/)과 설정(config/work_rules.json)을 글로 읽어 확인한다 — 브라우저 없이 도는 검사다.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from teukbo import local, records
from teukbo.config import ROOT, load_config

WEB = ROOT / "web"
NEW_SECTION = "   첫 화면 고도화:"
MOTION_GATE = "@media (prefers-reduced-motion: no-preference) {"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class TypeFirstTest(unittest.TestCase):
    """숫자보다 '무슨 특보인지'가 먼저 보인다. 종류를 고르면 가능성·지도·기록이 모두 그 종류만 보여 준다."""

    def test_hazard_picture_and_name_come_before_the_number(self):
        script = read("home.js")
        hero = script[script.index("function hero(sm)"):script.index("function hazardRow(")]
        self.assertIn("Icons.hazard(sm.type", hero)
        self.assertLess(hero.index("hero-icon"), hero.index("hero-name"))  # 그림 → 이름 → 숫자
        self.assertLess(hero.index("hero-name"), hero.index("hero-num"))
        self.assertIn("hazard.name", hero)  # 이름은 받아 온 자료의 특보 이름 그대로
        style = read("style.css")
        name_size = int(re.search(r"\.hero-name b \{ font-size: (\d+)px", style).group(1))
        self.assertGreaterEqual(name_size, 26)  # 이름이 작은 글씨로 밀려나지 않게

    def test_every_hazard_type_has_its_own_picture(self):
        icons = read("icons.js")
        for type_id in load_config().thresholds.types:
            self.assertIn(f"    {type_id}: () =>", icons, type_id)
        self.assertIn('"aria-hidden": "true"', icons)  # 그림은 장식 — 이름은 늘 글로 적는다
        self.assertNotIn("innerHTML", icons)

    def test_type_picker_drives_result_map_and_records(self):
        html, script = read("index.html"), read("home.js")
        body = html[html.index("<main"):html.index("</main>")]
        self.assertIn('<div class="types" id="types" role="group" aria-label="특보 종류"></div>', body)
        self.assertLess(body.index('id="q"'), body.index('id="types"'))
        self.assertLess(body.index('id="types"'), body.index('id="result"'))
        self.assertIn('TYPE_KEY = "teukbo.type"', script)  # 고른 종류를 기억한다
        self.assertIn("localStorage.setItem(TYPE_KEY, state.type)", script)
        paint = script[script.index("function paintMap()"):script.index("// ------------------------------------------------------------ 기록")]
        self.assertIn("gradeOn(region)", paint)  # 지도 색도 고른 종류의 등급으로
        self.assertIn('$("map-what").textContent', paint)  # 무엇을 칠했는지 지도 위에 적는다
        sky = script[script.index("function paintSky()"):script.index("// ------------------------------------------------------------ 과거 검증")]
        self.assertIn("gradeOn(", sky)
        rec = script[script.index("function renderRecords("):script.index("// ------------------------------------------------------------ 머리말 · 전체")]
        self.assertIn("type: state.type", rec)
        self.assertIn("types: state.latest.types, levels: state.latest.levels", rec)  # 종류·단계 이름은 자료에서

    def test_new_scripts_load_before_the_screen_script_and_work_offline(self):
        html, worker = read("index.html"), read("sw.js")
        for name in ("ui.js", "live.js", "icons.js", "records.js"):
            self.assertLess(html.index(f'src="{name}?v=__BUILD__"'), html.index('src="home.js?v=__BUILD__"'), name)
        for name in ("icons.js", "records.js"):
            self.assertIn(f'"{name}?v=__BUILD__"', worker, name)


class WorkRulesTest(unittest.TestCase):
    """폭염·한파 작업 기준: 수치와 문구는 config/work_rules.json 에만 있고, 의무와 권고를 구별해 적는다."""

    def setUp(self):
        self.rules = json.loads((ROOT / "config" / "work_rules.json").read_text(encoding="utf-8"))
        self.th = load_config().thresholds

    def test_rules_match_the_warning_types_and_levels(self):
        self.assertRegex(self.rules["checked_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(set(self.rules["kinds"]), {"duty", "advice"})
        self.assertEqual([g["type"] for g in self.rules["guides"]], ["heat_wave", "cold_wave"])
        for guide in self.rules["guides"]:
            self.assertIn(guide["type"], self.th.types, guide["id"])
            self.assertIn(guide["metric"]["stat"], ("min", "max"), guide["id"])
            self.assertTrue(all(1 <= m <= 12 for m in guide["months"]), guide["id"])
            self.assertTrue(guide["stages"], guide["id"])
            for stage in guide["stages"]:
                self.assertIn(stage["kind"], self.rules["kinds"], stage)
                self.assertTrue(stage["text"] and stage["basis"], stage)  # 단계마다 할 일과 근거
                keys = [k for k in ("min", "max", "level") if k in stage]
                self.assertEqual(len(keys), 1, stage)  # 체감온도 기준이거나 특보 단계 기준, 둘 중 하나
                if "level" in stage:
                    self.assertIn(stage["level"], self.th.levels, stage)
                    self.assertEqual(stage["name"], self.th.type_name(guide["type"]) + self.th.level_name(stage["level"]), stage)
            for source in guide["sources"]:
                self.assertTrue(source["url"].startswith("https://"), source)
        months = [set(g["months"]) for g in self.rules["guides"]]
        self.assertFalse(months[0] & months[1])  # '전체'에서 두 기준이 한꺼번에 뜨지 않는다

    def test_heat_stages_follow_the_rule_and_the_ministry_guide(self):
        heat = next(g for g in self.rules["guides"] if g["id"] == "heat")
        mins = [s["min"] for s in heat["stages"]]
        self.assertEqual(mins, sorted(mins))  # 낮은 단계부터
        self.assertEqual(sorted(set(mins)), [31, 33, 35, 38])
        duty = {s["min"] for s in heat["stages"] if s["kind"] == "duty"}
        self.assertEqual(duty, {31, 33})  # 법령상 의무는 31℃·33℃ 조치뿐 — 35℃·38℃의 옥외작업 중지는 권고다
        rest = next(s for s in heat["stages"] if s["min"] == 33 and s["kind"] == "duty")
        self.assertIn("매 2시간 이내에 20분 이상", rest["text"])
        self.assertIn("제560조제3항", rest["basis"])
        self.assertIn("권고", heat["note"])
        cold = next(g for g in self.rules["guides"] if g["id"] == "cold")
        self.assertTrue(all(s["kind"] == "advice" for s in cold["stages"]))  # 한파는 단계별 의무 조문이 아직 없다
        self.assertEqual([s["level"] for s in cold["stages"]], ["advisory", "warning"])

    def test_screen_reads_the_rules_from_data_and_keeps_numbers_out_of_code(self):
        script = read("home.js")
        self.assertIn('getJson("data/work_rules.json")', script)
        work = script[script.index("function renderWork(region)"):script.index("function fillWeather(")]
        for needle in ("rules.guides", "stage.min", "stage.max", "stage.level", "rules.kinds", "stage.basis", "guide.sources", "rules.checked_at", "rules.notice"):
            self.assertIn(needle, work, needle)
        for guide in self.rules["guides"]:
            for stage in guide["stages"]:
                if "min" in stage:
                    self.assertNotIn(f"{stage['min']}℃", script, stage["min"])
                self.assertNotIn(stage["text"][:12], script, stage["text"])
        site = (ROOT / "teukbo" / "site.py").read_text(encoding="utf-8")
        self.assertIn('site_data / "work_rules.json"', site)  # 배포 때 화면이 읽는 자리로 복사된다

    def test_work_card_folds_into_the_weather_card_when_nothing_applies(self):
        # 간소화(2026-11-26): 눈여겨볼 단계가 없는 날에는 작업 기준이 칸을 따로 차지하지 않고 날씨 칸 끝의 접힌 한 줄이 된다
        # (체감온도는 바로 위 날씨 칸에 있다). 해당하는 단계가 있거나 그 특보의 가능성이 '주의' 이상이면 전처럼 제 칸으로 보인다.
        script, style = read("home.js"), read("style.css")
        work = script[script.index("function renderWork(region)"):script.index("function fillWeather(")]
        self.assertIn('if (state_ === "near") near = [stage, note];', work)
        self.assertIn("const notable = Boolean(reached || near);", work)
        self.assertIn('if (!notable && doc === "loading") return null;', work)  # 받는 중에는 칸을 띄웠다 접지 않는다
        self.assertIn("if (!notable && w) return { tail:", work)
        self.assertIn("if (state.type && notable) return { card:", work)  # 고른 종류에 눈여겨볼 단계가 있으면 펼쳐서
        self.assertIn('"해당 단계 없음"', work)  # 접어 둔 채로도 제목에서 보인다
        fill = script[script.index("function fillWeather(region, animate)"):script.index("/** 바탕 하늘")]
        self.assertIn("weather = renderWeather(region, work.tail)", fill)
        self.assertIn("!weather && work.tail", fill)  # 날씨 칸을 못 그린 때에는 제 칸에 둔다(사라지지 않게)
        weather = script[script.index("function renderWeather(region, tail)"):script.index("// ------------------------------------------------------------ 작업 기준")]
        self.assertIn("if (tail) box.append(tail);", weather)
        self.assertIn(".weather > .work-tail {", style)

    def test_feel_temperature_series_reaches_the_screen(self):
        script = read("home.js")
        self.assertIn('mid("v:feel")', script)  # 체감온도는 서버가 계산해 싣는 계열을 읽는다(화면에서 식을 다시 계산하지 않는다)
        self.assertNotIn("13.12", script)
        self.assertIn('"v:feel"', (ROOT / "teukbo" / "storage.py").read_text(encoding="utf-8"))


class RecordsScreenTest(unittest.TestCase):
    """특보 기록(달력): 배포 때 만든 지역별 기록을 읽어, 고른 종류의 특보 발효일과 그날의 관측값을 보여 준다."""

    def test_screen_reads_the_built_records(self):
        script = read("records.js")
        self.assertIn("data/records/${encodeURIComponent(id)}.json", script)
        self.assertNotIn("innerHTML", script)  # 받아 온 글은 늘 텍스트로
        names = set(re.findall(r'\["(\w+)", "[^"]+", "[^"]*"\]', script[script.index("const METRIC = {"):script.index("const docs = new Map()")]))
        self.assertTrue(names)
        self.assertLessEqual(names, set(records.VARS) | {"feel_min", "feel_max"})  # 달력 칸에 적는 값은 기록 파일에 실리는 값뿐(체감온도는 그날의 요약 f 에서)
        for type_id in re.findall(r"(\w+): \[\[", script[script.index("const METRIC = {"):script.index("const docs = new Map()")]):
            self.assertIn(type_id, load_config().thresholds.types, type_id)
        for needle in ("ctx.types", "ctx.levels", "doc.station", "doc.hours", "navigator.clipboard.writeText", "aria-pressed"):
            self.assertIn(needle, script, needle)
        self.assertNotIn("주의보", script)  # 단계 이름은 자료(levels)에서 읽는다
        html = read("index.html")
        self.assertIn('<section class="glass records" id="records" aria-labelledby="h-records">', html)
        self.assertIn('id="records-body"', html)

    def test_local_mode_serves_records_and_work_rules(self):
        web, data = Path("/srv/web"), Path("/srv/data")
        self.assertEqual(local.resolve_path("/data/records/beolgyo.json", web, data), data / "records" / "beolgyo.json")
        self.assertEqual(local.resolve_path("/data/work_rules.json", web, data), ROOT / "config" / "work_rules.json")
        self.assertIsNone(local.resolve_path("/data/records/../latest.json", web, data))
        self.assertIsNone(local.resolve_path("/data/obs/2026-11.json", web, data))  # 원자료는 내보내지 않는다


class HoursScreenTest(unittest.TestCase):
    """달력에서 날짜를 누르면 그날의 시간대별 체감온도·날씨가 아래에 뜬다(기상청 초단기실황 — 배포 때 만든 지역별 한 달 파일)."""

    def test_day_panel_loads_the_month_file_when_a_day_has_hourly_values(self):
        script = read("records.js")
        self.assertIn("data/hours/${encodeURIComponent(id)}/${month}.json", script)
        day = script[script.index("function renderDay(doc, ctx)"):script.index("/** 기록 칸을 그린다.")]
        self.assertIn("count = (doc.days[ymd] || {}).h || 0", day)
        self.assertIn("const hdoc = count ? loadHours(doc.id, ymd.slice(0, 7), ctx) : null;", day)  # 값이 있는 날에만, 그 달 것을 그때 받는다
        self.assertIn("hourStrip(rows, peak,", day)
        self.assertIn('"이 날 시간별 표 복사"', day)
        facts = script[script.index("function dayFacts(doc, ymd, rows, hdoc)"):script.index("/** 날짜를 누르면 그 아래에 뜨는 그날 칸:")]
        self.assertIn("doc.hours.from", facts)  # 시간별 값이 없는 날에는 언제부터 있는지 적는다
        self.assertIn("이 날은 시간대별 값을 받지 못했습니다.", facts)  # 모으기 시작한 뒤의 빈 날은 그렇게 따로 적는다

    def test_day_panel_is_one_block_and_sources_live_in_one_fold(self):
        # 간소화(2026-11-26): 날짜를 누르면 하루 값 칸과 시간대별 칸이 따로 뜨던 것을 한 덩어리로 —
        # 날짜·특보 → 그날의 숫자 한 묶음(체감·기온·강수·바람) → 시간대별 띠 → 표 복사. 출처·기간은 맨 아래 접힌 칸 한 곳에만.
        script, style = read("records.js"), read("style.css")
        self.assertEqual(script.count('class: "rec-facts"'), 1)  # 숫자 묶음은 하나
        day = script[script.index("function dayFacts(doc, ymd, rows, hdoc)"):script.index("/** 기록 칸을 그린다.")]
        self.assertLess(day.index('class: "rec-facts"'), day.index("hourStrip(rows, peak,"))  # 숫자 → 띠
        for word in ("rec-source", "관측소(약", "초단기실황", "hr-title"):
            self.assertNotIn(word, day, word)  # 그날 칸에는 출처·기간을 되풀이하지 않는다
        body = script[script.index("  function render(box, ctx) {"):script.index("  return { render };")]
        fold = body[body.index('ctx.fold("rec-src", "자료: 기상청'):]
        for needle in ("doc.station.name", "doc.station.km", "doc.obs_to", "doc.hours.source", "doc.hours.from", "관측 지점이 달라"):
            self.assertIn(needle, fold, needle)  # 출처 글은 자료에 실린 그대로, 지점이 다르다는 것도 여기서 밝힌다
        self.assertIn('"source": HOURS_SOURCE', (ROOT / "teukbo" / "records.py").read_text(encoding="utf-8"))
        for rule in (".hr-title", ".rec-source", ".hr {"):
            self.assertNotIn(rule, style, rule)
        self.assertIn(".rec-facts { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr) minmax(0, 1fr);", style)
        # 하루 값(관측소)이 없는 날은 받은 시간별 값으로 적고, 그렇다고 밝힌다
        self.assertIn('else { const a = extreme(rows, "temp", -1), b = extreme(rows, "temp", 1);', day)
        self.assertIn("기준입니다(", day)

    def test_daily_feel_extremes_come_only_from_the_served_summary(self):
        # 받은 시각이 모자란 날(수집 첫날, 오늘, 수집이 멈췄던 날)에 받은 시각만으로 '그날의 체감 최저'를 내세우지 않는다:
        # 서버가 실어 준 f 가 있을 때만 그 값·시각을 그대로 적고, 없으면 이름에 '(N시간 중)'을 붙인다. 문턱 숫자는 화면에 없다.
        script = read("records.js")
        block = script[script.index("function dayFacts(doc, ymd, rows, hdoc)"):script.index("/** 날짜를 누르면 그 아래에 뜨는 그날 칸:")]
        self.assertIn("Array.isArray(day.f) && day.f.length === 4", block)
        self.assertIn("const [lo, loHour, hi, hiHour] = day.f", block)
        self.assertIn('"strong", `(${rows.length}시간 중)`', block)
        self.assertIn("시간대별 값은 ${rows.length}시간치만 있습니다.", block)
        self.assertNotIn("feel_min_hours", script)
        self.assertNotRegex(block, r"[<>]=?\s*(19|20)\b")  # 20시각 문턱을 화면에서 다시 따지지 않는다
        self.assertIn("kinds.get(name).push(r.hour)", block)  # 강수는 형태 이름별로 시각을 묶는다(비·눈이 섞인 날을 한 이름으로 부르지 않는다)

    def test_days_stay_reachable_when_the_warning_history_lags(self):
        script = read("records.js")
        self.assertIn("const ends = [doc.to, doc.obs_to, doc.hours && doc.hours.to].filter(Boolean).sort();", script)
        body = script[script.index("  function render(box, ctx) {"):script.index("  return { render };")]
        self.assertIn("inRange = ymd >= span.from && ymd <= span.to", body)  # 특보 이력이 늦거나 없어도 값이 있는 날은 누를 수 있다
        self.assertIn("monthsOf(span)", body)
        self.assertIn('!warnKnown(doc, ymd) ? "특보 이력 아직 없음"', script)  # '없음'과 '모름'을 가른다
        self.assertIn("if (!warnKnown(doc, ymd)) continue;", body)  # 이 달의 특보 일수는 이력을 받아 둔 날만 센다
        self.assertIn('"이력 없음"', script)  # 복사한 표에서도 빈칸(= 특보 없음)과 구별된다

    def test_loaded_files_retry_after_failure_and_refresh_with_new_data(self):
        script, home = read("records.js"), read("home.js")
        cache = script[script.index("function cached(store, key, url, ctx, relevant)"):script.index("const load = (id, ctx)")]
        self.assertIn("entry.stamp !== ctx.stamp", cache)  # 계산 시각이 바뀌면 다시 받는다(열어 둔 탭에서 오늘의 시간대별이 멈추지 않게)
        self.assertIn("if (doc != null || entry.doc == null) entry.doc = doc;", cache)  # 다시 받기에 실패해도 가지고 있던 것은 버리지 않는다
        self.assertIn("if (relevant()) ctx.again();", cache)  # 다른 지역·달로 옮긴 뒤에 온 응답은 화면을 건드리지 않는다
        self.assertIn("stamp: state.base.generated_at", home)
        self.assertIn("onclick: () => { retryFailed(); view.day", script)  # 못 받은 것은 날짜를 다시 누르면 한 번 더 받는다
        self.assertIn("entry.doc === null && !entry.busy", script)

    def test_redraw_keeps_focus_and_cells_are_named_for_screen_readers(self):
        script = read("records.js")
        body = script[script.index("  function render(box, ctx) {"):script.index("  return { render };")]
        self.assertIn('active.getAttribute("data-focus")', body)
        self.assertIn("again.focus({ preventScroll: true })", body)
        for key in ('"data-focus": `day:${ymd}`', '"data-focus": "prev"', '"data-focus": "next"', '"data-focus": "copy-month"'):
            self.assertIn(key, body, key)
        strip = script[script.index("function hourStrip(rows, peak, label, key)"):script.index("function dayFacts(doc, ymd, rows, hdoc)")]
        self.assertIn('h("span", { class: "sr-only", text: said })', strip)  # 칸마다 이름을 붙인 한 줄 글
        self.assertIn('"aria-hidden": "true", text })', strip)  # 보이는 숫자는 따로 읽지 않는다
        self.assertIn('role: "region"', strip)
        self.assertIn('"data-focus": "hours"', strip)
        self.assertIn("view.copied.where === where", script)  # 복사 알림은 그 날·그 달의 단추 옆에만

    def test_feel_temperature_is_shown_as_served_never_recomputed(self):
        script = read("records.js")
        for constant in ("13.12", "0.6215", "11.37", "0.3965", "4.8", "0.16", "atan"):
            self.assertNotIn(constant, script, constant)  # 체감온도 식은 서버(teukbo/indicators.py)에만 있다
        self.assertIn('hdoc.feel_stat !== "max"', script)  # 어느 쪽 극값을 앞세울지도 파일이 정한다(철을 화면에서 따지지 않는다)
        self.assertIn("window.TeukboLive.ptyText(pty)", script)  # 강수형태 이름은 실시간 화면과 같은 표에서
        for word in ("소나기", "빗방울", "눈날림"):
            self.assertNotIn(word, script, word)
        self.assertIn('4: "소나기"', read("live.js"))  # 2026-06 가이드에서 초단기에 추가된 코드
        self.assertNotIn("innerHTML", script)

    def test_strip_keeps_gaps_and_layout_numbers_in_step_with_the_stylesheet(self):
        script, style = read("records.js"), read("style.css")
        self.assertIn("const HOUR_COL = 46, HOUR_CURVE = 54;", script)
        self.assertIn(".hr-col { width: 46px;", style)
        self.assertIn(".hr-names, .hr-col { display: grid; flex: none; grid-template-rows: 20px 18px 54px 20px 22px 18px;", style)
        self.assertIn(".hr-chart { position: absolute; top: 38px;", style)  # 시각(20) + 강수(18) 줄 아래가 꺾은선 자리
        strip = script[script.index("function hourStrip(rows, peak, label, key)"):script.index("function dayFacts(doc, ymd, rows, hdoc)")]
        self.assertIn("r.hour === last[last.length - 1].hour + 1", strip)  # 이어진 시각끼리만 선으로 잇는다(빈 시각을 건너 잇지 않는다)
        # 비·눈이 한 시각도 없던 날은 늘 비어 있는 강수 줄을 두지 않는다 — 줄이 하나 빠진 만큼 꺾은선 자리도 올린다
        self.assertIn("wet = rows.some(isWet)", strip)
        self.assertIn(".hr-strip.dry .hr-names, .hr-strip.dry .hr-col { grid-template-rows: 20px 54px 20px 22px 18px; }", style)
        self.assertIn(".hr-strip.dry .hr-chart { top: 20px; }", style)
        # 띠의 왼쪽 끝은 늘 칸 경계에 온다(글자가 반쯤 잘려 보이지 않게): 처음 자리도, 맨 끝까지 민 자리도
        self.assertIn("/ HOUR_COL) * HOUR_COL)", strip)
        self.assertIn("scroller.clientWidth % HOUR_COL", strip)
        self.assertIn("값 없음", strip)  # 값이 없는 시각은 칸을 비우고 그렇게 알린다
        self.assertIn("view.strip.left = scroller.scrollLeft", strip)  # 옆으로 민 자리는 다시 그려도 그대로
        self.assertIn('class: "hr-key feel"', strip)  # 선의 뜻은 색만이 아니라 이름표와 선 모양(점선)으로도

    def test_forecast_strip_notes_feel_temperature_only_when_it_differs(self):
        script, style = read("home.js"), read("style.css")
        strip = script[script.index("function hourStrip(w)"):script.index("/** 날씨 카드")]
        self.assertIn("Math.abs(b.feel - b.temp) >= FEEL_GAP", strip)
        self.assertIn('class: "strip-feel"', strip)
        self.assertIn(".strip.has-feel .strip-line { bottom: 16px; }", style)  # 한 줄이 늘면 기온 선도 같이 올린다


class MotionTest(unittest.TestCase):
    """움직임은 '움직임 줄이기'를 켜지 않았을 때만, 그리고 지역·종류를 바꾼 첫 그림에서만."""

    def test_new_motion_is_gated_by_reduced_motion(self):
        style = read("style.css")
        section = style[style.index(NEW_SECTION):]
        before, gated = section.split(MOTION_GATE, 1)
        self.assertNotIn("animation", before)  # 움직임 선언은 모두 그 묶음 안에
        self.assertNotIn("transition", before)
        self.assertNotIn("@keyframes", before)
        for name in ("rise", "gauge-grow", "hz-drop", "hz-gust", "hz-spin", "sky-rain"):
            self.assertIn(f"@keyframes {name} ", gated, name)
        self.assertNotIn("backdrop-filter", section)  # 칸마다 따로 합성되는 배경 흐림은 쓰지 않는다(지도가 덜 그려지던 문제)

    def test_motion_plays_only_when_the_scene_changes(self):
        script = read("home.js")
        render = script[script.index("  function render() {"):script.index("  function applyLive()")]
        self.assertIn("state.fresh = scene !== state.scene;", render)  # 몇 분마다 다시 그릴 때는 움직이지 않는다
        self.assertIn('if (state.fresh) box.firstChild.classList.add("enter");', script)
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)").matches', script)  # 숫자 올리기도 설정을 따른다
        fill = script[script.index("function fillWeather(region, animate)"):script.index("/** 바탕 하늘")]
        self.assertNotIn("renderResult", fill)  # 날씨가 늦게 와도 가능성의 머리는 다시 그리지 않는다
        # 인트로가 걷힐 때 첫 그림의 움직임을 한 번 보여 주되, 인트로를 건너뛴 때(같은 탭에서 다시 연 때)는 다시 움직이지 않는다
        # — 건너뛴 채로도 몇 초 뒤에 화면이 한 번 더 떠오르던 문제(2026-11-25).
        intro = script[script.index("function closeIntro(now)"):script.index("function repaintMap()")]
        skip, replay = intro.index('classList.contains("no-intro")) { intro.remove(); return; }'), intro.index("state.scene = null; render();")
        self.assertLess(skip, replay)


if __name__ == "__main__":
    unittest.main()
