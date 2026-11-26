/* 특보 기록 — 고른 지역에 특보가 언제 발효됐는지, 그날 실제 날씨(기상청 관측)가 어땠는지를 달력 한 장으로 보여 준다.
 *
 * 자료: data/records/<지역>.json (teukbo/records.py 가 배포 때 만든다)
 *   days["2026-11-18"] = { o: [vars 순서의 관측값…], w: [[종류, 단계, 발효 시각, 해제 시각|null], …], h: 시간별 값이 있는 시각 수, f: [체감 최저, 시, 체감 최고, 시] }
 *   관측(o)은 가까운 기상청 관측소의 일자료(전날 것이 다음 날 오전에 들어온다), 특보는 기상청 발표·해제 이력.
 * 날짜를 누르면 그 달의 시간별 파일 data/hours/<지역>/<연-월>.json 을 그때 받아 '시간대별' 칸을 그린다(기상청 초단기실황 — 그 지역 격자의 정시 값).
 *   체감온도는 서버가 기상청 식으로 계산해 실어 준 값을 그대로 쓴다(화면에서 다시 계산하지 않는다). 하루 관측값(관측소)과는 지점이 달라 칸을 따로 둔다.
 * 첫 화면(home.js)이 고른 지역과 특보 종류를 넘겨 준다. 종류를 고르면 그 종류의 특보만 표시하고, 달력 칸의 숫자도 그 종류에 맞는 값으로 바뀐다
 * (한파 → 최저기온, 호우 → 강수량, 강풍 → 순간풍속 …). 종류·단계의 이름은 받아 온 자료(types·levels)에서 읽는다.
 * 원칙: 받아 온 글은 늘 텍스트로 넣는다. 값이 없으면 지어내지 않고 '–' 로 적는다.
 */
