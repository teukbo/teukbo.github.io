/* 첫 화면 — 지역을 찾으면 그 지역의 특보 가능성만 보여 준다. 지도에서 눌러도 된다.
 *
 * 자료
 *  data/latest.json   3시간마다 계산한 지역별 가능성(등급·%·예상 시간대) — 전국의 기상청 특보구역 단위
 *  live.json          몇 분마다 확인한 공식 특보·예비특보(있으면 덮어 쓴다: 발효 중이면 '발효 중', 예비특보면 '높음' 이상)
 *  geo/zones.json     특보구역 경계(지도)
 *  geo/rail.json · geo/stations.json  철도 노선과 역 위치(지도에 겹쳐 그린다. 역 이름으로도 지역을 찾는다). 없어도 화면은 동작한다
 *  data/detail/<지역>.json  그 지역의 시간별 예보 값(모델별) — 고른 지역의 '앞으로 48시간 날씨'를 적는 데 쓴다. 없으면 그 칸을 뺀다
 *  data/backtest.json 과거 검증 — 지난 예보로 가능성을 다시 계산해 실제 특보와 맞춰 본 결과(teukbo.backtest). 없으면 그 구역을 숨긴다
 *  data/rain_longcheck.json 호우를 더 긴 기간(2년 남짓)으로 본 확인 — 실제 결과를 기상청 관측 일강수량으로 바꿔 맞춰 본 것(tools/rain_longcheck.py). 없으면 그 부분만 뺀다
 *  data/work_rules.json  폭염·한파 때의 작업 기준(법령·고용노동부 지침을 옮긴 config/work_rules.json). 없으면 '작업 기준' 칸을 뺀다
 *  data/records/<지역>.json  그 지역의 특보 발효 기록과 그날의 관측값 — records.js 가 읽어 달력으로 그린다
 * 화면의 상태는 둘이다: 고른 지역(없으면 전국)과 고른 특보 종류(없으면 전체). 종류를 고르면 가능성·지도·기록이 모두 그 종류만 보여 준다.
 * 원칙: 받아 온 글은 늘 텍스트로 넣는다. 공식 특보가 있으면 가능성보다 먼저 보여 준다. 숫자보다 '무슨 특보인지'(그림·이름)를 먼저 보여 준다.
 */
