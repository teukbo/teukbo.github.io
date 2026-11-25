/* 특보 종류 그림 — 숫자보다 먼저 '무슨 특보인지'가 보이게, 종류마다 그림을 하나씩 둔다.
 * 선으로만 그리고 색은 글자색(currentColor)을 따른다. live 를 주면 조금씩 움직인다(style.css 의 hz-* 애니메이션 — 움직임 줄이기 설정이면 멈춘다).
 * 모르는 종류는 게이지(전체) 그림으로 대신한다. 그림은 장식이라 읽어 주지 않는다(이름은 늘 옆에 글로 적는다).
 */
window.TeukboIcons = (() => {
  "use strict";
  const { s } = window.TeukboUI;
  const CLOUD = "M6.5 12.5H17a3.5 3.5 0 0 0 .6-6.95A5.5 5.5 0 0 0 7.1 6.700a3 3 0 0 0-.6 5.800Z";
  const line = (d, cls) => s("path", { d, class: cls || null });
  const dot = (cx, cy, r, cls) => s("circle", { cx: String(cx), cy: String(cy), r: String(r), class: cls || null });

  const SHAPES = {
    all: () => [line("M4 16.5a8 8 0 0 1 16 0"), line("M12 16.5l4.200-5.200", "hz-move hz-needle"), dot(12, 16.5, 1.3, "hz-fill")],
    heavy_rain: () => [line(CLOUD), line("M8.500 15.5l-1 3", "hz-move hz-drop d1"), line("M12.500 15.5l-1 3", "hz-move hz-drop d2"), line("M16.500 15.5l-1 3", "hz-move hz-drop d3")],
    strong_wind: () => [line("M3 8h8.500a2.500 2.500 0 1 0-2.400-3.200", "hz-move hz-gust d1"), line("M3 12h14.500a2.500 2.500 0 1 1-2.400 3.200", "hz-move hz-gust d2"), line("M3 16h7", "hz-move hz-gust d3")],
    heavy_snow: () => [line(CLOUD), dot(8, 16.5, 0.9, "hz-fill hz-move hz-flake d1"), dot(12, 18.5, 0.9, "hz-fill hz-move hz-flake d2"), dot(16, 16.5, 0.9, "hz-fill hz-move hz-flake d3")],
    cold_wave: () => [line("M6 4.500a2 2 0 0 1 4 0v8.600a4 4 0 1 1-4 0Z"), line("M8 14.500v2", "hz-move hz-mercury"), dot(8, 16.6, 1.5, "hz-fill"),
      s("g", { class: "hz-move hz-spin", style: "transform-origin: 17.5px 8px" }, line("M17.500 4.500v7"), line("M14.500 6.250l6 3.500"), line("M14.500 9.750l6-3.500"))],
    heat_wave: () => [dot(12, 12, 4, null), s("g", { class: "hz-move hz-spin", style: "transform-origin: 12px 12px" },
      line("M12 2.500v2.500"), line("M12 19v2.500"), line("M2.500 12H5"), line("M19 12h2.500"), line("M5.300 5.300l1.800 1.800"), line("M16.900 16.900l1.800 1.800"), line("M5.300 18.700l1.800-1.800"), line("M16.900 7.100l1.800-1.800"))],
    tropical_night: () => [line("M15.500 4a8.200 8.200 0 1 0 4.700 13.300A7 7 0 0 1 15.500 4Z"), line("M5 5.500v2M4 6.500h2", "hz-move hz-twinkle d1"), line("M9 2.500v1.600M8.200 3.300h1.600", "hz-move hz-twinkle d2")],
    typhoon: () => [s("g", { class: "hz-move hz-spin", style: "transform-origin: 12px 12px" }, dot(12, 12, 3.2, null), line("M12 8.800c3.200 0 5.400-1.700 6.500-4.800"), line("M12 15.200c-3.200 0-5.400 1.700-6.500 4.800"))],
  };

  /** 종류 id(heavy_rain …, 전체는 "all") → 그림. opts.live: 움직이게, opts.class: 덧붙일 클래스 */
  function hazard(type, opts = {}) {
    const key = SHAPES[type] ? type : "all";
    return s("svg", { class: `hz-icon hz-${key}${opts.live ? " live" : ""}${opts.class ? ` ${opts.class}` : ""}`, viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" }, SHAPES[key]());
  }

  return { hazard, has: (type) => Boolean(SHAPES[type]) };
})();
