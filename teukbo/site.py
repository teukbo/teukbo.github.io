"""배포용 정적 사이트 조립: web/ + 최신 결과 JSON → _site/

    python -m teukbo.site                       (data/ 사용)
    python -m teukbo.site --data-dir data-demo  (데모 자료로 미리보기)
    python -m http.server -d _site 8000         (로컬 확인)

data/ 의 최신 결과 말고도, 저장소에 쌓인 특보 이력·관측 일자료로 지역별 기록(data/records/<지역>.json — teukbo/records.py)을 만들고
config/work_rules.json(작업 기준)이 있으면 data/work_rules.json 으로 싣는다.

web/ 파일 내용으로 빌드 번호를 만들어 sw.js·index.html 의 __BUILD__ 를 바꿔 넣는다.
화면 파일이 바뀌면 번호가 달라져 설치된 앱(PWA)의 캐시가 새로 갈린다.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sys
from pathlib import Path

from .config import ROOT

log = logging.getLogger("teukbo.site")

BUILD_TOKEN = "__BUILD__"
WORK_RULES_FILE = ROOT / "config" / "work_rules.json"
_TEXT_SUFFIXES = {".html", ".js", ".css", ".webmanifest", ".json"}


def build_id(web_dir: Path) -> str:
    digest = hashlib.sha1()
    for path in sorted(p for p in web_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(web_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def build_site(data_dir: Path, out_dir: Path, web_dir: Path | None = None) -> str:
    web_dir = web_dir or ROOT / "web"
    latest = data_dir / "latest.json"
    if not latest.is_file():
        raise FileNotFoundError(f"{latest} 가 없습니다. 먼저 `python -m teukbo.run` 을 실행하세요.")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(web_dir, out_dir)

    build = build_id(web_dir)
    for path in out_dir.rglob("*"):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            if BUILD_TOKEN in text:
                path.write_text(text.replace(BUILD_TOKEN, build), encoding="utf-8")

    site_data = out_dir / "data"
    site_data.mkdir(exist_ok=True)
    shutil.copy2(latest, site_data / "latest.json")
    # live.json 은 데모·내 컴퓨터용(배포본은 따로 올라온다). rain_longcheck.json 은 tools/rain_longcheck.py 가 만든 '호우를 긴 기간으로 본 확인'.
    for name in ("accuracy.json", "warnings.json", "live.json", "backtest.json", "rain_longcheck.json"):
        if (data_dir / name).is_file():
            shutil.copy2(data_dir / name, site_data / name)
    if (data_dir / "rail" / "summary.json").is_file():
        shutil.copy2(data_dir / "rail" / "summary.json", site_data / "rail.json")
    # 분석 화면(과거 자료 탐색): 묶음 집계와, 날짜를 골랐을 때 읽는 그날의 열차 목록
    days_dir = data_dir / "rail" / "days"
    if (data_dir / "rail" / "cube.json").is_file():
        shutil.copy2(data_dir / "rail" / "cube.json", site_data / "rail-cube.json")
    elif days_dir.is_dir() and any(days_dir.glob("*.json")):
        # 묶음이 아직 없으면(수집이 중간에 멈춘 회차 등) 쌓여 있는 날짜별 기록으로 여기서 만든다 — 분석 화면이 비지 않게
        from . import rail, rail_run, storage

        storage.write_json(site_data / "rail-cube.json", rail.build_cube(rail_run.load_days(data_dir)))
    if days_dir.is_dir():
        shutil.copytree(days_dir, site_data / "rail-days")
    if (data_dir / "detail").is_dir():
        shutil.copytree(data_dir / "detail", site_data / "detail")
    # 작업 기준(폭염·한파 때의 작업 조정·휴식 기준): 설정 파일을 그대로 화면에 넘긴다. 파일이 없으면 화면이 그 칸을 뺀다.
    if WORK_RULES_FILE.is_file():
        shutil.copy2(WORK_RULES_FILE, site_data / "work_rules.json")
    build_records(data_dir, site_data / "records")
    return build


def build_records(data_dir: Path, out_dir: Path) -> int:
    """지역별 기록(특보가 발효된 날과 그날의 관측값)을 만든다. 실패해도 사이트 조립은 계속한다 — 화면은 그 칸만 뺀다."""
    try:
        from . import records
        from .config import load_config

        # 달력에서 날짜를 누르면 읽는 시간별 한 달 파일(data/hours/<지역>/<연-월>.json)도 같이 만든다
        return records.write_site_records(load_config(), data_dir, out_dir, hours_dir=out_dir.parent / "hours")
    except Exception:  # noqa: BLE001 — 기록 때문에 배포가 멈추면 안 된다
        log.exception("지역별 기록을 만들지 못했습니다 — 기록 없이 사이트를 조립합니다.")
        shutil.rmtree(out_dir, ignore_errors=True)  # 쓰다 만 파일을 남기지 않는다
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="배포용 정적 사이트 조립")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        build = build_site(args.data_dir, args.out)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    log.info("사이트 조립 완료: %s (빌드 %s)", args.out, build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