(() => {
  "use strict";
  const { h, s, $, clear, fmtClock, fmtAgo, getJson } = window.TeukboUI;
  const Live = window.TeukboLive, P = Live.radarProject, Icons = window.TeukboIcons, Records = window.TeukboRecords;
  const GRADE = { low: "낮음", caution: "주의", high: "높음", active: "발효 중", unknown: "확인 불가" };
  const RANK = { unknown: -1, low: 0, caution: 1, high: 2, active: 3 };
  const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];
  const RELOAD_MS = 10 * 60 * 1000, STORE_KEY = "teukbo.region", TYPE_KEY = "teukbo.type", COUNT_MS = 800, MAX_MATCHES = 8, MAX_OTHERS = 4, MAX_NOTABLE = 4, MAX_STATIONS = 12;
  const MAX_OTHER_STATION_NAMES = 8, ZONE_FONT = 12, STATION_FONT = 11;
  const ZOOM_MIN_W = 5, LABEL_MAX_W = 120, ZOOM_STEP = 1.6;  // 가장 가까이 볼 때의 너비(지도 단위), 이름표·역을 적는 가장 넓은 범위, ＋/－ 한 번의 배율
  const INTRO_REVEAL_MS = 80;  // 인트로가 옅어지기 시작하고 이만큼 뒤에 본문을 그린다
  const INTRO_MIN_MS = 4200, INTRO_FADE_MS = 500;  // 여는 화면: 게이지가 차고(2.3초) 이름이 다 나온 뒤(3.2초) 1초쯤 머물도록 연 때부터 최소 이만큼은 보이고, 이만큼에 걸쳐 걷힌다(style.css 의 .intro 와 같게)
  const FEEL_GAP = 2;  // 체감온도가 기온과 이만큼 다를 때만 '지금' 옆에 따로 적는다
  const RAIN_MODEL_MM = 1.0, RAIN_START_MM = 0.3, SNOW_MIN_CM = 1.0;  // 날씨 요약: 48시간 합계가 이만큼이면 '비 예상'으로, 3시간 합계가 이만큼 되면 '시작'으로 본다  // 지도 위 이름표의 글자 크기(px) — style.css 의 .map-label · .map-station-name 과 같게
  const NEAR_COUNT = 16, NEAR_MIN = 12, NEAR_MAX = 42;  // 고른 지역 둘레를 얼마나 넓게 보여 줄지(이웃 지역 수 기준, 지도 단위 ≈ 1.7km)
  const MIN_PROOF_CASES = 30, MIN_PROOF_DAYS = 3;  // 과거 검증 값을 가능성 옆에 적으려면: 표본 30회 이상, 특보가 있었던 날 3일 이상
  const state = { base: null, latest: null, live: null, zones: null, proof: null, longRain: null, work: null, openFolds: new Set(), region: null, type: null, scene: null, query: "", paths: new Map(), centers: new Map(), labels: new Map(), svg: null, overlay: null, wideBox: null, liveTimer: null,
    rail: [], stations: [], stationsOf: new Map(),
    view: null,  // 지도를 직접 확대·이동했을 때의 보는 범위 [x, y, w, h]. null 이면 기본 범위(전국 또는 고른 지역 둘레)
    details: new Map() };  // `${지역 id}@${계산 시각}` → 시간별 예보(받는 중이면 "loading", 못 받았으면 null)  // stations: [{name, x, y, region}], stationsOf: 지역 id → 그 지역의 역 이름들

  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const plain = (text) => String(text || "").replace(/[\s()·.]/g, "").toLowerCase();
  const stationName = (name) => String(name).replace(/^S(?=[가-힣])/, "");
  const shortName = (name) => String(name).replace(/\(.*\)/, "").replace(/(특별자치시|광역시|특별시|시|군)$/, "") || String(name);

  /** 한국 시각으로 '13일(금) 19시' — 보는 기기의 시간대와 상관없이 같게 */
  function dayHour(iso) {
    const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000);
    return { day: `${t.getUTCDate()}일(${WEEKDAY[t.getUTCDay()]})`, hour: `${String(t.getUTCHours()).padStart(2, "0")}시` };
  }
  function rangeText(from, to) {
    const a = dayHour(from), b = dayHour(to || from);
    if (!to || to === from) return `${a.day} ${a.hour} 무렵`;
    return a.day === b.day ? `${a.day} ${a.hour} ~ ${b.hour}` : `${a.day} ${a.hour} ~ ${b.day} ${b.hour}`;
  }
  const hazardName = (id) => (state.latest.hazards.find((x) => x.id === id) || {}).name || id;
  const regionOf = (id) => (id ? state.latest.regions.find((r) => r.id === id) || null : null);
  const fullName = (region) => `${region.name}${region.province ? ` · ${region.province}` : ""}`;

  // ------------------------------------------------------------ 지역 찾기

  const zoneNames = (r) => (r.zones || []).map((code) => (state.zones[code] || {}).n || "").join("");
  const nameHit = (r, q) => plain(`${r.name}${r.province || ""}${zoneNames(r)}`).includes(q);
  /** 그 지역 안의 역 가운데 찾는 글자가 든 역 이름('순천역'처럼 '역'을 붙여 적어도 된다). 이름이 똑같은 역을 먼저. */
  function stationHit(r, q) {
    const word = q.length > 1 && q.endsWith("역") ? q.slice(0, -1) : q;
    const names = state.stationsOf.get(r.id) || [];
    return names.find((name) => plain(name) === word) || names.find((name) => plain(name).includes(word)) || null;
  }

  /** 지역 이름·도 이름·특보구역 이름, 그리고 그 지역 안의 역 이름 어디에든 들어 있으면 찾는다. 이름이 그 글자로 시작하는 지역을 앞에 둔다. */
  function matches() {
    const q = plain(state.query);
    if (!q) return [];
    const score = (r) => (plain(r.name).startsWith(q) ? 3 : nameHit(r, q) ? 2 : plain(stationHit(r, q) || "") === (q.endsWith("역") && q.length > 1 ? q.slice(0, -1) : q) ? 1 : 0);
    return state.latest.regions.filter((r) => nameHit(r, q) || stationHit(r, q)).sort((a, b) => score(b) - score(a));
  }

  function renderMatches() {
    const box = clear($("matches")), found = matches(), q = plain(state.query);
    const viaStation = (r) => { const name = nameHit(r, q) ? null : stationHit(r, q); return name ? h("small", null, ` ${name}역`) : null; };  // 역 이름으로 찾았으면 어느 역인지 적는다
    if (!plain(state.query)) return;
    if (!found.length) {
      box.append(h("p", { class: "finder-none", text: `‘${state.query.trim()}’(으)로 찾은 지역이 없습니다. 시·군이나 역 이름으로 입력해 보세요(예: 순천, 파주, 오송역).` }));
      return;
    }
    put(box, found.slice(0, MAX_MATCHES).map((r) => h("button", { class: "chip", type: "button", "aria-pressed": String(r.id === state.region), onclick: () => pick(r.id) }, fullName(r), viaStation(r))),
      found.length > MAX_MATCHES ? h("span", { class: "hint", text: `외 ${found.length - MAX_MATCHES}곳 — 이름을 더 입력하세요` }) : null);
  }

  /** 지역을 고른다(null 이면 전국으로 돌아간다). */
  function pick(id, fromMap) {
    state.region = regionOf(id) ? id : null;
    state.view = null;  // 지역을 고르거나 전국으로 돌아가면 지도도 기본 범위로
    try { if (state.region) localStorage.setItem(STORE_KEY, state.region); else localStorage.removeItem(STORE_KEY); } catch (_) { /* 저장소를 못 써도 화면은 동작한다 */ }
    state.query = ""; $("q").value = "";
    render();
    // 지도에서 골랐는데 가능성 칸이 화면 위로 밀려나 있으면 보이게 올려 준다
    if (fromMap && $("result").getBoundingClientRect().top < 0) $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------ 특보 종류

  const levelRank = (hazard) => ((state.latest.levels || {})[hazard.level] || {}).rank || 0;
  const typeName = (id) => ((state.latest.types || []).find((t) => t.id === id) || {}).name || id;
  /** 한 지역의 한 종류: 단계(주의보·경보…)별 가능성과 그 가운데 대표(등급 높은 것 → % 높은 것 → 낮은 단계). 값이 하나도 없으면 null. */
  function typeSummary(region, typeId) {
    const rows = state.latest.hazards.filter((hazard) => hazard.type === typeId).map((hazard) => ({ hazard, sum: region.hazards[hazard.id] })).filter((x) => x.sum)
      .sort((a, b) => levelRank(a.hazard) - levelRank(b.hazard));
    if (!rows.length) return null;
    const best = [...rows].sort((a, b) => RANK[b.sum.g] - RANK[a.sum.g] || (b.sum.p || 0) - (a.sum.p || 0) || levelRank(a.hazard) - levelRank(b.hazard))[0];
    return { type: typeId, rows, best, grade: best.sum.g, p: best.sum.p };
  }
  /** 눈여겨볼 것인가: 발효 중·예비특보이거나 가능성이 0보다 크다 */
  const shown = (sm) => Boolean(sm && (sm.grade === "active" || sm.best.sum.official || (sm.p || 0) > 0));
  /** 그 지역의 종류별 요약 — 등급 높은 순, 같으면 % 높은 순 */
  const summariesOf = (region) => (state.latest.types || []).map((t) => typeSummary(region, t.id)).filter(Boolean).sort((a, b) => RANK[b.grade] - RANK[a.grade] || (b.p || 0) - (a.p || 0));
  /** 지도와 바탕 하늘에 쓰는 등급: 종류를 골랐으면 그 종류의 등급, 아니면 그 지역에서 가장 높은 것 */
  function gradeOn(region) {
    if (!region) return "low";
    if (!state.type) return region.top ? region.top.grade : "low";
    const sm = typeSummary(region, state.type);
    return sm ? sm.grade : "low";
  }

  /** 특보 종류를 고른다(null 이면 전체). 고른 종류는 다음에 열 때도 기억한다. */
  function setType(id) {
    state.type = id && (state.latest.types || []).some((t) => t.id === id) ? id : null;
    try { if (state.type) localStorage.setItem(TYPE_KEY, state.type); else localStorage.removeItem(TYPE_KEY); } catch (_) { /* 저장소를 못 써도 화면은 동작한다 */ }
    render();
  }

  /** 종류 고르기: 전체 + 종류별(모두 한눈에 보이게 줄을 바꿔 늘어놓는다). 단추마다 지금 보는 범위(고른 지역, 없으면 전국에서 가장 높은 곳)의 그 종류 등급을 점으로 찍는다. */
  function renderTypes() {
    const box = $("types"), region = regionOf(state.region);
    const worst = (typeId) => { let grade = "low"; for (const r of region ? [region] : state.latest.regions) { const sm = typeSummary(r, typeId); if (sm && RANK[sm.grade] > RANK[grade]) grade = sm.grade; } return grade; };
    const chip = (id, name) => {
      const grade = id ? worst(id) : "low";
      return h("button", { type: "button", class: "type-chip", "aria-pressed": String((state.type || null) === id), onclick: () => setType(id) }, Icons.hazard(id || "all"), h("span", { text: name }),
        RANK[grade] > 0 ? [h("i", { class: `type-dot g-${grade}`, "aria-hidden": "true" }), h("span", { class: "sr-only", text: ` ${GRADE[grade]}` })] : null);
    };
    put(clear(box), chip(null, "전체"), (state.latest.types || []).map((t) => chip(t.id, t.name)));
  }

  // ------------------------------------------------------------ 가능성

  const pinIcon = () => s("svg", { class: "pin", viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M12 21.5s7-6.300 7-11.8A7 7 0 0 0 5 9.700c0 5.500 7 11.800 7 11.800Z" }), s("circle", { cx: "12", cy: "9.700", r: "2.600" }));
  const gradePill = (grade) => h("span", { class: `pill g-${grade}`, text: GRADE[grade] || GRADE.unknown });
  const topText = (top) => `${hazardName(top.hazard)} ${top.grade === "active" ? "발효 중" : `${top.p == null ? "" : `${top.p}% `}${GRADE[top.grade]}`}`;

  /** 가능성 막대: 낮음·주의·높음 구간 위에 지금 값만큼 채운다. 구간의 경계는 받아 온 등급 기준(grades)에서 읽는다. */
  function meter(p, grade, small) {
    const g = state.latest.grades || {}, caution = Number(g.caution_min) || 30, high = Number(g.high_min) || 60, v = Math.max(0, Math.min(100, p || 0));
    return h("span", { class: `gauge${small ? " small" : ""} g-${grade}`, style: `--v:${v}`, role: "img", "aria-label": `가능성 ${p == null ? "확인 불가" : `${p}%`} · ${GRADE[grade] || ""}` },
      h("span", { class: "gauge-zone z-low", style: `flex-grow:${caution}` }), h("span", { class: "gauge-zone z-caution", style: `flex-grow:${high - caution}` }), h("span", { class: "gauge-zone z-high", style: `flex-grow:${100 - high}` }),
      h("span", { class: "gauge-fill" }), small ? null : h("span", { class: "gauge-knob" }));
  }

  /** 가능성의 머리: 무슨 특보인지(그림·이름)를 먼저, 그 아래에 숫자·등급·막대·예상 시간대. */
  function hero(sm) {
    const { hazard, sum } = sm.best, active = sum.g === "active";
    const when = active ? (sum.official_from ? `${rangeText(sum.official_from)} 발효` : null) : sum.from ? `${rangeText(sum.from, sum.to)} 기준 도달 예상` : null;
    return h("div", { class: `hero g-${sum.g}` },
      h("div", { class: "hero-head" }, h("span", { class: "hero-icon" }, Icons.hazard(sm.type, { live: true })),
        h("p", { class: "hero-name" }, h("b", { text: hazard.name }), h("span", { text: active ? "기상청 특보" : "가능성" }))),
      active ? h("p", { class: "hero-value word" }, h("span", { class: "hero-num", text: "발효 중" }))
        : h("p", { class: "hero-value" }, h("span", { class: "hero-num", "data-count": sum.p == null ? null : String(sum.p), text: sum.p == null ? "–" : String(sum.p) }), sum.p != null ? h("small", null, "%") : null, gradePill(sum.g)),
      active || sum.p == null ? null : meter(sum.p, sum.g),
      when ? h("p", { class: "when", text: when }) : null,
      // 호우: 주변 지역의 예보까지 보고 올린 가능성이면 그렇게 적는다(teukbo/area.py)
      !active && sum.area != null ? h("p", { class: "basis", text: sum.near ? `주변 지역(${sum.near})의 큰비 예보까지 반영한 값입니다.` : "이 지역과 주변의 큰비 예보를 반영한 값입니다." }) : null,
      !active && sum.p == null && hazard.note ? h("p", { class: "basis", text: hazard.note }) : null);
  }

  /** 한 줄: 그림 · 이름 · 작은 막대 · % · 등급. onpick 을 주면 누를 수 있다(그 종류만 보기). */
  function hazardRow(typeId, hazard, sum, onpick) {
    const active = sum.g === "active";
    const body = [Icons.hazard(typeId), h("span", { class: "name", text: hazard.name }), active || sum.p == null ? h("span", { class: "gauge-gap" }) : meter(sum.p, sum.g, true),
      h("b", { text: active ? "" : sum.p == null ? "–" : `${sum.p}%` }), gradePill(sum.g)];
    return h("li", null, onpick ? h("button", { type: "button", class: "hz-row", onclick: onpick }, body) : h("div", { class: "hz-row" }, body));
  }

  function renderRegion(region) {
    const all = summariesOf(region), official = region.official || {};
    const lead = state.type ? typeSummary(region, state.type) : all.find(shown) || null;
    const card = h("div", { class: `card result g-${lead ? lead.grade : "low"}` });
    card.append(h("p", { class: "where" }, pinIcon(), h("b", null, region.name), region.province ? h("small", null, region.province) : null));

    if (lead) card.append(hero(lead));
    else {
      put(card, h("div", { class: "hero g-low" }, h("div", { class: "hero-head" }, h("span", { class: "hero-icon" }, Icons.hazard("all", { live: true })), h("p", { class: "hero-name" }, h("b", { text: "기상특보" }), h("span", { text: "가능성" }))),
        h("p", { class: "hero-value word" }, h("span", { class: "hero-num", text: "낮음" })),
        h("p", { class: "when", text: `앞으로 ${state.latest.horizon_hours}시간, 모든 특보 종류의 가능성이 0%입니다.` })));
    }
    card.append(h("div", { id: "nowline" }));  // 날씨 한 줄: 시간별 예보를 받으면 채운다(fillWeather)

    // 공식 특보·예비특보(있으면 가능성보다 이것이 우선이다)
    const lines = [];
    for (const item of official.active || []) lines.push(h("li", null, h("span", { class: "pill g-active", text: "발효 중" }), ` ${item.name}`));
    for (const item of official.preliminary || []) lines.push(h("li", null, h("span", { class: "pill g-pre", text: "예비특보" }), ` ${item.name}${item.expected ? ` · ${item.expected}` : ""}`));
    if (lines.length) card.append(h("ul", { class: "official-lines", "aria-label": "기상청 공식 특보" }, lines));
    else if (official.available === false) card.append(h("p", { class: "basis", text: "기상청 공식 특보는 지금 확인하지 못했습니다." }));

    if (state.type && lead) {  // 고른 종류의 다른 단계(경보 등)
      const rest = lead.rows.filter((row) => row !== lead.best);
      if (rest.length) card.append(h("ul", { class: "others", "aria-label": `${typeName(state.type)}의 다른 단계` }, rest.map((row) => hazardRow(lead.type, row.hazard, row.sum))));
    } else if (!state.type) {  // 다른 종류 — 누르면 그 종류만 본다
      const rest = all.filter((sm) => shown(sm) && sm !== lead);
      if (rest.length) {
        put(card, h("ul", { class: "others", "aria-label": "다른 특보 종류" }, rest.slice(0, MAX_OTHERS).map((sm) => hazardRow(sm.type, sm.best.hazard, sm.best.sum, () => setType(sm.type)))),
          rest.length > MAX_OTHERS ? h("p", { class: "basis", text: `외 ${rest.length - MAX_OTHERS}종` }) : null);
      }
    }
    const models = (region.models || []).length;
    const evidence = lead && lead.grade !== "active" ? proofLine(lead.best.hazard, lead.p) : null;
    if (evidence) card.append(h("p", { class: "evidence", text: evidence }));
    card.append(h("p", { class: "basis", text: `앞으로 ${state.latest.horizon_hours}시간${models ? ` · 예보 ${models}개 비교` : ""} · 참고값` }));
    const here = state.stationsOf.get(region.id) || [];
    if (here.length) card.append(h("p", { class: "stations-line" }, h("span", { class: "map-station key", "aria-hidden": "true" }), `이 지역의 역: ${here.slice(0, MAX_STATIONS).join(", ")}${here.length > MAX_STATIONS ? ` 외 ${here.length - MAX_STATIONS}곳` : ""}`));
    return [card, h("div", { id: "work" }), h("div", { id: "weather" })];  // 작업 기준·날씨 칸도 시간별 예보를 받으면 채운다
  }

  /** 지역을 고르지 않았을 때: 전국에서 지금 눈여겨볼 곳(종류를 골랐으면 그 종류만) */
  function renderNation() {
    const regions = state.latest.regions, types = state.latest.types || [];
    const tallyOf = (typeId) => { const c = { active: 0, high: 0, caution: 0 }; for (const r of regions) { const g = typeId ? (typeSummary(r, typeId) || {}).grade : r.top && r.top.grade; if (c[g] != null) c[g] += 1; } return c; };
    const count = tallyOf(state.type), worst = count.active ? "active" : count.high ? "high" : count.caution ? "caution" : "low";
    const parts = [["active", "발효 중"], ["high", "높음"], ["caution", "주의"]];
    const tallyText = (c) => parts.filter(([grade]) => c[grade]).map(([grade, label]) => `${label} ${c[grade]}`).join(" · ");
    const card = h("div", { class: `card result nation g-${worst}` });
    card.append(h("p", { class: "where" }, pinIcon(), h("b", null, "전국"), h("small", null, `${regions.length}개 지역`)));
    const head = h("div", { class: "hero-head" }, h("span", { class: "hero-icon" }, Icons.hazard(state.type || "all", { live: true })),
      h("p", { class: "hero-name" }, h("b", { text: state.type ? `${typeName(state.type)} 특보` : "기상특보" }), h("span", { text: "가능성이 있는 지역" })));
    if (worst === "low") {
      put(card, h("div", { class: "hero g-low" }, head, h("p", { class: "hero-value word" }, h("span", { class: "hero-num", text: "없음" })),
        h("p", { class: "when", text: `앞으로 ${state.latest.horizon_hours}시간, 전국 모든 지역의 ${state.type ? `${typeName(state.type)} 특보 ` : "특보 "}가능성이 낮습니다.` })));
    } else {
      put(card, h("div", { class: `hero g-${worst}` }, head, h("p", { class: "tally" }, parts.filter(([grade]) => count[grade]).map(([grade, label]) => h("span", null, gradePill(grade), h("b", { "data-count": String(count[grade]), text: ` ${count[grade]}` }), "곳")))));
      if (!state.type) {  // 종류별로 몇 곳인지 — 누르면 그 종류만 본다
        const byType = types.map((t) => ({ t, c: tallyOf(t.id) })).filter((x) => x.c.active + x.c.high + x.c.caution > 0).sort((a, b) => b.c.active - a.c.active || b.c.high - a.c.high || b.c.caution - a.c.caution);
        card.append(h("ul", { class: "others by-type", "aria-label": "특보 종류별" }, byType.map(({ t, c }) => h("li", null, h("button", { type: "button", class: "hz-row", onclick: () => setType(t.id) }, Icons.hazard(t.id), h("span", { class: "name", text: t.name }), h("span", { class: "counts", text: tallyText(c) }))))));
      }
      const leadOf = (r) => { if (state.type) return typeSummary(r, state.type); const hz = r.top && state.latest.hazards.find((x) => x.id === r.top.hazard); return hz ? typeSummary(r, hz.type) : null; };
      const notable = regions.map((r) => ({ r, sm: leadOf(r) })).filter((x) => x.sm && RANK[x.sm.grade] >= RANK.caution).sort((a, b) => RANK[b.sm.grade] - RANK[a.sm.grade] || (b.sm.p || 0) - (a.sm.p || 0));
      put(card, h("ul", { class: "others notable", "aria-label": "눈여겨볼 지역" }, notable.slice(0, MAX_NOTABLE).map(({ r, sm }) => h("li", null, h("button", { type: "button", class: "hz-row", onclick: () => pick(r.id) },
        Icons.hazard(sm.type), h("span", { class: "name" }, fullName(r), h("small", { text: sm.best.hazard.name })), h("b", { text: sm.grade === "active" || sm.p == null ? "" : `${sm.p}%` }), gradePill(sm.grade))))),
        notable.length > MAX_NOTABLE ? h("p", { class: "basis", text: `외 ${notable.length - MAX_NOTABLE}곳 — 지도에서 색이 칠해진 곳입니다` }) : null);
    }
    card.append(h("p", { class: "basis", text: "지역 이름을 입력하거나 지도에서 눌러 보세요." }));
    return card;
  }

  function renderResult() {
    const region = regionOf(state.region), box = clear($("result"));
    put(box, region ? renderRegion(region) : renderNation());
    if (state.fresh) box.firstChild.classList.add("enter");
    if (region) fillWeather(region, state.fresh);
    paintSky();
    if (state.fresh) countUp(box);
  }

  /** 숫자가 0에서 올라오게(지역이나 종류를 바꾼 첫 그림에서만). 움직임 줄이기 설정이면 바로 적는다. */
  function countUp(root) {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    for (const node of root.querySelectorAll("[data-count]")) {
      const end = Number(node.dataset.count), lead = node.textContent.startsWith(" ") ? " " : "", t0 = performance.now();
      if (!(end > 0)) continue;
      const step = (now) => { if (!node.isConnected) return; const k = Math.min(1, (now - t0) / COUNT_MS); node.textContent = `${lead}${Math.round(end * (1 - (1 - k) ** 3))}`; if (k < 1) requestAnimationFrame(step); };
      node.textContent = `${lead}0`;
      requestAnimationFrame(step);
    }
  }


  // ------------------------------------------------------------ 날씨(앞으로 48시간)

  /** 고른 지역의 시간별 예보를 받아 둔다. 처음이면 받기 시작하고, 받으면 화면을 다시 그린다. */
  function detailOf(region) {
    const key = `${region.id}@${state.base.generated_at}`;
    if (state.details.has(key)) return state.details.get(key);
    state.details.set(key, "loading");
    getJson(`data/detail/${encodeURIComponent(region.id)}.json`).then((doc) => doc, () => null).then((doc) => {
      state.details.set(key, doc && Array.isArray(doc.times) && doc.series ? doc : null);
      if (state.region === region.id) { fillWeather(region, true); paintSky(); }  // 가능성의 머리는 그대로 두고 날씨에 기대는 칸만 채운다
    });
    return "loading";
  }

  const median = (values) => { const v = values.filter((x) => x != null && Number.isFinite(x)).sort((a, b) => a - b); return v.length ? (v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2) : null; };
  /** '오늘 18시' '내일 06시' — 한국 시각 기준 */
  function whenText(iso) {
    const day = (t) => Math.floor((new Date(t).getTime() + 9 * 3600 * 1000) / 86400000);
    const diff = day(iso) - day(Date.now()), t = dayHour(iso);
    return `${diff === 0 ? "오늘" : diff === 1 ? "내일" : diff === 2 ? "모레" : t.day} ${t.hour}`;
  }

  /** 시간별 예보(모델별) → 날씨 요약. 값은 모델들의 중간값이고, 비·눈은 몇 개 모델이 예상하는지도 센다.
   *  rain_3h 는 '그 시각까지 3시간 동안 내린 양'이라, 3시간 간격으로 집으면 겹치지 않는 시간대별 강수량이 된다.
   *  '지금'은 계산한 시각이 아니라 보는 시각이다: 계산이 몇 시간 전 것이어도 이미 지나간 시간대는 빼고 남은 예보만 요약한다
   *  (지나간 '19시부터 비'를 그대로 보여 주지 않으려고). 예보 기간이 모두 지났으면 아무것도 싣지 않는다. */
  function weatherOf(doc) {
    const times = doc.times, series = doc.series || {}, n = times.length;
    const ahead = (Date.now() - new Date(times[0]).getTime()) / 3600000;  // 자료의 첫 시각에서 지금까지 흐른 시간
    const at = Math.max(0, Math.min(n - 1, Math.round(ahead)));  // 지금에 가장 가까운 시각
    const from = Math.max(1, at);  // 요약에 넣는 첫 시각
    const out = { blocks: [], rain: null, snow: null, tempMin: null, tempMax: null, wind: null, gust: null, now: null, until: times[n - 1], times, from, feel: null, feelMin: null, feelMax: null, feelNow: null };
    if (ahead >= n - 1) return out;
    const models = (key) => Object.values(series[key] || {}).filter((row) => Array.isArray(row) && row.some((v, i) => i >= from && v != null));
    const mid = (key) => { const rows = models(key); return rows.length ? times.map((_, i) => median(rows.map((row) => row[i]))) : null; };
    const peak = (row, sign) => { let best = null; (row || []).forEach((v, i) => { if (i >= from && v != null && (best == null || sign * v > sign * row[best])) best = i; }); return best == null ? null : { v: row[best], t: times[best] }; };

    const rainRows = models("i:rain_3h");
    if (rainRows.length) {
      const ends = [];  // 아직 끝나지 않은 3시간대의 끝 시각(번호)
      for (let i = 3; i < n; i += 3) if (i > ahead) ends.push(i);
      for (const i of ends) out.blocks.push({ at: i - 3, from: times[i - 3], to: times[i], mm: median(rainRows.map((row) => row[i])), temp: null });
      const totals = rainRows.map((row) => ends.reduce((sum, i) => sum + (row[i] || 0), 0));
      const wet = rainRows.map((row, k) => (totals[k] >= RAIN_MODEL_MM ? row.findIndex((v, i) => i >= from && v != null && v >= RAIN_START_MM) : -1)).filter((i) => i > 0).sort((x, y) => x - y);
      const start = wet.length ? wet[Math.floor((wet.length - 1) / 2)] : null;
      // ongoing: 지금에 가장 가까운 시각의 3시간 강수량이 이미 비로 잡혀 있다 = 비가 오고 있거나 막 지나간 시간대
      out.rain = { models: rainRows.length, wet: wet.length, start: start == null ? null : times[start], ongoing: start != null && start <= at, min: Math.min(...totals), max: Math.max(...totals) };
    }
    const snowRows = models("i:snow_24h"), snowMax = snowRows.map((row) => Math.max(0, ...row.filter((v, i) => i >= from && v != null)));
    if (snowRows.length) out.snow = { models: snowRows.length, wet: snowMax.filter((v) => v >= SNOW_MIN_CM).length, max: Math.max(0, ...snowMax) };
    const temp = mid("v:temp"), wind = mid("i:wind"), gust = mid("i:gust");
    out.tempMin = peak(temp, -1); out.tempMax = peak(temp, 1); out.wind = peak(wind, 1); out.gust = peak(gust, 1);
    if (temp) {
      out.blocks.forEach((blk) => { blk.temp = temp[blk.at]; });  // 그 시간대가 시작할 때의 기온
      if (temp[at] != null) out.now = { v: temp[at], t: times[at] };
    }
    // 체감온도(기상청 식: 여름철은 기온·습도, 겨울철은 기온·바람 — teukbo/indicators.py 가 계산해 싣는다). 없으면 그 줄을 뺀다
    const feel = mid("v:feel");
    if (feel) {
      out.feel = feel; out.feelMin = peak(feel, -1); out.feelMax = peak(feel, 1);
      if (feel[at] != null) out.feelNow = feel[at];
      out.blocks.forEach((blk) => { blk.feel = feel[blk.at]; });  // 그 시간대가 시작할 때의 체감온도
    }
    return out;
  }

  const mm = (v) => (v < 1 ? (v < 0.05 ? "0" : v.toFixed(1)) : String(Math.round(v)));

  /** 비 한 줄. detail: 날씨 칸('비' 이름표 옆)에 쓰는 꼴 — 예보가 끝나는 시각까지의 양을 덧붙인다. */
  const rainText = (w, detail) => {
    const r = w.rain;
    const amount = !detail || r.max < 0.5 ? "" : ` · ${whenText(w.until)}까지 ${mm(r.min) === mm(r.max) ? `${mm(r.max)}mm` : `${mm(r.min)}~${mm(r.max)}mm`}`;
    if (!r.wet) return "비 소식 없음";
    if (r.wet * 2 < r.models) return `비가 올 수도 있음${amount}`;
    if (r.ongoing) return `${detail ? "지금 오는 시간대" : "지금 비 예보"}${amount}`;
    return `${whenText(r.start)} 무렵부터${detail ? "" : " 비"}${amount}`;
  };
  /** 가능성 아래에 적는 날씨 한 줄: '지금 14° · 최고 17° / 최저 13° · 오늘 17시 무렵부터 비' */
  function nowLine(region) {
    const doc = detailOf(region);
    if (!doc || doc === "loading") return null;
    const w = weatherOf(doc), parts = [];
    if (w.now) parts.push(`지금 ${Math.round(w.now.v)}°${w.feelNow != null && Math.abs(w.feelNow - w.now.v) >= FEEL_GAP ? ` (체감 ${Math.round(w.feelNow)}°)` : ""}`);
    if (w.tempMin && w.tempMax) parts.push(`최고 ${Math.round(w.tempMax.v)}° / 최저 ${Math.round(w.tempMin.v)}°`);
    if (!parts.length && !w.rain) return null;
    return h("div", { class: "nowline" }, parts.length ? h("p", { text: parts.join(" · ") }) : null,
      w.rain ? h("p", { class: "rainline" }, w.rain.wet ? dropIcon(3) : null, rainText(w, false)) : null);
  }

  const dropIcon = (level) => s("svg", { class: `wx-icon rain l${level}`, viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M12 3.2c3.2 4.3 5.3 7.2 5.3 10.2a5.3 5.3 0 0 1-10.6 0c0-3 2.1-5.900 5.3-10.2Z" }));
  const STRIP_COL = 56, STRIP_CURVE = 52;  // 시간대 한 칸의 너비, 기온 곡선 칸의 높이(px) — style.css 의 .strip-col · .strip-temp 과 같게

  /** 시간대별(3시간씩) 날씨 줄: 시각 · 비(아이콘과 mm) · 기온 곡선. 옆으로 밀어 48시간을 본다.
   *  하늘 상태(구름의 양)는 받는 자료에 없어 그리지 않는다 — 비가 없는 시간대는 아이콘을 비워 둔다. */
  function hourStrip(w) {
    const cols = w.blocks, temps = cols.map((b) => b.temp).filter((v) => v != null);
    const lo = Math.min(...temps), span = Math.max(2, Math.max(...temps) - lo);
    const y = (v) => 24 + (1 - (v - lo) / span) * (STRIP_CURVE - 34);  // 곡선 칸 안에서의 높이(위에서부터 px)
    const points = cols.map((b, k) => (b.temp == null ? null : `${k * STRIP_COL + STRIP_COL / 2},${y(b.temp).toFixed(1)}`)).filter(Boolean).join(" ");
    let lastDay = "";
    const columns = cols.map((b) => {
      const [day, hour] = whenText(b.from).split(" "), value = b.mm || 0, newDay = day !== lastDay;
      lastDay = day;
      // 체감온도는 기온과 눈에 띄게 다를 때만 기온 아래에 작게 적는다(같을 때까지 적으면 줄만 늘어난다)
      const feels = b.temp != null && b.feel != null && Math.abs(b.feel - b.temp) >= FEEL_GAP ? Math.round(b.feel) : null;
      return h("div", { class: "strip-col", title: `${day} ${hour}부터 3시간: 비 ${mm(value)}mm${b.temp == null ? "" : `, 기온 ${Math.round(b.temp)}℃`}${feels == null ? "" : `, 체감 ${feels}℃`}` },
        h("span", { class: "strip-day", text: newDay ? day : "" }),
        h("span", { class: "strip-hour", text: hour }),
        h("span", { class: "strip-icon" }, value >= 0.1 ? dropIcon(value >= 5 ? 3 : value >= 1 ? 2 : 1) : null),
        h("span", { class: "strip-mm", text: value >= 0.1 ? `${mm(value)}mm` : "" }),
        h("span", { class: "strip-temp" }, b.temp == null ? null : [h("span", { class: "strip-deg", style: `top:${(y(b.temp) - 22).toFixed(1)}px`, text: `${Math.round(b.temp)}°` }), h("span", { class: "strip-dot", style: `top:${y(b.temp).toFixed(1)}px` })]),
        h("span", { class: "strip-feel", text: feels == null ? "" : `체감 ${feels}°` }));
    });
    const anyFeel = cols.some((b) => b.temp != null && b.feel != null && Math.abs(b.feel - b.temp) >= FEEL_GAP);
    const wet = cols.filter((b) => (b.mm || 0) >= 0.1);
    const label = `시간대별 날씨(3시간씩). ${wet.length ? `비: ${wet.map((b) => `${whenText(b.from)}부터 ${mm(b.mm)}mm`).join(", ")}` : "비 없음"}. 기온 ${cols.filter((b) => b.temp != null).map((b) => `${whenText(b.from)} ${Math.round(b.temp)}도`).join(", ")}`;
    return h("div", { class: `strip${anyFeel ? " has-feel" : ""}`, role: "img", "aria-label": label, tabindex: "0" },
      h("div", { class: "strip-inner", style: `width:${cols.length * STRIP_COL}px` },
        points ? s("svg", { class: "strip-line", width: String(cols.length * STRIP_COL), height: String(STRIP_CURVE), viewBox: `0 0 ${cols.length * STRIP_COL} ${STRIP_CURVE}`, "aria-hidden": "true" }, s("polyline", { points })) : null,
        columns));
  }

  /** 날씨 카드: 비·눈·기온·바람 한 줄씩과 시간대별 줄 */
  function renderWeather(region) {
    const doc = detailOf(region);
    if (!doc) return null;
    const box = h("section", { class: "glass weather", "aria-label": "날씨" });
    if (doc === "loading") return put(box, h("p", { class: "glass-title" }, "날씨"), h("div", { class: "skeleton", role: "status", "aria-label": "날씨를 불러오는 중" }, h("span"), h("span"), h("span")));
    const w = weatherOf(doc), lines = [];
    box.append(h("p", { class: "glass-title" }, "날씨", h("small", null, ` 지금부터 ${whenText(w.until)}까지 · 예보들의 중간값`)));
    if (w.rain) lines.push(h("li", null, h("span", { class: "name", text: "비" }), h("b", { text: rainText(w, true) }), w.rain.wet ? h("small", null, `예보 ${w.rain.models}개 중 ${w.rain.wet}개가 비 예상`) : null));
    if (w.snow && w.snow.wet) lines.push(h("li", null, h("span", { class: "name", text: "눈" }), h("b", { text: `최대 ${mm(w.snow.max)}cm` }), h("small", null, `예보 ${w.snow.models}개 중 ${w.snow.wet}개가 눈 예상(24시간 신적설)`)));
    if (w.tempMin && w.tempMax) lines.push(h("li", null, h("span", { class: "name", text: "기온" }), h("b", { text: `${Math.round(w.tempMin.v)}~${Math.round(w.tempMax.v)}℃` }), h("small", null, `최저 ${whenText(w.tempMin.t)} · 최고 ${whenText(w.tempMax.t)}`)));
    if (w.feelMin && w.feelMax) lines.push(h("li", null, h("span", { class: "name", text: "체감" }), h("b", { text: `${Math.round(w.feelMin.v)}~${Math.round(w.feelMax.v)}℃` }), h("small", null, `가장 낮을 때 ${whenText(w.feelMin.t)} · 가장 높을 때 ${whenText(w.feelMax.t)}`)));
    if (w.wind) lines.push(h("li", null, h("span", { class: "name", text: "바람" }), h("b", { text: `최대 ${Math.round(w.wind.v)}m/s` }), h("small", null, `${whenText(w.wind.t)}${w.gust ? ` · 순간 ${Math.round(w.gust.v)}m/s` : ""}`)));
    if (!lines.length) return null;
    if (w.blocks.length) box.append(hourStrip(w));
    box.append(h("ul", { class: "weather-lines" }, lines));
    return box;
  }

  // ------------------------------------------------------------ 작업 기준(폭염·한파)

  /** 연속한 시각들을 '내일 12시~17시' 꼴로 묶는다(많으면 앞의 둘만 적고 '외'). */
  function hourRanges(times, picked) {
    const runs = [];
    for (const i of picked) { const last = runs[runs.length - 1]; if (last && i === last[1] + 1) last[1] = i; else runs.push([i, i]); }
    const text = ([a, b]) => { const from = whenText(times[a]), to = whenText(times[b]); return a === b ? from : from.split(" ")[0] === to.split(" ")[0] ? `${from}~${to.split(" ")[1]}` : `${from}~${to}`; };
    return runs.slice(0, 2).map(text).join(", ") + (runs.length > 2 ? " 외" : "");
  }

  /** 폭염·한파 때의 작업 기준. 기준(단계·문구·출처)은 모두 받아 온 자료(work_rules.json)에서 읽는다 — 화면 코드에 수치를 적지 않는다.
   *  단계는 두 가지 꼴이다: 체감온도가 얼마 이상/이하(min·max)이면 해당하는 것, 특보 단계(level)가 발효·예상되면 해당하는 것.
   *  한파·폭염을 골랐으면 그 기준을 펼쳐서, 전체를 볼 때는 제철인 기준만 접어서 보여 준다. */
  function renderWork(region) {
    const rules = state.work, doc = detailOf(region);
    if (!rules || !Array.isArray(rules.guides)) return null;
    const month = new Date(Date.now() + 9 * 3600 * 1000).getUTCMonth() + 1;
    const guide = state.type ? rules.guides.find((g) => g.type === state.type) : rules.guides.find((g) => (g.months || []).includes(month));
    if (!guide) return null;
    const w = doc && doc !== "loading" ? weatherOf(doc) : null, metric = guide.metric || {}, unit = metric.unit || "℃";
    const extreme = w ? (metric.stat === "min" ? w.feelMin : w.feelMax) : null, sm = typeSummary(region, guide.type);
    const kindName = (kind) => (rules.kinds || {})[kind] || kind || "";
    let reached = null;  // 지금 예보로 해당하는 가장 높은 단계
    const stages = (guide.stages || []).map((stage) => {
      let state_ = "", note = "";
      if (stage.level) {
        const row = sm && sm.rows.find((x) => x.hazard.level === stage.level), sum = row && row.sum;
        if (sum && sum.g === "active") { state_ = "on"; note = "발효 중"; }
        else if (sum && sum.official) { state_ = "on"; note = "예비특보"; }
        else if (sum && RANK[sum.g] >= RANK.caution) { state_ = "near"; note = `가능성 ${sum.p == null ? "" : `${sum.p}% `}${GRADE[sum.g]}`; }
        else if (sum && sum.p != null) note = `가능성 ${sum.p}%`;
      } else if (w && w.feel) {
        const hit = [];
        w.feel.forEach((v, i) => { if (i >= w.from && v != null && ((stage.min != null && v >= stage.min) || (stage.max != null && v <= stage.max))) hit.push(i); });
        if (hit.length) { state_ = "on"; note = `해당 예상 · ${hourRanges(w.times, hit)}`; }
      }
      if (state_ === "on") reached = stage;
      const label = stage.level ? stage.name : `${metric.label || "체감온도"} ${stage.min != null ? `${stage.min}${unit} 이상` : `${stage.max}${unit} 이하`}`;
      return h("li", { class: `stage${state_ ? ` ${state_}` : ""}` },
        h("p", { class: "stage-key" }, h("b", { text: label }), stage.kind ? h("span", { class: `kind k-${stage.kind}`, text: kindName(stage.kind) }) : null, note ? h("span", { class: "stage-note", text: note }) : null),
        h("p", { class: "stage-text" }, stage.level || !stage.name ? null : h("b", { text: `${stage.name} ` }), stage.text));
    });
    const stageLabel = (stage) => (stage.level ? stage.name : `${metric.label || "체감온도"} ${stage.min != null ? `${stage.min}${unit} 이상` : `${stage.max}${unit} 이하`}${stage.name ? ` ${stage.name}` : ""}`);
    const temp = w ? (metric.stat === "min" ? w.tempMin : w.tempMax) : null;
    const status = extreme ? `${metric.stat === "min" ? "가장 낮은" : "가장 높은"} ${metric.label || "체감온도"} ${Math.round(extreme.v)}${unit} · ${whenText(extreme.t)}` : doc === "loading" ? "예보를 불러오는 중…" : "";
    const body = [
      status ? h("p", { class: "work-status" }, Icons.hazard(guide.type, { live: true }), h("span", null, h("b", { text: status }),
        h("small", null, `${temp ? `기온은 ${metric.stat === "min" ? "최저" : "최고"} ${Math.round(temp.v)}${unit} · ` : ""}${reached ? `‘${stageLabel(reached)}’ 단계에 해당` : "지금 예보로는 해당하는 단계가 없습니다"}`))) : null,
      h("ol", { class: "stages" }, stages),
      (guide.basics || []).length ? h("p", { class: "work-basics" }, h("b", { text: `${guide.basics_title || "기본 수칙"} ` }), guide.basics.join(" · ")) : null,
      // 긴 설명(유의할 점·단계별 근거·출처)은 접어 둔다
      fold(`work-more-${guide.id}`, "근거와 유의할 점",
        guide.note ? h("p", { class: "work-basics", text: guide.note }) : null,
        h("ul", { class: "fold-notes" }, (guide.stages || []).filter((stage) => stage.basis).map((stage) => h("li", null, h("b", { text: `${stageLabel(stage)}(${kindName(stage.kind)}) ` }), stage.basis))),
        h("p", { class: "work-source" }, rules.notice ? `${rules.notice} ` : null, (guide.sources || []).map((src, k) => [k ? " · " : "출처: ", src.url ? h("a", { href: src.url, rel: "noopener", text: src.name }) : src.name]), rules.checked_at ? ` (${rules.checked_at} 확인)` : null))];
    const box = h("section", { class: "glass work", "aria-label": guide.title });
    if (state.type) return put(box, h("p", { class: "glass-title" }, guide.title, guide.subtitle ? h("small", null, ` ${guide.subtitle}`) : null), body);
    // 접어 둔 채로도 지금 어느 단계인지는 제목에서 보이게
    return put(box, fold(`work-${guide.id}`, `${guide.title}${w ? ` — ${reached ? `${stageLabel(reached)} 단계` : "해당 단계 없음"}` : ""}`, body));
  }

  /** 시간별 예보에 기대는 칸(날씨 한 줄 · 작업 기준 · 날씨)을 채운다. 가능성의 머리는 건드리지 않는다. animate: 새로 나타나게. */
  function fillWeather(region, animate) {
    const slots = [["nowline", nowLine(region)], ["work", renderWork(region)], ["weather", renderWeather(region)]];
    for (const [id, node] of slots) {
      const slot = $(id);
      if (!slot) continue;
      clear(slot);
      if (node) { if (animate) node.classList.add("pop"); slot.append(node); }
    }
  }

  /** 바탕 하늘: 낮·밤(한국 시각), 지금 비가 오는 시간대면 비 하늘. 가능성이 '주의' 이상이면 위쪽에 그 색을 얹는다. */
  function paintSky() {
    const region = regionOf(state.region), hour = new Date(Date.now() + 9 * 3600 * 1000).getUTCHours();
    let sky = hour >= 6 && hour < 19 ? "day" : "night";
    const doc = region ? state.details.get(`${region.id}@${state.base.generated_at}`) : null;
    if (doc && doc !== "loading") { const first = weatherOf(doc).blocks[0]; if (first && (first.mm || 0) >= 0.5) sky = "rain"; }
    let grade = "low";
    if (region) grade = gradeOn(region);
    else for (const r of state.latest.regions) { const g = gradeOn(r); if (RANK[g] > RANK[grade]) grade = g; }
    document.body.dataset.sky = sky;
    document.body.dataset.grade = RANK[grade] > 0 ? grade : "low";
    const meta = document.querySelector('meta[name="theme-color"]:not([media])');
    if (meta) meta.setAttribute("content", { day: "#2a6bb8", night: "#101c3c", rain: "#3b4c66" }[sky]);
  }

  // ------------------------------------------------------------ 과거 검증

  /** 몇 번 가운데 몇 번인지를 %로. 0번이 아닌데 0%로, 전부가 아닌데 100%로 적지 않는다. */
  function share(hits, total) {
    if (!total) return "–";
    if (!hits) return "0%";
    const p = (hits / total) * 100;
    return p < 0.1 ? "0.1% 미만" : p < 1 ? `${p.toFixed(1)}%` : p > 99 && hits < total ? "99%" : `${Math.round(p)}%`;
  }
  const rowFor = (rows, p) => (rows || []).find((row) => p >= row.from && p <= row.to) || null;
  const solid = (row) => Boolean(row && row.cases >= MIN_PROOF_CASES && row.event_days >= MIN_PROOF_DAYS);

  /** 가능성 숫자 옆에 적는 근거 한 줄: 과거에 이 구간의 가능성이 나왔을 때 실제 특보로 이어진 비율. 근거가 모자라면 적지 않는다. */
  function proofLine(hazard, p) {
    const proof = state.proof;
    if (!proof || p == null || p <= 0) return null;
    const own = rowFor(((proof.by_type || {})[hazard.type] || {}).calibration, p), all = rowFor(proof.calibration, p);
    const row = solid(own) ? own : solid(all) ? all : null;
    if (!row) return null;
    const scope = row === own ? `${(proof.by_type[hazard.type] || {}).name || ""} 특보` : "특보";
    return `과거 ${row.from === row.to ? `${row.from}%` : `${row.from}~${row.to}%`}였을 때 ${share(row.hits, row.cases)}가 실제 ${scope}로 이어졌습니다 (${row.cases.toLocaleString("ko-KR")}번 중 ${row.hits.toLocaleString("ko-KR")}번).`;
  }

  /** 접어 두는 칸. 화면은 몇 분마다 다시 그려지므로, 펼쳐 둔 칸은 기억해 두었다가 다시 펼친다. */
  function fold(key, title, ...kids) {
    const box = h("details", { class: "fold" }, h("summary", { text: title }), h("div", { class: "fold-body" }, kids));
    box.open = state.openFolds.has(key);
    box.addEventListener("toggle", () => { if (box.open) state.openFolds.add(key); else state.openFolds.delete(key); });
    return box;
  }
  const statRow = (...stats) => h("div", { class: "stats" }, stats.map(([label, value]) => h("div", { class: "stat" }, h("p", { class: "stat-value", text: value }), h("p", { class: "stat-label", text: label }))));

  /** 과거 검증: 눈에 보이는 것은 숫자 셋과 한 줄 설명뿐. 표·방법·한계는 접어 둔다(펼치면 전과 같은 내용이 모두 있다). */
  function renderProof() {
    const proof = state.proof, section = $("proof"), box = clear($("proof-body"));
    section.hidden = !proof || !proof.period || !proof.period.replays;
    if (section.hidden) return;
    const day = (iso) => { const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000); return `${t.getUTCMonth() + 1}월 ${t.getUTCDate()}일`; };
    const count = (n) => Number(n || 0).toLocaleString("ko-KR");
    const lead = (proof.capture || {})["24"] || {}, alerts = (proof.false_alarm || {}).caution_or_higher || {}, high = (proof.false_alarm || {}).high || {};
    const right = (a) => (a.alerts || 0) - (a.false || 0);
    put(box,
      statRow(["하루 전에 잡아냄", share(lead.captured, lead.events)], ["‘주의’ 이상 맞음", share(right(alerts), alerts.alerts)], ["‘높음’ 맞음", share(right(high), high.alerts)]),
      h("p", { class: "proof-lede", text: `${day(proof.period.from)} ~ ${day(proof.period.to)}의 실제 특보 ${count(proof.events)}건과 맞춰 본 결과입니다.` }));
    const rows = (proof.calibration || []).filter((row) => row.cases);
    if (rows.length) {
      box.append(fold("buckets", "가능성 구간별로 보기", h("div", { class: "table-scroll" }, h("table", { class: "fit proof-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "표시한 가능성" }), h("th", { scope: "col", text: "횟수" }), h("th", { scope: "col" }, "실제 특보로 이어짐", h("small", null, "48시간 안")))),
        h("tbody", null, rows.map((row) => h("tr", null, h("th", { scope: "row", text: row.bucket }), h("td", { text: count(row.cases) }), h("td", null, share(row.hits, row.cases), h("small", null, `${count(row.hits)}번 · 특보일 ${count(row.event_days)}일`)))))))));
    }
    const types = Object.values(proof.by_type || {}).filter((info) => info.events).sort((a, b) => b.events - a.events);
    if (types.length) {
      box.append(fold("types", "특보 종류별로 보기", h("div", { class: "table-scroll" }, h("table", { class: "fit proof-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "특보 종류" }), h("th", { scope: "col", text: "실제 특보" }), h("th", { scope: "col" }, "하루 전 포착", h("small", null, "‘주의’ 이상")), h("th", { scope: "col", text: "‘주의’ 이상이 맞음" }))),
        h("tbody", null, types.map((info) => {
          const c = (info.capture || {})["24"] || {}, a = (info.false_alarm || {}).caution_or_higher || {};
          // 표본이 적으면(30 미만) %를 적지 않고 횟수만 적는다 — 몇 건 안 되는 비율은 믿을 숫자가 아니다
          const cell = (hits, total, none) => (!total ? h("td", null, "–", h("small", null, none))
            : total < MIN_PROOF_CASES ? h("td", null, `${count(hits)}/${count(total)}`, h("small", null, "표본 적음"))
              : h("td", null, share(hits, total), h("small", null, `${count(hits)}/${count(total)}`)));
          return h("tr", null, h("th", { scope: "row", text: info.name }),
            h("td", null, `${count(info.events)}건`, info.event_days != null ? h("small", null, `특보일 ${info.event_days}일`) : null),
            cell(c.captured, c.events, "해당 없음"), cell(right(a), a.alerts, "표시 없음"));
        }))))));
    }
    const longRain = longRainFold(state.longRain);
    if (longRain) box.append(longRain);
    box.append(fold("method", "어떻게 검증했나 · 한계", h("ul", { class: "fold-notes" },
      h("li", { text: `전국 ${count(proof.regions)}곳을 6시간마다 그때의 예보로 다시 계산해(${count(proof.period.replays)}회) 기상청의 실제 특보 기록과 맞춰 봤습니다. 실제 특보 ${count(proof.events)}건${proof.event_days != null ? ` · 특보가 시작된 날 ${count(proof.event_days)}일` : ""}.` }),
      lead.events ? h("li", { text: `하루 전에 잡아냄: 실제 특보 ${count(lead.events)}건 중 ${count(lead.captured)}건이 24시간 전에 ‘주의’ 이상이었습니다.` }) : null,
      alerts.alerts ? h("li", { text: `‘주의’ 이상 맞음: ${count(alerts.alerts)}번 표시 중 ${count(right(alerts))}번이 48시간 안에 실제 특보로 이어졌습니다. ‘높음’은 ${count(high.alerts)}번 중 ${count(right(high))}번.` }) : null,
      (proof.notes || []).map((text) => h("li", { text })),
      (proof.types_without_events || []).length ? h("li", { text: `이 기간에 실제 특보가 없어 검증하지 못한 종류: ${proof.types_without_events.join(", ")}` }) : null,
      h("li", { text: "가능성 옆의 ‘과거 …였을 때’ 한 줄은 표본 30회 이상·특보가 있었던 날 3일 이상일 때만 적습니다." }))));
  }

  /** 호우를 더 긴 기간으로 본 확인(관측 일강수량 기준). 위의 숫자와 잣대가 달라 접는 칸 하나에 따로 담고, 무엇을 센 것인지 먼저 적는다. */
  function longRainFold(doc) {
    if (!doc || !doc.period || !doc.events) return null;
    const count = (n) => Number(n || 0).toLocaleString("ko-KR");
    const [y1, m1] = String(doc.period.from).split("-").map(Number), [y2, m2] = String(doc.period.to).split("-").map(Number);
    const months = (y2 - y1) * 12 + (m2 - m1), span = `${Math.floor(months / 12) ? `${Math.floor(months / 12)}년 ` : ""}${months % 12 ? `${months % 12}개월` : ""}`.trim();
    const mm = Number(doc.heavy_mm), before = doc.before_area_score || {}, high = doc.high || {}, zero = (doc.calibration || [])[0] || {}, unseen = doc.unseen || {};
    const rows = (doc.calibration || []).filter((row) => row.cases), periods = (doc.by_period || []).filter((row) => row.events);
    return fold("long-rain", `호우는 ${span}치로도 확인`,
      h("p", { class: "proof-lede", text: `${y1}년 ${m1}월 ~ ${y2}년 ${m2}월, 관측소가 있는 ${count(doc.regions)}개 지역. 여기서 ‘큰비’는 관측소의 하루 강수량 ${mm}mm 이상입니다 — 특보 발표가 아니라 실제로 내린 비와 맞춘 것이라 위의 숫자와 잣대가 다릅니다.` }),
      statRow(["큰비를 전날 잡아냄", share(doc.day_before, doc.events)], ["‘높음’ 뒤 큰비", share(high.hits, high.alerts)], ["0% 뒤 큰비", share(zero.hits, zero.cases)]),
      rows.length ? h("div", { class: "table-scroll" }, h("table", { class: "fit proof-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "표시한 호우주의보 가능성" }), h("th", { scope: "col", text: "횟수" }), h("th", { scope: "col" }, "큰비로 이어짐", h("small", null, "이틀 안")))),
        h("tbody", null, rows.map((row) => h("tr", null, h("th", { scope: "row", text: row.bucket }), h("td", { text: count(row.cases) }), h("td", null, share(row.hits, row.cases), h("small", null, `${count(row.hits)}번`))))))) : null,
      h("ul", { class: "fold-notes" },
        h("li", { text: `매일 0시 기준으로 ${count(doc.period.replays)}회 다시 계산했습니다. 큰비 ${count(doc.events)}건(큰비가 온 날 ${count(doc.event_days)}일) 중 ${count(doc.day_before)}건이 전날 0시에 ‘주의’ 이상${before.events ? ` — 주변 지역을 보기 전 계산식으로는 ${share(before.day_before, before.events)}` : ""}. ‘높음’ ${count(high.alerts)}번 중 ${count(high.hits)}번이 이틀 안에 큰비.` }),
        (doc.notes || []).map((text) => h("li", { text })),
        unseen.events ? h("li", { text: `기준을 맞출 때 보지 않은 기간(~ ${unseen.until})만: 큰비 ${count(unseen.events)}건 중 전날 ‘주의’ 이상 ${count(unseen.day_before)}건(${share(unseen.day_before, unseen.events)})${(unseen.before_area_score || {}).events ? `, 주변 지역을 보기 전 계산식으로는 ${share(unseen.before_area_score.day_before, unseen.before_area_score.events)}` : ""}.` }) : null,
        periods.length ? h("li", { text: `기간별 전날 포착: ${periods.map((row) => `${row.name} ${share(row.day_before, row.events)}(${count(row.day_before)}/${count(row.events)})`).join(" · ")}.` }) : null));
  }

  // ------------------------------------------------------------ 도움말(? 단추)

  /** 이 화면의 등급과 기상청 특보 단계. 숫자와 발표 기준은 받아 온 자료(등급 기준 grades, 특보별 발표 기준 hazards[].official_text)에서 가져온다 —
   *  기준값을 여기에 따로 적지 않는다. 등급 옆의 '과거 …'는 과거 검증에서 그 구간이 실제 특보로 이어진 비율이다. */
  function renderHelp() {
    const box = clear($("help-body")), latest = state.latest || {}, proof = state.proof || {};
    const caution = Math.round((latest.grades || {}).caution_min || 30), high = Math.round((latest.grades || {}).high_min || 60);
    const past = (from) => { const row = (proof.calibration || []).find((r) => r.from === from && r.cases); return row ? `과거 ${share(row.hits, row.cases)}가 실제 특보로 이어짐` : null; };
    const grade = (id, range, text, note) => h("li", null, h("div", { class: "help-key" }, gradePill(id), range ? h("span", { class: "help-range", text: range }) : null),
      h("p", { class: "help-text" }, text, note ? h("small", null, note) : null));
    put(box,
      h("h3", { text: "이 화면의 등급" }),
      h("ul", { class: "help-list" },
        grade("low", `0~${caution - 1}%`, "특보 가능성이 낮습니다."),
        grade("caution", `${caution}~${high - 1}%`, "예보를 다시 확인해 둘 때입니다.", past(caution)),
        grade("high", `${high}% 이상`, "특보가 날 가능성이 큽니다.", past(high)),
        grade("active", "", "기상청 특보가 실제로 내려져 있습니다.")),
      h("p", { class: "help-note", text: "기상청이 예비특보를 내면 숫자와 관계없이 ‘높음’ 이상으로, 특보를 내면 ‘발효 중’으로 바꿔 보여 줍니다. 공식 발표가 늘 먼저입니다." }));

    const levels = Object.entries(latest.levels || {}).sort((a, b) => a[1].rank - b[1].rank);
    const meaning = { advisory: "재해가 일어날 우려가 있을 때", warning: "중대한 재해가 일어날 우려가 있을 때" };
    const used = new Set((latest.hazards || []).map((item) => item.level));
    const extra = levels.filter(([id]) => !meaning[id] && used.has(id)).map(([id, info]) => {
      const types = [...new Set((latest.hazards || []).filter((item) => item.level === id).map((item) => (latest.types || []).find((t) => t.id === item.type)).filter(Boolean).map((t) => t.name))];
      return `${types.join("·")}에는 그 위에 ${info.name}가 있습니다.`;
    });
    put(box,
      h("h3", { text: "기상청 특보 단계" }),
      h("ol", { class: "help-steps" },
        h("li", null, h("b", { text: "예비특보" }), h("span", { text: "특보를 낼 것으로 예상될 때 미리 알림" })),
        levels.filter(([id]) => meaning[id]).map(([id, info]) => h("li", null, h("b", { text: info.name }), h("span", { text: meaning[id] })))),
      extra.length ? h("p", { class: "help-note", text: extra.join(" ") }) : null);

    const groups = (latest.types || []).map((type) => ({ type, items: (latest.hazards || []).filter((item) => item.type === type.id && item.official_text).sort((a, b) => ((latest.levels || {})[a.level] || {}).rank - ((latest.levels || {})[b.level] || {}).rank) })).filter((group) => group.items.length);
    if (groups.length) {
      const source = latest.thresholds || {};
      box.append(fold("help-criteria", "특보 종류별 발표 기준",
        h("dl", { class: "help-criteria" }, groups.map((group) => [h("dt", { text: group.type.name }),
          group.items.map((item) => h("dd", null, h("b", { text: ((latest.levels || {})[item.level] || {}).name || item.name }), ` ${item.official_text}`))])),
        h("p", { class: "help-note", text: `기상법 시행령의 특보 기준${source.version ? `(${source.version} 시행)` : ""}을 옮긴 것입니다${source.checked_at ? ` · ${source.checked_at} 확인` : ""}.` })));
    }
  }
  function openHelp() {
    const dialog = $("help");
    renderHelp();
    if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); } else dialog.setAttribute("open", "");
  }
  function closeHelp() {
    const dialog = $("help");
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
  }

  // ------------------------------------------------------------ 지도

  const pathOf = (rings) => rings.map((ring) => { let d = ""; for (let i = 0; i < ring.length; i += 2) { const [x, y] = P(ring[i], ring[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return `${d}Z`; }).join("");

  const lineOf = (flat) => { let d = ""; for (let i = 0; i < flat.length; i += 2) { const [x, y] = P(flat[i], flat[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return d; };

  /** 지도는 한 번만 그린다(경계·노선은 바뀌지 않는다). 색·고른 표시·보는 범위는 paintMap 이, 이름표와 역은 layoutOverlay 가 바꾼다. */
  function drawMap() {
    const holder = clear($("map")), zones = state.zones;
    state.paths.clear(); state.centers.clear(); state.labels.clear(); state.stationsOf.clear();
    const owner = new Map();  // 특보구역 코드 → 그 구역을 쓰는 첫 지역
    for (const region of state.latest.regions) for (const code of region.zones || []) if (!owner.has(code) && zones[code]) owner.set(code, region.id);
    if (!owner.size) { state.svg = null; state.overlay = null; return; }
    const svg = s("svg", { role: "group", "aria-label": "지역별 특보 가능성 지도. 지역을 누르면 그 지역의 가능성을 보여 줍니다." });
    // 바탕: 구역 경계를 단순하게 줄이면서 생긴 구역 사이의 작은 틈으로 페이지 바탕이 비치지 않게 지도 안에 직접 깐다
    const ground = s("rect", { class: "ground", x: "-2000", y: "-2000", width: "6000", height: "6000", "aria-hidden": "true" });
    const around = s("g", { class: "around", "aria-hidden": "true" }), covered = s("g", { class: "covered" }), rails = s("g", { class: "rails", "aria-hidden": "true" });
    for (const [code, zone] of Object.entries(zones)) if (!owner.has(code)) around.append(s("path", { d: pathOf(zone.p) }));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [code, id] of owner) {
      const zone = zones[code], region = regionOf(id), [cx, cy] = P(zone.c[0], zone.c[1]);
      x0 = Math.min(x0, cx); y0 = Math.min(y0, cy); x1 = Math.max(x1, cx); y1 = Math.max(y1, cy);
      const path = s("path", { d: pathOf(zone.p), "data-region": id, tabindex: "0", role: "button", onclick: () => pick(id, true),
        onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(id, true); } } });
      covered.append(path);
      if (!state.paths.has(id)) state.paths.set(id, []);
      state.paths.get(id).push(path);
      if (!state.centers.has(id)) {  // 구역이 여럿인 지역은 첫 구역에 이름을 적는다
        state.centers.set(id, [cx, cy]);
        const single = (region.zones || []).filter((c) => zones[c]).length === 1;
        state.labels.set(id, single ? zone.n : shortName(region.name));
      }
    }
    for (const line of state.rail) for (const flat of line.paths || []) rails.append(s("path", { d: lineOf(flat), class: line.fast ? "fast" : "" }));
    state.stations = [];
    for (const st of state.stationList || []) {
      const id = owner.get(st.zone), name = stationName(st.name), [x, y] = P(st.lon, st.lat);
      state.stations.push({ name, x, y, region: id || null });
      if (!id) continue;
      if (!state.stationsOf.has(id)) state.stationsOf.set(id, []);
      if (!state.stationsOf.get(id).includes(name)) state.stationsOf.get(id).push(name);
    }
    const pad = 14;
    state.wideBox = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
    svg.append(ground, around, covered, rails);
    state.overlay = h("div", { class: "map-overlay", "aria-hidden": "true" });
    const tools = h("div", { class: "map-zoom" },
      h("button", { type: "button", "aria-label": "지도 확대", title: "확대", onclick: () => zoomBy(ZOOM_STEP) }, "+"),
      h("button", { type: "button", "aria-label": "지도 축소", title: "축소", onclick: () => zoomBy(1 / ZOOM_STEP) }, "−"));
    holder.append(svg, state.overlay, tools);
    state.svg = svg;
    bindMapGestures(svg);
    put(clear($("legend")), ["low", "caution", "high", "active"].map((grade) => h("li", null, h("span", { class: `swatch map-key g-${grade}` }), GRADE[grade])),
      state.stations.length ? h("li", null, h("span", { class: "map-station key" }), "역") : null);
  }

  /** 지역 이름표와 역(점·이름)을 지도 위에 얹는다 — 지역을 골라 가까이 볼 때만(전국 지도에는 200곳이 넘어 적지 않는다).
   *  지도 그림(SVG) 안의 글자가 아니라 보통 글자로 얹는다: 브라우저가 화면을 강제로 어둡게 바꿔도(일부 휴대폰) 글자와 바탕이 함께 바뀌어 읽힌다.
   *  겹치지 않게: 고른 지역 이름 → 다른 지역 이름 → 고른 지역의 역 이름 → 다른 역 이름 순으로, 자리가 있으면 적는다. 역의 점은 모두 찍는다. */
  function layoutOverlay() {
    const layer = state.overlay;
    if (!layer || !state.svg) return;
    clear(layer);
    const picked = state.centers.has(state.region) ? state.region : null;
    if (currentBox()[2] > LABEL_MAX_W) return;  // 넓게 볼 때는 적지 않는다
    const ctm = state.svg.getScreenCTM(), host = layer.getBoundingClientRect();
    if (!ctm || !host.width) return;
    const at = ([x, y]) => [ctm.a * x + ctm.e - host.left, ctm.d * y + ctm.f - host.top];
    const inside = ([x, y], margin) => x >= margin && y >= margin && x <= host.width - margin && y <= host.height - margin;
    const widthOf = (text, size) => [...text].reduce((w, ch) => w + (ch.charCodeAt(0) > 0x2e80 ? 1 : 0.62), 0) * size + 8;
    const taken = [[host.width - 30, 56, 60, 112]];  // 이미 차지한 자리의 [가운데 x, 가운데 y, 너비, 높이] — 처음에는 확대·축소 단추 자리
    const free = (x, y, w, hgt) => x - w / 2 >= 0 && x + w / 2 <= host.width && y - hgt / 2 >= 0 && y + hgt / 2 <= host.height
      && !taken.some((b) => Math.abs(b[0] - x) < (b[2] + w) / 2 && Math.abs(b[1] - y) < (b[3] + hgt) / 2);
    const mark = (cls, text, x, y) => h("span", { class: cls, style: `left:${x.toFixed(1)}px;top:${y.toFixed(1)}px`, text });
    const dots = [], stationNames = [], zoneNames = [];  // 쌓는 순서: 역의 점 → 역 이름 → 지역 이름(맨 위)

    const focus = picked ? at(state.centers.get(picked)) : null;
    const [px, py] = focus && inside(focus, 0) ? focus : [host.width / 2, host.height / 2];  // 가까운 것부터 적는 기준점: 고른 지역(화면 밖이면 화면 가운데)
    const near = (a, b) => Math.hypot(a[0] - px, a[1] - py) - Math.hypot(b[0] - px, b[1] - py);
    const zoneMarks = [...state.centers].map(([id, c]) => ({ id, text: state.labels.get(id), xy: at(c) })).filter((m) => m.text && (m.id === picked || inside(m.xy, 4))).sort((a, b) => near(a.xy, b.xy));
    const stops = state.stations.map((st) => ({ st, mine: st.region === picked, xy: at([st.x, st.y]) })).filter((m) => inside(m.xy, 2)).sort((a, b) => near(a.xy, b.xy));
    const zoneName = (m, always) => {
      const w = widthOf(m.text, ZONE_FONT), [x, y] = m.xy;
      if (!always && !free(x, y, w, ZONE_FONT + 6)) return;
      taken.push([x, y, w, ZONE_FONT + 6]);
      zoneNames.push(mark(m.id === picked ? "map-label picked" : "map-label", m.text, x, y));
    };
    const stationNameAt = (m) => {
      const [x, y] = m.xy, text = `${m.st.name}역`, w = widthOf(text, STATION_FONT), hgt = STATION_FONT + 5, dx = 7 + w / 2, dy = 6 + hgt / 2;
      // 이름 자리: 점의 오른쪽 → 왼쪽 → 아래 → 위 → 대각선
      const spot = [[x + dx, y], [x - dx, y], [x, y + dy], [x, y - dy], [x + dx, y + dy], [x + dx, y - dy], [x - dx, y + dy], [x - dx, y - dy]].find(([lx, ly]) => free(lx, ly, w, hgt));
      if (!spot) return false;
      taken.push([spot[0], spot[1], w, hgt]);
      stationNames.push(mark(m.mine ? "map-station-name mine" : "map-station-name", text, spot[0], spot[1]));
      return true;
    };
    // 자리 잡는 순서: 고른 지역 이름 → 고른 지역의 역(점·이름) → 다른 지역 이름 → 다른 역(점·이름).
    // 지역 이름은 다른 지역의 역 점 위에 덮어 적는다(수도권처럼 역이 촘촘한 곳에서 지역 이름이 빠지지 않게).
    const dot = (m) => {
      taken.push([m.xy[0], m.xy[1], m.mine ? 11 : 7, m.mine ? 11 : 7]);  // 역의 점 위에는 역 이름을 적지 않는다
      dots.push(mark(m.mine ? "map-station mine" : "map-station", "", m.xy[0], m.xy[1]));
    };
    zoneMarks.filter((m) => m.id === picked).forEach((m) => zoneName(m, true));
    stops.filter((m) => m.mine).forEach(dot);
    stops.filter((m) => m.mine).forEach(stationNameAt);
    zoneMarks.filter((m) => m.id !== picked).forEach((m) => zoneName(m, false));
    stops.filter((m) => !m.mine).forEach(dot);
    let others = 0;  // 다른 지역의 역 이름은 가까운 몇 곳만
    for (const m of stops) if (!m.mine && others < MAX_OTHER_STATION_NAMES && stationNameAt(m)) others += 1;
    layer.append(...dots, ...stationNames, ...zoneNames);
  }

  /** 고른 지역 둘레의 보는 범위: 이웃 지역 NEAR_COUNT 곳쯤이 들어오게(지역이 촘촘한 수도권은 좁게, 넓은 산지는 넓게). */
  function nearBox(id) {
    const [cx, cy] = state.centers.get(id);
    const far = [...state.centers.values()].map(([x, y]) => Math.max(Math.abs(x - cx), Math.abs(y - cy))).sort((a, b) => a - b);
    const half = Math.max(NEAR_MIN, Math.min(NEAR_MAX, (far[Math.min(NEAR_COUNT, far.length - 1)] || NEAR_MIN) + 3));
    return [cx - half, cy - half, 2 * half, 2 * half];
  }

  const defaultBox = () => (state.centers.has(state.region) ? nearBox(state.region) : state.wideBox);
  const currentBox = () => state.view || defaultBox();

  /** 보는 범위만 바꾼다(확대·이동할 때마다 불린다). 넓게 보면 이름표·역을 숨기고 선을 가늘게 한다. */
  function applyView() {
    if (!state.svg) return;
    const box = currentBox();
    state.svg.setAttribute("viewBox", box.map((v) => v.toFixed(2)).join(" "));
    state.svg.setAttribute("class", `${box[2] > LABEL_MAX_W ? "wide" : "near"}${state.view ? " zoomed" : ""}`);
    $("back").hidden = !state.view && !state.centers.has(state.region);
    // 이름표·역의 자리는 화면을 한 번 그릴 때 한 번만 다시 잡는다(손가락으로 확대·이동하는 동안 버벅이지 않게)
    if (!state.overlayFrame) state.overlayFrame = requestAnimationFrame(() => { state.overlayFrame = 0; layoutOverlay(); });
  }

  /** 보는 범위를 정한다: 너무 가까이·너무 멀리 가지 않게, 지도 밖으로 벗어나지 않게. */
  function setView(box) {
    const wide = state.wideBox, ratio = box[3] / box[2];
    const w = Math.max(ZOOM_MIN_W, Math.min(box[2], Math.max(wide[2], wide[3] / ratio))), hgt = w * ratio;
    const cx = Math.max(wide[0], Math.min(wide[0] + wide[2], box[0] + box[2] / 2)), cy = Math.max(wide[1], Math.min(wide[1] + wide[3], box[1] + box[3] / 2));
    state.view = [cx - w / 2, cy - hgt / 2, w, hgt];
    applyView();
  }

  /** 배율만큼 확대(1보다 크면)·축소한다. 화면의 한 점(손가락·마우스 위치)을 주면 그 점이 제자리에 있게. */
  function zoomBy(factor, clientX, clientY) {
    if (!state.svg) return;
    const rect = state.svg.getBoundingClientRect(), [x, y, w, hgt] = currentBox();
    const fx = clientX == null ? 0.5 : (clientX - rect.left) / rect.width, fy = clientY == null ? 0.5 : (clientY - rect.top) / rect.height;
    setView([x + (w - w / factor) * fx, y + (hgt - hgt / factor) * fy, w / factor, hgt / factor]);
  }

  /** 지도 손짓: 두 손가락으로 벌리고 오므려 확대·축소(함께 움직이면 이동), 확대한 뒤에는 한 손가락으로 끌어 이동.
   *  확대하기 전의 한 손가락은 페이지를 넘기는 데 쓰게 둔다(지도가 화면을 꽉 채워도 아래로 내려갈 수 있게). 마우스는 끌어서 이동, Ctrl+휠로 확대. */
  function bindMapGestures(svg) {
    const pointers = new Map();
    let start = null, moved = 0;
    const begin = () => { start = { box: [...currentBox()], pts: [...pointers.values()].map((p) => ({ ...p })) }; };
    svg.addEventListener("pointerdown", (event) => {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) moved = 0;
      begin();
    });
    window.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId) || !start) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      const rect = svg.getBoundingClientRect(), scale = start.box[2] / rect.width, pts = [...pointers.values()];
      if (pts.length === 1 && start.pts.length === 1) {
        if (event.pointerType === "touch" && !state.view) return;  // 확대 전의 한 손가락은 페이지 넘기기
        const dx = pts[0].x - start.pts[0].x, dy = pts[0].y - start.pts[0].y;
        moved = Math.max(moved, Math.hypot(dx, dy));
        if (moved > 4) setView([start.box[0] - dx * scale, start.box[1] - dy * scale, start.box[2], start.box[3]]);
      } else if (pts.length >= 2 && start.pts.length >= 2) {
        moved = 99;
        const d0 = Math.hypot(start.pts[0].x - start.pts[1].x, start.pts[0].y - start.pts[1].y) || 1, d1 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        const w = start.box[2] / (d1 / d0), hgt = start.box[3] / (d1 / d0), s2 = w / rect.width;
        const mx0 = (start.pts[0].x + start.pts[1].x) / 2, my0 = (start.pts[0].y + start.pts[1].y) / 2, mx1 = (pts[0].x + pts[1].x) / 2, my1 = (pts[0].y + pts[1].y) / 2;
        // 처음 두 손가락 가운데가 가리키던 지점이 지금 가운데 아래에 오도록
        const gx = start.box[0] + (mx0 - rect.left) * scale, gy = start.box[1] + (my0 - rect.top) * scale;
        setView([gx - (mx1 - rect.left) * s2, gy - (my1 - rect.top) * s2, w, hgt]);
      }
    });
    const end = (event) => { if (pointers.delete(event.pointerId)) begin(); };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    // 두 손가락 손짓과 확대한 뒤의 끌기는 브라우저가 페이지를 움직이는 데 쓰지 않게 한다
    svg.addEventListener("touchmove", (event) => { if (event.touches.length >= 2 || state.view) event.preventDefault(); }, { passive: false });
    svg.addEventListener("wheel", (event) => {  // 그냥 휠은 페이지를 넘기는 데 쓰게 두고, Ctrl+휠(트랙패드 오므리기 포함)일 때만 확대
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.25 : 0.8, event.clientX, event.clientY);
    }, { passive: false });
    svg.addEventListener("click", (event) => { if (moved > 6) { event.stopPropagation(); event.preventDefault(); } }, true);  // 끌어서 옮긴 것은 누른 것으로 치지 않는다
    svg.addEventListener("keydown", (event) => { if (event.key === "+" || event.key === "=") zoomBy(ZOOM_STEP); else if (event.key === "-") zoomBy(1 / ZOOM_STEP); });
  }

  function paintMap() {
    if (!state.svg) return;
    const picked = state.centers.has(state.region) ? state.region : null;
    for (const [id, paths] of state.paths) {
      const region = regionOf(id), top = region && region.top, grade = gradeOn(region), isPicked = id === picked;
      const sm = region && state.type ? typeSummary(region, state.type) : null;
      const label = !region ? id : `${fullName(region)}: ${state.type ? (sm && RANK[sm.grade] > 0 ? topText({ hazard: sm.best.hazard.id, grade: sm.grade, p: sm.p }) : `${typeName(state.type)} 가능성 낮음`) : top && RANK[top.grade] > 0 ? topText(top) : "가능성 낮음"}`;
      for (const path of paths) {
        path.setAttribute("class", `zone g-${grade}${isPicked ? " picked" : ""}`);
        path.setAttribute("aria-label", label);
        path.setAttribute("aria-pressed", String(isPicked));
        if (isPicked && path.parentNode.lastChild !== path) path.parentNode.append(path);  // 고른 지역의 테두리가 이웃에 가리지 않게 맨 위로
      }
    }
    $("map-what").textContent = `색: ${state.type ? `${typeName(state.type)} 특보` : "가장 높은 특보"} 가능성`;
    applyView();
  }

  // ------------------------------------------------------------ 기록

  /** 특보 기록(달력): 고른 지역과 종류를 넘겨 records.js 가 그린다. */
  function renderRecords(fresh) {
    const region = regionOf(state.region);
    $("records-title").textContent = `특보 기록${state.type ? ` · ${typeName(state.type)}` : ""}${region ? ` · ${region.name}` : ""}`;
    Records.render($("records-body"), { region: region ? { id: region.id, name: region.name } : null, type: state.type, types: state.latest.types, levels: state.latest.levels, again: () => renderRecords(false), fresh, fold });
  }

  // ------------------------------------------------------------ 머리말 · 전체

  function renderHead() {
    const base = state.base, alerts = clear($("alerts"));
    const liveCfg = base.live || {}, usable = state.live && Live.fresh(state.live.warnings, liveCfg.stale_after_minutes || 30);
    const checked = usable ? fmtClock(state.live.warnings.checked_at).split(" ").pop() : null;
    $("updated").textContent = `계산 ${fmtClock(base.generated_at)} · ${fmtAgo(base.generated_at)}${checked ? ` · 특보 확인 ${checked}` : ""}`;
    if (base.demo) alerts.append(h("div", { class: "banner demo", role: "status" }, h("div", null, "데모 화면입니다. 표시된 수치는 실제 예보가 아닌 가상 자료입니다.")));
    const hours = (Date.now() - new Date(base.generated_at).getTime()) / 3600000;
    if (hours > (base.stale_after_hours || 7)) alerts.append(h("div", { class: "banner", role: "status" }, h("div", null, `자료가 ${Math.floor(hours)}시간 전 것입니다. 최신 예보가 반영되지 않았을 수 있습니다.`)));
  }

  function render() {
    if (!state.latest) return;
    if (!regionOf(state.region)) state.region = null;
    // 지역이나 종류가 바뀐 첫 그림에서만 움직인다(몇 분마다 다시 그릴 때는 가만히)
    const scene = `${state.region || ""}|${state.type || ""}`;
    state.fresh = scene !== state.scene;
    state.scene = scene;
    renderHead(); renderMatches(); renderTypes(); renderResult(); paintMap(); renderRecords(state.fresh); renderProof();
    state.fresh = false;
  }

  function applyLive() {
    state.latest = Live.applyToLatest(state.base, state.live, (state.base.live || {}).stale_after_minutes || 30);
    render();
  }

  async function refreshLive() {
    const config = state.base && state.base.live;
    if (!config || !config.url) return;
    try { state.live = (await Live.load(config)) || state.live; } catch (_) { /* 실시간 자료를 못 받아도 3시간 자료로 보여 준다 */ }
    applyLive();
  }

  async function load() {
    try {
      const [latest, zones, proof, rail, stations, longRain, work] = await Promise.all([getJson("data/latest.json"), state.zones || getJson("geo/zones.json"), getJson("data/backtest.json").catch(() => null),
        state.rail.length ? state.rail : getJson("geo/rail.json").catch(() => []), state.stationList || getJson("geo/stations.json").catch(() => []),
        state.longRain || getJson("data/rain_longcheck.json").catch(() => null), state.work || getJson("data/work_rules.json").catch(() => null)]);
      state.work = work || state.work;
      state.proof = proof || state.proof;
      state.longRain = longRain || state.longRain;
      state.rail = Array.isArray(rail) ? rail : []; state.stationList = Array.isArray(stations) ? stations : [];
      const first = !state.base;
      state.base = latest; state.zones = zones;
      state.latest = Live.applyToLatest(latest, state.live, (latest.live || {}).stale_after_minutes || 30);
      if (first) {
        try { state.region = localStorage.getItem(STORE_KEY); state.type = localStorage.getItem(TYPE_KEY); } catch (_) { /* 무시 */ }
        if (!(latest.types || []).some((item) => item.id === state.type)) state.type = null;
        drawMap();
      }
      render();
      closeIntro();
      refreshLive();
      const poll = Math.max(30, Number((latest.live || {}).poll_seconds) || 120) * 1000;
      if (!state.liveTimer && latest.live && latest.live.url) state.liveTimer = setInterval(refreshLive, poll);
    } catch (error) {
      if (!state.base) {
        $("updated").textContent = "";
        clear($("result")).append(h("div", { class: "panel" }, h("p", { class: "empty", text: "자료를 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요." })));
      }
      closeIntro();  // 못 받았을 때도 안내 글이 보이게 걷는다
    }
  }

  /** 여는 화면(인트로)을 걷는다: 첫 화면이 그려진 뒤에, 그래도 너무 번쩍 지나가지 않게 최소 시간은 채우고. 누르면 바로 걷힌다.
   *  스크립트가 돌지 않아도 몇 초 뒤에는 CSS 가 스스로 걷는다(style.css 의 intro-leave) — 여는 화면에 갇히는 일이 없게. */
  function closeIntro(now) {
    const intro = $("intro");
    if (!intro || intro.dataset.closing) return;
    intro.dataset.closing = "1";
    try { sessionStorage.setItem("teukbo.intro", "1"); } catch (_) { /* 무시 */ }
    // 인트로를 건너뛴 때(같은 탭에서 다시 연 때)는 기다릴 것도, 첫 그림을 다시 움직일 것도 없다 — 본문은 처음부터 보이고 있다
    if (document.documentElement.classList.contains("no-intro")) { intro.remove(); return; }
    const wait = now ? 0 : Math.max(0, INTRO_MIN_MS - performance.now());
    setTimeout(() => {
      intro.classList.add("is-leaving");
      // 본문은 인트로가 옅어지기 시작한 바로 뒤에 처음 그린다(그 전에는 html.intro-on 으로 가려 둔다):
      // 인트로에 완전히 가려진 채 그려지고 밀린 지도가 덜 그려진 채 남는 일을 막는다.
      setTimeout(() => { document.documentElement.classList.remove("intro-on"); state.scene = null; render(); layoutOverlay(); }, INTRO_REVEAL_MS);
      setTimeout(() => { intro.remove(); repaintMap(); }, INTRO_FADE_MS + 100);
    }, wait);
  }

  /** 지도 칸을 다시 그리게 한다(자리·크기는 그대로). 인트로가 걷힌 뒤 한 번 — 덜 그려진 조각이 남았더라도 여기서 채워진다. */
  function repaintMap() {
    const card = document.querySelector(".home-map");
    if (!card) return;
    card.style.visibility = "hidden";
    void card.offsetHeight;  // 지금 상태를 한 번 계산시킨 뒤 되돌려, 칸 전체를 새로 그릴 것으로 표시한다
    card.style.visibility = "";
    layoutOverlay();
  }

  $("q").addEventListener("input", (event) => { state.query = event.target.value; if (state.latest) renderMatches(); });
  $("q").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !state.latest) return;
    const found = matches();
    if (found.length) { event.preventDefault(); pick(found[0].id); event.target.blur(); }
  });
  $("back").addEventListener("click", () => pick(null));
  $("help-open").addEventListener("click", openHelp);
  $("help-close").addEventListener("click", closeHelp);
  $("help").addEventListener("click", (event) => { if (event.target === $("help")) closeHelp(); });  // 바깥의 어두운 곳을 누르면 닫는다
  if ($("intro")) $("intro").addEventListener("click", () => closeIntro(true));
  // 화면 크기가 바뀌면(휴대폰을 돌리거나 창을 줄이면) 이름표·역의 자리를 다시 잡는다
  if (window.ResizeObserver) new ResizeObserver(() => layoutOverlay()).observe($("map")); else window.addEventListener("resize", layoutOverlay);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.base) { load(); } });
  setInterval(load, RELOAD_MS);
  load();
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
