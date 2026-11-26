"""시간별 실황 이어 받기: 기상청 초단기실황을 정시마다 저장소에 쌓는다 — 지역별 기록의 '시간대별 체감온도·날씨'가 쓴다.

    python -m teukbo.hourly            (collect.yml 이 수집·계산 뒤에 부른다)

무엇을 받나
- 초단기실황(getUltraSrtNcst — 단기예보 조회서비스와 같은 활용신청): 5km 격자마다 짝지어 둔 대표 방재기상관측(AWS) 지점의 정시 값.
  기온 T1H · 습도 REH · 풍속 WSD · 1시간 강수량 RN1 · 강수형태 PTY. 하늘 상태는 이 자료에 없다.
- 한 번 호출에 격자 한 곳·기준시각 하나만 온다. 그래서 '지역의 격자 수(225) × 정시 수'만큼 부른다(평소 3시간마다 675회, 하루 약 5,400회).

런너에서 실제 응답으로 확인한 것(2026-11-26, teukbo/check.py 의 check_ncst_history)
- 가장 최근 기준시각에서 23시간 전까지 값이 온다. 24시간 전부터는 "오류 코드 10 최근 1일 간의 자료만 제공합니다."
  → 수집이 하루 넘게 멈추면 그 사이의 시각은 다시 받을 수 없다(빈 채로 남는다).
- 지난 기준시각을 연달아 두 번 물으면 같은 값이 온다. 한 번 호출은 0.45~0.8초, 100번에 두어 번은 응답이 오래 멈춘다
  → 이 수집은 짧게 기다리고(timeout_seconds) 한 번만 다시 부른다.
- 가장 최근 기준시각은 다음 실행으로 미룬다(skip_latest_hours): 진행 중인 시각의 값이 뒤에 바뀌는지는 확인하지 못했다.

저장: data/hourly/YYYY-MM-DD/HH.json  (기준시각의 한국 날짜·시. 커밋하고 지우지 않는다)
    {"schema": 1, "t": "2026-11-26T14:00+09:00", "vars": ["temp", "rh", "wind", "rain", "pty"],
     "grids": {"60,127": [0.8, 81, 2.8, 0.0, 0]},        // "nx,ny" → vars 순서의 값(없으면 null)
     "missing": ["61,128"], "tries": 1}                  // 못 받은 격자가 있을 때만. 다 받았으면 두 키가 없다
- 다 받은 파일은 다시 쓰지 않는다(저장소가 덜 커진다). 못 받은 격자가 남은 파일만, 조회 가능 기간 안에서 refill_tries 번까지 다시 채운다
  (빠진 격자를 모두 물어본 실행만 한 번으로 센다 — 거절·연속 실패·시간 초과·상한으로 끊긴 실행은 세지 않는다).
- data/hourly/calls.json {"date": "2026-11-26", "calls": 5123} — 오늘 부른 횟수. 하루 상한(daily_call_cap)을 넘기지 않으려고 센다:
  이 서비스의 하루 호출 한도(개발계정 10,000건)를 단기예보 수집과 나눠 쓸 수도 있어, 시간별 실황이 한도를 다 써 버리지 않게 한다.

원칙: 이 단계가 실패해도 주 수집(특보 가능성 계산)·배포는 계속되어야 한다 — 예외를 밖으로 내지 않고 늘 0 을 돌려준다. 인증키는 어디에도 남기지 않는다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import storage
from .config import ROOT, Config, load_config
from .http import FetchError, normalize_service_key
from .sources import kma_ultra
from .sources.datagokr import NoData, OutOfRange, is_rejected
from .timeutil import KST, iso, now_kst

log = logging.getLogger("teukbo.hourly")

SCHEMA = 1
VARS = ("temp", "rh", "wind", "rain", "pty")
DIGITS = {"temp": 1, "rh": 0, "wind": 1, "rain": 1, "pty": 0}  # 저장할 때의 소수 자리(0 = 정수)
CALLS_FILE = "calls.json"

Grid = tuple[int, int]
Fetch = Callable[[datetime, Grid], "Mapping[str, Any] | None"]  # (기준시각, 격자) → parse_ncst 꼴의 값. 없으면 None, 실패하면 FetchError


def hourly_dir(data_dir: Path) -> Path:
    return data_dir / "hourly"


def hour_path(data_dir: Path, t: datetime) -> Path:
    t = t.astimezone(KST)
    return hourly_dir(data_dir) / t.strftime("%Y-%m-%d") / f"{t:%H}.json"


def grid_key(grid: Grid) -> str:
    return f"{grid[0]},{grid[1]}"


def pack(row: Mapping[str, Any] | None) -> list[Any] | None:
    """초단기실황 한 격자의 값 → vars 순서의 목록(정해 둔 자리로 반올림). 쓸 값이 하나도 없으면 None."""
    if not row:
        return None
    out: list[Any] = []
    for name in VARS:
        value = row.get(name)
        if value is None:
            out.append(None)
        elif DIGITS[name] == 0:
            out.append(int(round(float(value))))
        else:
            out.append(round(float(value), DIGITS[name]))
    return out if any(v is not None for v in out) else None


def load_hour(path: Path) -> dict[str, Any] | None:
    """저장된 정시 파일. 없거나 꼴이 맞지 않으면(쓰다 만 파일 등) None — 그 시각은 다시 받는다."""
    try:
        doc = storage.read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("grids"), dict) or list(doc.get("vars") or []) != list(VARS):
        return None
    return doc


def wanted_hours(now: datetime, max_back_hours: int, skip_latest_hours: int = 0) -> list[datetime]:
    """지금 받을 정시들 — 오래된 것부터(조회 가능 기간에서 곧 밀려날 것을 먼저 받는다).

    skip_latest_hours: 가장 최근 기준시각 몇 개는 건너뛴다. 기상청 가이드는 초단기실황이 '10분마다 최신 정보로 업데이트'된다고 적는데
    그것이 진행 중인 시각의 값을 바꾸는지는 문서에 없다 — 한 번 받은 파일은 다시 쓰지 않으므로, 시각이 지나 값이 굳은 뒤에 받는다.
    """
    latest = kma_ultra.base_time(kma_ultra.ncst_bases(now, count=1)[0])
    return [latest - timedelta(hours=back) for back in range(max(0, int(max_back_hours)), max(0, int(skip_latest_hours)) - 1, -1)]


def stored_days(data_dir: Path) -> list[str]:
    """시간별 값이 저장된 날짜(YYYY-MM-DD)들, 오름차순."""
    folder = hourly_dir(data_dir)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[7] == "-")


def load_day(data_dir: Path, day: str) -> dict[int, dict[str, list[Any]]]:
    """한 날의 시간별 값: 시(0~23) → {"nx,ny": [vars 순서의 값]}. 읽을 수 없는 파일은 건너뛴다."""
    out: dict[int, dict[str, list[Any]]] = {}
    folder = hourly_dir(data_dir) / day
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("[0-2][0-9].json")):
        doc = load_hour(path)
        if doc is None:
            continue
        try:
            hour = int(path.stem)
        except ValueError:
            continue
        if 0 <= hour <= 23:
            out[hour] = doc["grids"]
    return out


def _calls_today(data_dir: Path, today: str) -> int:
    try:
        doc = storage.read_json(hourly_dir(data_dir) / CALLS_FILE)
    except (OSError, ValueError):
        return 0
    if isinstance(doc, dict) and doc.get("date") == today:
        try:
            return max(0, int(doc.get("calls", 0)))
        except (TypeError, ValueError):
            return 0
    return 0


def make_fetch(env: Mapping[str, str], opts: Mapping[str, Any]) -> Fetch | None:
    """인증키에 맞는 조회 함수. 공공데이터포털 키가 먼저, 없으면 기상청 API허브 키. 둘 다 없으면 None."""
    key = normalize_service_key(env.get("DATA_GO_KR_SERVICE_KEY"))
    hub = (env.get("KMA_APIHUB_KEY") or "").strip()
    if key:
        url, key_param, secret = f"{kma_ultra.DATA_GO_KR_BASE}/getUltraSrtNcst", "serviceKey", key
    elif hub:
        url, key_param, secret = f"{kma_ultra.APIHUB_BASE}/getUltraSrtNcst", "authKey", hub
    else:
        return None
    http_opts = {"timeout_seconds": float(opts["timeout_seconds"]), "backoff_seconds": float(opts["backoff_seconds"])}
    retries = int(opts["retries"])

    def fetch(base: datetime, grid: Grid) -> Mapping[str, Any] | None:
        base = base.astimezone(KST)
        return kma_ultra.parse_ncst(kma_ultra._fetch_grid(url, key_param, secret, (base.strftime("%Y%m%d"), base.strftime("%H00")), grid, http_opts, retries, "시간별 실황"))

    return fetch


def fetch_hour(t: datetime, todo: Sequence[Grid], fetch: Fetch, workers: int, deadline: float, stop_after_failures: int) -> tuple[dict[str, list[Any]], dict[str, int], str]:
    """한 정시의 격자들을 동시에 받는다. 반환: (격자 키 → 값, 결과별 횟수, 끝난 까닭 'done'·'expired'·'rejected'·'failing'·'timeup').

    'expired' = 조회 가능 기간 밖(이 시각은 더 받을 수 없다), 'rejected' = 키·한도 문제로 거절(실행 전체를 멈춘다),
    'failing' = 연달아 실패(서버 장애로 보고 실행 전체를 멈춘다), 'timeup' = 시간 예산 초과.
    """
    stop = threading.Event()
    lock = threading.Lock()
    state = {"reason": "done", "streak": 0}

    def halt(reason: str) -> None:
        with lock:
            if state["reason"] in ("done", "timeup"):
                state["reason"] = reason
        stop.set()

    def one(grid: Grid) -> tuple[Grid, list[Any] | None, str]:
        # 멈출지는 부르는 쪽(작업 스레드)에서 바로 정한다 — 결과를 모으는 쪽에서 정하면 그사이 이미 여러 번 더 부른 뒤다
        if stop.is_set():
            return grid, None, "skipped"
        if time.monotonic() > deadline:
            with lock:
                if state["reason"] == "done":
                    state["reason"] = "timeup"
            return grid, None, "skipped"
        status, values = "failed", None
        try:
            values = pack(fetch(t, grid))
            status = "ok" if values is not None else "empty"
        except OutOfRange:
            status = "expired"
        except NoData:
            status = "empty"
        except FetchError as e:
            status = "rejected" if is_rejected(e) else "failed"
        except Exception:  # noqa: BLE001 — 한 격자의 뜻밖의 오류로 수집 전체가 멈추지 않게
            log.exception("시간별 실황 %s 격자 %s: 뜻밖의 오류", t.strftime("%m-%d %H시"), grid_key(grid))
        if status in ("expired", "rejected"):
            halt(status)
        elif status == "failed":
            with lock:
                state["streak"] += 1
                failing = state["streak"] >= stop_after_failures
            if failing:
                halt("failing")
        else:
            with lock:
                state["streak"] = 0
        return grid, values, status

    got: dict[str, list[Any]] = {}
    counts = {"ok": 0, "empty": 0, "failed": 0, "calls": 0}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for grid, values, status in pool.map(one, todo):
            if status == "skipped":
                continue
            counts["calls"] += 1
            if status == "ok":
                got[grid_key(grid)] = values  # type: ignore[assignment]
                counts["ok"] += 1
            elif status == "empty":
                counts["empty"] += 1
            elif status == "failed":
                counts["failed"] += 1
    return got, counts, str(state["reason"])


def run(cfg: Config, now: datetime, data_dir: Path, env: Mapping[str, str], fetch: Fetch | None = None) -> int:
    """시간별 실황을 이어 받는다. 늘 0 을 돌려준다(실패해도 수집·배포는 계속되어야 한다)."""
    opts = cfg.settings.hourly
    if not opts.get("enabled", True):
        log.info("시간별 실황: 설정(hourly.enabled)에서 꺼져 있습니다.")
        return 0
    fetch = fetch or make_fetch(env, opts)
    if fetch is None:
        log.info("인증키가 없어 시간별 실황을 받지 않습니다.")
        return 0
    grids = sorted({(r.nx, r.ny) for r in cfg.regions})
    keys = [grid_key(g) for g in grids]
    today = now.astimezone(KST).strftime("%Y-%m-%d")
    cap = int(opts["daily_call_cap"])
    calls = _calls_today(data_dir, today)
    started = time.monotonic()
    deadline = started + float(opts["time_budget_seconds"])
    done_hours = saved = failed = new_calls = 0
    why = ""

    for t in wanted_hours(now, int(opts["max_back_hours"]), int(opts["skip_latest_hours"])):
        path = hour_path(data_dir, t)
        doc = load_hour(path)
        have: dict[str, list[Any]] = dict(doc["grids"]) if doc else {}
        tries = int(doc.get("tries", 0)) if doc else 0
        todo = [g for g, key in zip(grids, keys) if key not in have]
        if not todo:
            continue  # 다 받은 시각
        if doc is not None and tries >= int(opts["refill_tries"]):
            continue  # 못 받은 격자를 채워 볼 만큼 채워 봤다
        if time.monotonic() > deadline:
            why = f"시간 예산({float(opts['time_budget_seconds']):.0f}초)을 다 썼습니다"
            break
        room = cap - calls
        if room <= 0:
            why = f"오늘 호출 상한({cap}회)에 닿았습니다"
            break
        asked_all = len(todo) <= room  # 상한 때문에 일부만 묻는 실행은 '채워 본 횟수'로 치지 않는다
        todo = todo[:room]

        got, counts, reason = fetch_hour(t, todo, fetch, int(opts["workers"]), deadline, int(opts["stop_after_failures"]))
        # 빠진 격자를 모두 물어본 실행만 한 번으로 센다: 거절·연속 실패·시간 초과·상한으로 끊긴 실행이 횟수를 깎아 먹으면
        # 서버가 멀쩡해진 뒤에도 그 시각을 영영 채우지 않게 된다
        full_try = asked_all and reason == "done"
        calls += counts["calls"]
        new_calls += counts["calls"]
        failed += counts["failed"]
        if got or doc is not None:
            have.update(got)
            missing = [key for key in keys if key not in have]
            out: dict[str, Any] = {"schema": SCHEMA, "t": iso(t.astimezone(KST)), "vars": list(VARS), "grids": {key: have[key] for key in keys if key in have}}
            if missing:
                out["missing"] = missing
                out["tries"] = tries + (1 if doc is not None and full_try else 0)
            if got or (doc is not None and missing and out["tries"] != tries):
                storage.write_json(path, out)  # 값이 늘었거나, 다 물어봤는데도 못 받아 횟수가 올랐을 때만 다시 쓴다
                saved += len(got)
            if not missing:
                done_hours += 1
        storage.write_json(hourly_dir(data_dir) / CALLS_FILE, {"date": today, "calls": calls})
        if reason == "rejected":
            why = "인증키 또는 하루 호출 한도 문제로 거절되었습니다 — 공공데이터포털의 활용신청과 일일 트래픽을 확인해 주세요"
            log.error("시간별 실황: %s. 이번 실행은 그만둡니다.", why)
            break
        if reason == "failing":
            why = f"연달아 {int(opts['stop_after_failures'])}번 받지 못했습니다(서버 장애로 보입니다)"
            log.error("시간별 실황: %s. 이번 실행은 그만둡니다.", why)
            break
        if reason == "timeup":
            why = f"시간 예산({float(opts['time_budget_seconds']):.0f}초)을 다 썼습니다"
            break
        # 'expired' 는 그 시각만 건너뛴다(다음 실행 때는 받을 시각 목록에서 빠져 있다)

    log.info("시간별 실황: 정시 %d개 완성, 격자값 %d개 저장, 호출 %d회(실패 %d) · %.0f초 · 오늘 누적 %d/%d회%s",
             done_hours, saved, new_calls, failed, time.monotonic() - started, calls, cap, f" — {why}" if why else "")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="시간별 실황(기상청 초단기실황) 이어 받기")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.ERROR)  # 요청마다 찍으면 수천 줄이 된다(실패는 이 모듈이 세어 한 줄로 적는다)
    try:
        return run(load_config(), now_kst().replace(second=0, microsecond=0), args.data_dir, os.environ)
    except Exception:  # noqa: BLE001 — 시간별 실황 때문에 수집·배포가 멈추면 안 된다
        log.exception("시간별 실황을 받는 중 오류 — 이번 실행은 건너뜁니다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
