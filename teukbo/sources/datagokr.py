"""공공데이터포털(기상청 API) 응답 공통 처리.

API허브의 typ02/openApi 경로도 같은 응답 구조를 쓴다.
- 정상:   {"response": {"header": {"resultCode": "00", …}, "body": {"items": {"item": [...]}, "totalCount": n}}}
- 자료없음: resultCode "03"  /  기타 오류: header 만 있고 body 없음 (예: "99" 조회 기간 초과)
- 요청값 오류: resultCode "10" — 초단기실황은 하루가 지난 기준시각을 물으면 이 코드와 "최근 1일 간의 자료만 제공합니다." 가 온다(2026-11-26 실제 응답)
- 거절: resultCode "22"(하루 호출 한도 초과) · "30"(등록되지 않은 키) 등 — 같은 실행에서 더 불러도 소용없다
- 게이트웨이 오류(키 미등록·한도 초과·IP 차단): {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {...}}} + HTTP 4xx
- API허브 오류: {"result": {"status": 401, "message": "…"}}
"""

from __future__ import annotations

from typing import Any, Mapping

from ..http import FetchError


class NoData(FetchError):
    """요청한 조건의 자료가 (아직) 없음 — resultCode 03 또는 빈 목록."""


class OutOfRange(FetchError):
    """요청값이 받을 수 있는 범위 밖 — resultCode 10 (예: 초단기실황에 하루가 지난 기준시각)."""


class Rejected(FetchError):
    """키 미등록·활용기간 만료·하루 한도 초과처럼, 같은 실행에서 다시 불러도 받아 주지 않는 거절."""


REJECTED_CODES = {"20", "21", "22", "30", "31", "32", "33"}  # 접근 거부·일시 사용 불가·한도 초과·미등록 키·기한 만료·미등록 IP·서명 오류
REJECTED_STATUS = {401, 403}  # 게이트웨이가 HTTP 상태로 거절할 때(teukbo.http 가 FetchError.status 에 싣는다). 429 는 초당 한도일 수 있어 넣지 않는다


def is_rejected(error: BaseException) -> bool:
    """이 오류가 '더 불러도 소용없는 거절'인가."""
    return isinstance(error, Rejected) or getattr(error, "status", None) in REJECTED_STATUS


def extract_items(payload: Any, label: str) -> tuple[list[Mapping[str, Any]], int]:
    """응답에서 item 목록과 totalCount 를 꺼낸다. 오류 응답이면 FetchError, 자료가 없으면 NoData."""
    if not isinstance(payload, dict):
        raise FetchError(f"{label}: 예상하지 못한 응답 형식")
    gateway = payload.get("OpenAPI_ServiceResponse")
    if gateway:
        head = gateway.get("cmmMsgHeader", {}) if isinstance(gateway, dict) else {}
        # 게이트웨이 응답이라도 사유가 키·한도일 때만 '거절'이다. 일시 오류(04 라우팅 오류, 05 시간 초과 등)는 다음에 다시 부를 만하다
        kind = Rejected if str(head.get("returnReasonCode", "")).zfill(2) in REJECTED_CODES else FetchError
        raise kind(
            f"{label}: {head.get('errMsg', '게이트웨이 오류')} "
            f"({head.get('returnAuthMsg', '')}, 코드 {head.get('returnReasonCode', '?')})"
        )
    if "result" in payload and "response" not in payload:
        res = payload["result"]
        message = res.get("message", res) if isinstance(res, dict) else res
        raise FetchError(f"{label}(API허브): {message}", status=res.get("status") if isinstance(res, dict) else None)
    response = payload.get("response") or {}
    header = response.get("header") or {}
    code = str(header.get("resultCode", "")).zfill(2)
    if code == "03":
        raise NoData(f"{label}: 자료 없음 ({header.get('resultMsg', 'NODATA_ERROR')})")
    if code == "10":
        raise OutOfRange(f"{label}: 오류 코드 {code} {header.get('resultMsg', '')}")
    if code in REJECTED_CODES:
        raise Rejected(f"{label}: 오류 코드 {code} {header.get('resultMsg', '')}")
    if code != "00":
        raise FetchError(f"{label}: 오류 코드 {code} {header.get('resultMsg', '')}")
    body = response.get("body") or {}
    items_node = body.get("items") or {}
    items = items_node.get("item") if isinstance(items_node, dict) else None
    if isinstance(items, dict):
        items = [items]
    if not items:
        raise NoData(f"{label}: 자료 없음 (빈 응답)")
    return list(items), int(body.get("totalCount") or len(items))