window.TeukboRecords = (() => {
  "use strict";
  const { h, s, clear, getJson } = window.TeukboUI;
  const HOUR_COL = 46, HOUR_CURVE = 54;  // 시간대 띠의 한 칸 너비, 꺾은선 칸의 높이(px) — style.css 의 .hr-col · .hr-lines 와 같게
  const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];
  // 종류별로 달력 칸에 적는 관측값: [변수, 이름, 단위]. 앞의 것이 없으면 다음 것을 쓴다.
  const METRIC = {
    cold_wave: [["temp_min", "최저기온", "℃"]], tropical_night: [["temp_min", "최저기온", "℃"]], heat_wave: [["feel_max", "체감 최고", "℃"], ["temp_max", "최고기온", "℃"]],
    heavy_rain: [["rain", "강수량", "mm"]], typhoon: [["rain", "강수량", "mm"]], strong_wind: [["gust_max", "최대순간풍속", "m/s"], ["wind_max", "최대풍속", "m/s"]], heavy_snow: [["snow_new", "새로 쌓인 눈", "cm"]],
  };
  const docs = new Map();  // 지역 id → 기록(받는 중이면 "loading", 못 받았으면 null)
  const hourDocs = new Map();  // `${지역 id}|${연-월}` → 시간별 한 달 파일(받는 중이면 "loading", 못 받았으면 null)
  const view = { region: null, month: null, day: null, type: undefined, copied: "", strip: { key: "", left: null } };  // 보고 있는 달·고른 날(지역이나 종류가 바뀌면 다시 잡는다)

  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (v, digits = 1) => (v == null ? "–" : Number(v).toFixed(digits).replace(/\.0$/, "").replace("-", "−"));
  const weekday = (ymd) => { const [y, m, d] = ymd.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)).getUTCDay(); };
  const dayLabel = (ymd) => { const [, m, d] = ymd.split("-").map(Number); return `${m}월 ${d}일(${WEEKDAY[weekday(ymd)]})`; };
  /** 한국 시각으로 '18일 04시' */
  const clock = (iso) => { const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000); return `${t.getUTCDate()}일 ${pad(t.getUTCHours())}시`; };
  const hoursBetween = (from, to) => Math.max(1, Math.round((new Date(to).getTime() - new Date(from).getTime()) / 3600000));

  function load(id, again) {
    if (docs.has(id)) return docs.get(id);
    docs.set(id, "loading");
    getJson(`data/records/${encodeURIComponent(id)}.json`).then((doc) => (doc && doc.days && Array.isArray(doc.vars) ? doc : null), () => null).then((doc) => { docs.set(id, doc); again(); });
    return "loading";
  }

  /** 그 날의 값을 이름으로 꺼낸다. 체감온도(feel_min·feel_max)는 시간별 값으로 구한 그날의 최저·최고(f)에서, 나머지는 관측소 일자료(o)에서. */
  const valueOf = (doc, ymd, name) => {
    const day = doc.days[ymd] || {};
    if (name === "feel_min" || name === "feel_max") return Array.isArray(day.f) ? day.f[name === "feel_min" ? 0 : 2] : null;
    const at = doc.vars.indexOf(name);
    return day.o && at >= 0 ? day.o[at] : null;
  };
  /** 그 날 발효 중이던 특보(고른 종류만, 높은 단계 먼저). */
  function warningsOn(doc, ymd, ctx) {
    const rank = (level) => ((ctx.levels || {})[level] || {}).rank || 0;
    return ((doc.days[ymd] || {}).w || []).filter((w) => !ctx.type || w[0] === ctx.type).sort((a, b) => rank(b[1]) - rank(a[1]));
  }
  const typeName = (ctx, id) => ((ctx.types || []).find((t) => t.id === id) || {}).name || id;
  const levelName = (ctx, id) => ((ctx.levels || {})[id] || {}).name || id;
  const warnName = (ctx, w) => `${typeName(ctx, w[0])}${levelName(ctx, w[1])}`;
  /** 달력 칸에 적을 값: 고른 종류에 맞는 것. 전체일 때는 최저/최고기온. */
  function cellValue(doc, ymd, ctx) {
    if (!ctx.type) { const lo = valueOf(doc, ymd, "temp_min"), hi = valueOf(doc, ymd, "temp_max"); return lo == null && hi == null ? null : { text: `${fmt(lo, 0)}/${fmt(hi, 0)}`, label: `최저 ${fmt(lo)}℃, 최고 ${fmt(hi)}℃` }; }
    for (const [name, label, unit] of METRIC[ctx.type] || []) {
      const v = valueOf(doc, ymd, name);
      if (v != null) return { text: fmt(v, Math.abs(v) >= 10 ? 0 : 1), label: `${label} ${fmt(v)}${unit}`, what: `${label}(${unit})` };
    }
    return null;
  }

  const monthsOf = (doc) => { const out = []; let [y, m] = doc.from.split("-").map(Number); const [y2, m2] = doc.to.split("-").map(Number); while (y < y2 || (y === y2 && m <= m2)) { out.push(`${y}-${pad(m)}`); m += 1; if (m > 12) { m = 1; y += 1; } } return out; };
  const daysIn = (month) => { const [y, m] = month.split("-").map(Number); return new Date(Date.UTC(y, m, 0)).getUTCDate(); };

  /** 달을 옮기거나 지역·종류가 바뀌면: 그 달에서 특보가 있었던 가장 최근 날을 고른다(없으면 고르지 않는다). */
  function pickDefaultDay(doc, ctx) {
    view.day = null;
    for (let d = daysIn(view.month); d >= 1; d -= 1) { const ymd = `${view.month}-${pad(d)}`; if (warningsOn(doc, ymd, ctx).length) { view.day = ymd; break; } }
  }

  /** 한 특보가 여러 날에 걸쳐 실려 있으므로 (종류·단계·발효 시각)으로 하나씩만 모은다. 최근 것부터. */
  function episodes(doc, ctx) {
    const seen = new Map();
    for (const [ymd, day] of Object.entries(doc.days)) for (const w of day.w || []) if (!ctx.type || w[0] === ctx.type) { const key = `${w[0]}|${w[1]}|${w[2]}`; if (!seen.has(key)) seen.set(key, { w, first: ymd }); else if (ymd < seen.get(key).first) seen.get(key).first = ymd; }
    return [...seen.values()].sort((a, b) => (a.w[2] < b.w[2] ? 1 : -1));
  }

  /** 이 달의 표를 탭으로 나눈 글로(엑셀에 그대로 붙는다). */
  function monthTable(doc, ctx) {
    const rows = [["날짜", "요일", "특보", "최저기온(℃)", "최고기온(℃)", "체감 최저(℃)", "체감 최고(℃)", "강수량(mm)", "새로 쌓인 눈(cm)", "최대풍속(m/s)", "최대순간풍속(m/s)"]];
    for (let d = 1; d <= daysIn(view.month); d += 1) {
      const ymd = `${view.month}-${pad(d)}`;
      if (ymd < doc.from || ymd > doc.to) continue;
      const cell = (name) => { const v = valueOf(doc, ymd, name); return v == null ? "" : String(v); };
      rows.push([ymd, WEEKDAY[weekday(ymd)], [...new Set(warningsOn(doc, ymd, ctx).map((w) => warnName(ctx, w)))].join(", "), cell("temp_min"), cell("temp_max"), cell("feel_min"), cell("feel_max"), cell("rain"), cell("snow_new"), cell("wind_max"), cell("gust_max")]);
    }
    return rows.map((row) => row.join("\t")).join("\n");
  }
  // ------------------------------------------------------------ 시간대별(날짜를 누르면)

  /** 그 달의 시간별 파일. 처음이면 받기 시작하고, 받으면 기록 칸을 다시 그린다. */
  function loadHours(id, month, again) {
    const key = `${id}|${month}`;
    if (hourDocs.has(key)) return hourDocs.get(key);
    hourDocs.set(key, "loading");
    getJson(`data/hours/${encodeURIComponent(id)}/${month}.json`).then((doc) => (doc && doc.days && Array.isArray(doc.vars) ? doc : null), () => null).then((doc) => { hourDocs.set(key, doc); again(); });
    return "loading";
  }

  /** 그 날의 시각별 값 → [{hour, temp, feel, rain, wind, rh, pty}] (값이 있는 시각만, 오름차순). 이름은 파일의 vars 에서 찾는다. */
  function hourRows(hdoc, ymd) {
    const day = hdoc.days[ymd];
    if (!day || !Array.isArray(day.h) || !Array.isArray(day.v)) return [];
    const pick = (row, name) => { const i = hdoc.vars.indexOf(name); return i >= 0 && row[i] != null ? row[i] : null; };
    return day.h.map((hour, k) => { const row = day.v[k] || []; return { hour, temp: pick(row, "temp"), feel: pick(row, "feel"), rain: pick(row, "rain"), wind: pick(row, "wind"), rh: pick(row, "rh"), pty: pick(row, "pty") }; });
  }
  /** 강수형태 코드의 이름은 실시간 화면과 같은 표(live.js)에서 읽는다. 표에 없으면 '강수'. */
  const wetName = (pty) => (window.TeukboLive && pty != null && window.TeukboLive.ptyText(pty)) || "강수";
  const isWet = (row) => (row.pty != null && row.pty !== 0) || (row.rain != null && row.rain > 0);
  /** 이어진 시각들을 '14~17시' 꼴로 */
  const hourSpans = (hours) => { const runs = []; for (const x of hours) { const last = runs[runs.length - 1]; if (last && x === last[1] + 1) last[1] = x; else runs.push([x, x]); } return runs.map(([a, b]) => (a === b ? `${pad(a)}시` : `${pad(a)}~${pad(b)}시`)).join(", "); };
  /** 값이 가장 낮은(sign −1)·높은(sign 1) 시각. 같으면 이른 시각. */
  const extreme = (rows, name, sign) => rows.reduce((best, row) => (row[name] != null && (best == null || sign * row[name] > sign * best[name]) ? row : best), null);

  /** 이 날의 시간별 표를 탭으로 나눈 글로(엑셀에 그대로 붙는다). */
  function dayTable(rows, ymd) {
    const cell = (v) => (v == null ? "" : String(v));
    return [["날짜", "시각", "기온(℃)", "체감온도(℃)", "강수량(mm)", "강수형태", "풍속(m/s)", "습도(%)"],
      ...rows.map((r) => [ymd, `${pad(r.hour)}:00`, cell(r.temp), cell(r.feel), cell(r.rain), r.pty ? wetName(r.pty) : "", cell(r.wind), cell(r.rh)])].map((row) => row.join("\t")).join("\n");
  }
  function copyText(text, again, where) {
    const done = (ok) => { view.copied = { where, text: ok ? "복사했습니다. 엑셀에 붙여 넣으세요." : "복사하지 못했습니다. 브라우저가 막았습니다." }; again(); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => done(true), () => done(false)); else done(false);
  }

  /** 24시간 띠: 칸마다 시각 · 강수 · (기온·체감 꺾은선) · 기온 · 체감 · 바람. 왼쪽 이름표 칸은 옆으로 밀어도 제자리에 있다.
   *  값이 없는 시각은 칸을 비우고, 꺾은선도 거기서 끊는다(없는 값을 이어 그리지 않는다). 눈여겨볼 시각(peak)은 칸을 도드라지게 한다. */
  function hourStrip(rows, peak, label, key) {
    const byHour = new Map(rows.map((r) => [r.hour, r]));
    const all = rows.flatMap((r) => [r.temp, r.feel]).filter((v) => v != null);
    const lo = Math.min(...all), span = Math.max(2, Math.max(...all) - lo);
    const y = (v) => 6 + (1 - (v - lo) / span) * (HOUR_CURVE - 12);
    const lines = (name) => {  // 이어진 시각끼리만 선으로 잇는다
      const runs = [];
      for (const r of rows) { if (r[name] == null) continue; const last = runs[runs.length - 1]; if (last && r.hour === last[last.length - 1].hour + 1) last.push(r); else runs.push([r]); }
      return runs.map((run) => (run.length > 1 ? s("polyline", { class: `hr-line ${name}`, points: run.map((r) => `${r.hour * HOUR_COL + HOUR_COL / 2},${y(r[name]).toFixed(1)}`).join(" ") })
        : s("circle", { class: `hr-dot ${name}`, cx: String(run[0].hour * HOUR_COL + HOUR_COL / 2), cy: y(run[0][name]).toFixed(1), r: "2" })));
    };
    const num = (r, name, unit) => (!r ? "" : r[name] == null ? "–" : `${fmt(r[name])}${unit}`);
    const cols = [];
    for (let hour = 0; hour < 24; hour += 1) {
      const r = byHour.get(hour);
      cols.push(h("div", { class: `hr-col${r ? "" : " none"}${r && r === peak ? " peak" : ""}`,
        title: r ? `${pad(hour)}시: 기온 ${num(r, "temp", "℃")}, 체감 ${num(r, "feel", "℃")}, 바람 ${num(r, "wind", "m/s")}, 습도 ${num(r, "rh", "%")}${isWet(r) ? `, ${wetName(r.pty)}${r.rain ? ` ${fmt(r.rain)}mm` : ""}` : ""}` : `${pad(hour)}시: 값 없음` },
      h("span", { class: "hr-hour", text: `${pad(hour)}시` }),
      h("span", { class: "hr-wet", text: r && isWet(r) ? `${wetName(r.pty)}${r.rain ? ` ${fmt(r.rain)}` : ""}` : "" }),
      h("span", { class: "hr-lines" }),
      h("span", { class: "hr-temp", text: num(r, "temp", "") }),
      h("span", { class: "hr-feel", text: num(r, "feel", "") }),
      h("span", { class: "hr-wind", text: num(r, "wind", "") })));
    }
    const scroller = h("div", { class: "hr-scroll", tabindex: "0", "aria-label": "시간대별 값 — 옆으로 밀어 봅니다" },
      h("div", { class: "hr-inner", style: `width:${24 * HOUR_COL}px` },
        s("svg", { class: "hr-chart", width: String(24 * HOUR_COL), height: String(HOUR_CURVE), viewBox: `0 0 ${24 * HOUR_COL} ${HOUR_CURVE}`, "aria-hidden": "true" }, lines("temp"), lines("feel")),
        cols));
    const strip = h("div", { class: "hr-strip", role: "group", "aria-label": label },
      h("div", { class: "hr-names", "aria-hidden": "true" }, h("span", { text: "시각" }), h("span", { text: "강수(mm)" }), h("span", { class: "hr-lines" }),
        h("span", null, h("i", { class: "hr-key temp" }), "기온(℃)"), h("span", null, h("i", { class: "hr-key feel" }), "체감(℃)"), h("span", { text: "바람(m/s)" })),
      scroller);
    // 옆으로 민 자리는 다시 그려도 그대로 둔다. 그 날을 처음 그릴 때는 눈여겨볼 시각이 가운데 오게 옮긴다
    scroller.addEventListener("scroll", () => { if (view.strip.key === key) view.strip.left = scroller.scrollLeft; }, { passive: true });
    const settle = () => {
      if (!scroller.isConnected) return;
      if (view.strip.key === key && view.strip.left != null) { scroller.scrollLeft = view.strip.left; return; }
      view.strip = { key, left: null };
      scroller.scrollLeft = Math.max(0, (peak ? peak.hour : 0) * HOUR_COL + HOUR_COL / 2 - scroller.clientWidth / 2);
      view.strip.left = scroller.scrollLeft;
    };
    (window.requestAnimationFrame || setTimeout)(settle);
    return strip;
  }

  /** 날짜를 누르면 그 아래에 뜨는 '시간대별' 칸. 시간별 값이 없는 날은 언제부터 모으는지만 적는다. */
  function renderHours(doc, ctx) {
    const ymd = view.day, count = (doc.days[ymd] || {}).h || 0;
    if (!count) return doc.hours && doc.hours.from ? h("p", { class: "rec-none", text: `이 날은 시간별 자료가 없습니다 — ${dayLabel(doc.hours.from)}부터 모읍니다.` }) : null;
    const box = h("div", { class: "hr" }, h("p", { class: "hr-title" }, "시간대별", h("small", null, ` 값이 있는 시각 ${count}개`)));
    const hdoc = loadHours(doc.id, ymd.slice(0, 7), ctx.again);
    if (hdoc === "loading") return put(box, h("div", { class: "skeleton", role: "status", "aria-label": "시간대별 값을 불러오는 중" }, h("span"), h("span")));
    const rows = hdoc ? hourRows(hdoc, ymd) : [];
    if (!rows.length) return put(box, h("p", { class: "rec-none", text: "시간대별 값을 불러오지 못했습니다. 잠시 뒤 다시 눌러 보세요." }));
    const low = hdoc.feel_stat !== "max";  // 겨울철 식을 쓰는 달은 가장 낮은 체감온도, 여름철 식을 쓰는 달은 가장 높은 체감온도를 앞세운다
    const peak = extreme(rows, "feel", low ? -1 : 1) || extreme(rows, "temp", low ? -1 : 1), other = extreme(rows, "feel", low ? 1 : -1);
    const tLow = extreme(rows, "temp", -1), tHigh = extreme(rows, "temp", 1), wet = rows.filter(isWet);
    const fact = (label, value, kind) => h("div", { class: `rec-fact${kind ? ` ${kind}` : ""}` }, h("span", { text: label }), h("b", { text: value }));
    const facts = [
      peak && peak.feel != null ? fact(low ? "체감 최저" : "체감 최고", `${fmt(peak.feel)}℃ · ${pad(peak.hour)}시`, "strong") : null,
      other && other.feel != null && other !== peak ? fact(low ? "체감 최고" : "체감 최저", `${fmt(other.feel)}℃ · ${pad(other.hour)}시`) : null,
      tLow && tHigh ? fact("기온", `${fmt(tLow.temp)}℃(${pad(tLow.hour)}시) ~ ${fmt(tHigh.temp)}℃(${pad(tHigh.hour)}시)`, "wide") : null,
      wet.length ? fact(wetName((wet.find((r) => r.pty) || {}).pty), hourSpans(wet.map((r) => r.hour)), "wide") : null];
    const label = `${dayLabel(ymd)} 시간대별. ${facts.filter(Boolean).map((node) => node.textContent).join(", ")}`;
    return put(box,
      h("div", { class: "rec-facts" }, facts),
      hourStrip(rows, peak, label, `${doc.id}|${ymd}`),
      h("div", { class: "rec-foot" }, h("button", { type: "button", class: "chip", onclick: () => copyText(dayTable(rows, ymd), ctx.again, "day") }, "이 날 시간별 표 복사(엑셀용)"),
        view.copied && view.copied.where === "day" ? h("span", { class: "rec-copied", role: "status", text: view.copied.text }) : null),
      h("p", { class: "rec-source", text: `시간별: ${hdoc.source || "기상청 초단기실황"}. 관측소 값과 다를 수 있습니다.` }));
  }

  function renderDay(doc, ctx) {
    const ymd = view.day, warns = warningsOn(doc, ymd, ctx), v = (name) => valueOf(doc, ymd, name), has = Boolean((doc.days[ymd] || {}).o);
    const item = (label, value) => h("div", { class: "rec-fact" }, h("span", { text: label }), h("b", { text: value }));
    // 하루 관측값(관측소)과 시간대별 값(지역 격자)은 지점이 달라 칸을 따로 둔다. 체감온도는 시간대별 칸에서만 적는다.
    return h("div", { class: "rec-day" },
      h("p", { class: "rec-day-title", text: dayLabel(ymd) }),
      warns.length ? h("ul", { class: "rec-warns" }, warns.map((w) => h("li", null, h("span", { class: `rec-tag lv-${w[1]}`, text: levelName(ctx, w[1]) }), h("b", { text: ` ${warnName(ctx, w)}` }),
        h("small", null, ` ${clock(w[2])} ~ ${w[3] ? `${clock(w[3])} · ${hoursBetween(w[2], w[3])}시간` : "계속"}`))))
        : h("p", { class: "rec-none", text: ctx.type ? `이 날은 ${typeName(ctx, ctx.type)}특보가 없었습니다.` : "이 날은 특보가 없었습니다." }),
      has ? h("div", { class: "rec-facts" }, item("최저기온", `${fmt(v("temp_min"))}℃`), item("최고기온", `${fmt(v("temp_max"))}℃`),
        item("강수량", v("rain") == null ? "–" : `${fmt(v("rain"))}mm`), v("snow_new") ? item("새로 쌓인 눈", `${fmt(v("snow_new"))}cm`) : null,
        item("바람", v("wind_max") == null ? "–" : `최대 ${fmt(v("wind_max"))}m/s${v("gust_max") == null ? "" : ` · 순간 ${fmt(v("gust_max"))}m/s`}`))
        : h("p", { class: "rec-none", text: doc.station ? "이 날의 관측소 값은 아직 들어오지 않았습니다(전날 것이 다음 날 오전에 들어옵니다)." : "가까운 관측소가 없어 관측소 값은 싣지 못했습니다." }),
      has && doc.station ? h("p", { class: "rec-source", text: `하루 값: 기상청 ${doc.station.name} 관측소(약 ${fmt(doc.station.km, 0)}km)` }) : null,
      renderHours(doc, ctx));
  }

  /** 기록 칸을 그린다. ctx: { region: {id, name}|null, type: 종류 id|null, types, levels, again: 다시 그리기, fresh: 지역·종류가 바뀐 첫 그림인지 } */
  function render(box, ctx) {
    clear(box);
    if (!ctx.region) return void box.append(h("p", { class: "rec-none", text: "지역을 고르면 그 지역에 특보가 언제 발효됐는지, 그날 날씨가 어땠는지 볼 수 있습니다." }));
    const doc = load(ctx.region.id, ctx.again);
    if (doc === "loading") return void box.append(h("p", { class: "rec-none", text: "불러오는 중…" }));
    if (!doc || !doc.from || !doc.to) return void box.append(h("p", { class: "rec-none", text: "이 지역의 기록을 아직 불러오지 못했습니다." }));

    const months = monthsOf(doc), moved = view.region !== ctx.region.id || view.type !== ctx.type;
    if (view.region !== ctx.region.id || !months.includes(view.month)) view.month = months[months.length - 1];
    if (moved) { view.region = ctx.region.id; view.type = ctx.type; view.copied = ""; pickDefaultDay(doc, ctx); }
    const at = months.indexOf(view.month), [year, mon] = view.month.split("-").map(Number);
    const go = (step) => { view.month = months[at + step]; view.copied = ""; pickDefaultDay(doc, ctx); view.slide = true; ctx.again(); };
    const animate = ctx.fresh || view.slide;
    view.slide = false;

    // 이 달의 특보 일수(단계별)
    const counts = {};
    let total = 0;
    for (let d = 1; d <= daysIn(view.month); d += 1) { const top = warningsOn(doc, `${view.month}-${pad(d)}`, ctx)[0]; if (top) { counts[top[1]] = (counts[top[1]] || 0) + 1; total += 1; } }
    const scope = ctx.type ? `${typeName(ctx, ctx.type)}특보` : "특보", order = Object.keys(ctx.levels || {}).sort((a, b) => ctx.levels[b].rank - ctx.levels[a].rank);
    const summary = total ? `이 달 ${scope} ${total}일 — ${order.filter((id) => counts[id]).map((id) => `${levelName(ctx, id)} ${counts[id]}일`).join(" · ")}` : `이 달에는 ${scope}가 없었습니다.`;

    const cells = [], shown = new Set();  // shown: 이 달의 칸에 실제로 적은 값의 이름
    for (let k = 0; k < weekday(`${view.month}-01`); k += 1) cells.push(h("span", { class: "cal-pad", "aria-hidden": "true" }));
    for (let d = 1; d <= daysIn(view.month); d += 1) {
      const ymd = `${view.month}-${pad(d)}`, inRange = ymd >= doc.from && ymd <= doc.to, warns = inRange ? warningsOn(doc, ymd, ctx) : [], top = warns[0], val = inRange ? cellValue(doc, ymd, ctx) : null;
      if (val && val.what) shown.add(val.what);
      const tag = !top ? "" : ctx.type ? levelName(ctx, top[1]) : `${typeName(ctx, top[0])}${new Set(warns.map((w) => w[0])).size > 1 ? "+" : ""}`;
      cells.push(h("button", { type: "button", class: `cal-day${top ? ` lv-${top[1]}` : ""}${ymd === view.day ? " picked" : ""}`, disabled: !inRange, "aria-pressed": String(ymd === view.day), style: `--i:${cells.length}`,
        "aria-label": `${dayLabel(ymd)}${warns.length ? `, ${[...new Set(warns.map((w) => warnName(ctx, w)))].join(", ")}` : ""}${val ? `, ${val.label}` : ""}`,
        onclick: () => { view.day = ymd === view.day ? null : ymd; ctx.again(); } },
      h("span", { class: "cal-num", text: String(d) }), h("span", { class: "cal-val", text: val ? val.text : "" }), h("span", { class: "cal-tag", text: tag })));
    }

    const list = episodes(doc, ctx);
    put(box,
      h("div", { class: "rec-head" },
        h("button", { type: "button", class: "rec-nav", "aria-label": "이전 달", disabled: at <= 0, onclick: () => go(-1) }, "‹"),
        h("p", { class: "rec-month", "aria-live": "polite", text: `${year}년 ${mon}월` }),
        h("button", { type: "button", class: "rec-nav", "aria-label": "다음 달", disabled: at >= months.length - 1, onclick: () => go(1) }, "›")),
      h("p", { class: "rec-summary", text: summary }),
      h("div", { class: "cal-week", "aria-hidden": "true" }, WEEKDAY.map((name) => h("span", { text: name }))),
      h("div", { class: `cal-grid${animate ? " enter" : ""}` }, cells),
      h("p", { class: "rec-key" }, order.slice().reverse().filter((id) => list.some((e) => e.w[1] === id) || id === "advisory").map((id) => h("span", null, h("i", { class: `rec-swatch lv-${id}` }), levelName(ctx, id))), h("span", { text: `칸의 숫자: ${!ctx.type ? "최저/최고기온(℃)" : [...shown].join(" 또는 ") || "관측값"}` })),
      view.day ? renderDay(doc, ctx) : h("p", { class: "rec-none", text: "날짜를 누르면 그날의 특보와 관측값을 보여 줍니다." }),
      list.length ? ctx.fold("rec-list", `${scope} 발효 기록 모두 보기 (${list.length}건)`, h("ul", { class: "rec-list" }, list.map((e) => h("li", null,
        h("button", { type: "button", class: "linklike", onclick: () => { view.month = e.first.slice(0, 7); view.day = e.first; view.slide = true; ctx.again(); } }, `${dayLabel(e.first)} ${warnName(ctx, e.w)}`),
        h("small", null, ` ${clock(e.w[2])} ~ ${e.w[3] ? clock(e.w[3]) : "계속"}`))))) : null,
      h("div", { class: "rec-foot" },
        h("button", { type: "button", class: "chip", onclick: () => copyText(monthTable(doc, ctx), ctx.again, "month") }, "이 달 표 복사(엑셀용)"),
        view.copied && view.copied.where === "month" ? h("span", { class: "rec-copied", role: "status", text: view.copied.text }) : null),
      h("p", { class: "rec-source", text: `특보: 기상청 발표·해제 이력(${dayLabel(doc.from)}부터). ${doc.station ? `하루 값: 기상청 ${doc.station.name} 관측소(약 ${fmt(doc.station.km, 0)}km) 일자료${doc.obs_to ? ` · ${dayLabel(doc.obs_to)}까지` : ""}.` : "가까운 관측소가 없어 하루 관측값은 없습니다."}${doc.hours ? ` 시간별: 기상청 초단기실황 · ${dayLabel(doc.hours.from)}부터.` : ""}` }));
  }

  return { render };
})();
