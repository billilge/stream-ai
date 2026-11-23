"""Gemini가 답변 중에 직접 부르는 조회 함수(function calling 도구).

function calling: 모델이 "이건 문서에 없고 지금 값을 조회해야 한다"고 판단하면
여기 등록된 함수를 대신 호출하고, 돌려받은 값을 보고 답을 쓰는 기능이다.
`app.py`가 함수 목록을 모델에 넘기고, 호출 자체는 google-genai SDK가 알아서 한다.

실시간 데이터(재고처럼 지금 값이 중요한 것)만 여기서 다룬다. 인덱싱하면 어제 값으로 답하게 된다.
AGENTS.md "데이터 분류" 참고.

**함수의 독스트링과 인자 이름이 그대로 모델에게 설명으로 전달된다.** 모델은 이 설명만 보고
언제 부를지, 무엇을 넣을지 정한다. 고칠 때 이 점을 기억한다.
"""

import logging
import os
from contextvars import ContextVar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 백엔드 연결 (#69)
# ─────────────────────────────────────────────────────────────
# 백엔드 주소. 배포에서는 같은 도커 네트워크 안의 내부 주소를 넣는다(예: http://<백엔드 컨테이너 이름>:8080).
# 비어 있으면 아래 가짜 응답을 쓴다. 로컬 개발과 평가(eval.py)는 학생 토큰이 없어서 가짜 응답으로 돈다.
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
if os.getenv("ENV", "dev") == "prod" and not BACKEND_BASE_URL:
    # 배포에서 빠뜨리면 학생에게 가짜 재고를 진짜처럼 보여주게 된다. 조용히 뜨지 않고 멈춘다.
    raise RuntimeError("ENV=prod 인데 환경변수 BACKEND_BASE_URL이 없습니다. 백엔드 내부 주소를 넣으세요.")

# 백엔드와 같은 내부 토큰. 재고처럼 학생마다 같은 값은 학생 JWT가 아니라 이 토큰으로 AI 서버용 내부 API(/v1/ai/**)를 부른다(#93).
# 학생 JWT는 쓰기 권한까지 있어서, 본인 것만 봐야 하는 조회(내 대여·사물함)에만 쓴다.
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

# 백엔드가 느리면 답변 전체가 기다린다. 재고 하나 조회에 5초 넘게 걸리면 실패로 보고 안내 문구로 답한다.
BACKEND_TIMEOUT_SECONDS = 5

# 지금 질문한 학생의 JWT. app.py가 요청마다 넣고, 도구 함수 안에서만 꺼내 쓴다.
# ContextVar: 요청마다 따로 보관되는 변수다. 여러 학생이 동시에 질문해도 서로의 토큰이 섞이지 않는다.
# 함수 인자로 받지 않는 이유: 인자는 모델이 채운다. 모델이 토큰을 보면 답변으로 샐 수 있고, 다른 값을 넣을 수도 있다.
# 이 토큰은 학생의 쓰기 권한까지 갖고 있다. 그래서 이 파일에는 조회(GET) 도구만 둔다(로드맵 「학생 토큰 전달」).
user_token: ContextVar[str | None] = ContextVar("user_token", default=None)

TYPE_LABELS = {"RENTAL": "대여품", "CONSUMABLE": "소모품"}

# ─────────────────────────────────────────────────────────────
# 가짜 응답 — 백엔드 주소가 없을 때(로컬 개발·평가) 쓴다
# ─────────────────────────────────────────────────────────────
# 모양은 stream-server의 `GET /v1/ai/billilge/items` 실제 응답과 같다
# (ApiResponse → 물품 배열 → AiItemResponse). 학생 앱 목록과 필드 이름이 같고, 이미지와 커서가 없다.
#
# 품목과 수량은 test_data/billilge.json(골든셋 기준 데이터)과 맞췄다.
# 그 파일은 예전 빌릴게 백엔드 모양(itemName, itemType)이라 직접 읽지 않는다.
_FAKE_RESPONSE = {
    "success": True,
    "code": "SUCCESS",
    "message": "요청에 성공했습니다.",
    "data": [
        {"itemId": 3, "name": "C타입 충전기", "category": "ELECTRONICS", "type": "RENTAL", "count": 7, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 6, "name": "HDMI 케이블", "category": "ELECTRONICS", "type": "RENTAL", "count": 6, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 5, "name": "공학용 계산기", "category": "ELECTRONICS", "type": "RENTAL", "count": 0, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 7, "name": "노트북 거치대", "category": "ELECTRONICS", "type": "RENTAL", "count": 3, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 4, "name": "멀티탭", "category": "ELECTRONICS", "type": "RENTAL", "count": 8, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 2, "name": "보조배터리", "category": "ELECTRONICS", "type": "RENTAL", "count": 4, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        {"itemId": 1, "name": "우산", "category": "DAILY_SUPPLIES", "type": "RENTAL", "count": 22, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
    ],
}


def _fetch_items() -> list[dict]:
    """백엔드에서 물품 목록을 받아 온다. 백엔드 주소가 없으면 가짜 응답을 돌려준다.

    AI 서버용 내부 API `GET /v1/ai/billilge/items`를 내부 토큰으로 부른다(#93).
    재고는 학생마다 같은 값이라 학생 JWT가 필요 없다. 백엔드가 목록을 끝까지 모아 한 번에 내려준다.

    실패하면 예외를 던진다. 부르는 쪽(`get_item_stock`)이 잡아서 "확인할 수 없다"는 안내로 바꾼다.
    """
    if not BACKEND_BASE_URL:
        return _FAKE_RESPONSE["data"]
    if not INTERNAL_TOKEN:
        raise RuntimeError("INTERNAL_TOKEN이 없어 재고를 조회할 수 없습니다")
    # 토큰은 로그에 남기지 않는다. 예외 메시지에도 주소만 들어간다.
    r = httpx.get(
        f"{BACKEND_BASE_URL}/v1/ai/billilge/items",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=BACKEND_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    response = r.json()
    if not response.get("success"):
        raise RuntimeError(f"백엔드 API 실패: /v1/ai/billilge/items code={response.get('code')}")
    return response.get("data") or []


def _backend_get(path: str, params: dict | None = None):
    """학생 JWT로 백엔드 학생 앱 API 하나를 불러 응답의 `data`를 돌려준다. 실패하면 예외를 던진다.

    내 대여·내 사물함 도구가 같은 방식으로 부르므로 한곳에 모았다(#79). 재고는 내부 API로 따로 부른다(#93).
    백엔드 응답은 항상 `{"success": ..., "code": ..., "data": ...}` 모양이다(ApiResponse).
    `data`는 비어 있을 수 있다(예: 배정된 사물함이 없으면 null).
    """
    token = user_token.get()
    if not token:
        raise RuntimeError("학생 토큰(X-User-Token)이 없어 백엔드를 조회할 수 없습니다")
    # 토큰은 로그에 남기지 않는다. 예외 메시지에도 주소만 들어가고 헤더는 들어가지 않는다.
    r = httpx.get(
        f"{BACKEND_BASE_URL}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=BACKEND_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    response = r.json()
    if not response.get("success"):
        raise RuntimeError(f"백엔드 API 실패: {path} code={response.get('code')}")
    return response.get("data")


def get_item_stock(item_name: str = "") -> dict:
    """빌릴게(학생회 물품 대여)에서 지금 빌릴 수 있는 물품과 남은 수량을 조회한다.

    학생이 물품이 남았는지, 몇 개 있는지, 빌릴 수 있는지, 어떤 물품이 있는지 물을 때 부른다.
    빌리는 방법·위치·운영 시간 같은 이용 안내는 이 함수가 아니라 참고 문서에 있다.

    Args:
        item_name: 찾을 물품 이름. "우산", "충전기"처럼 물품 이름만 넣는다.
            "우산 재고" 같은 말은 붙이지 않는다. 전체 목록이 필요하면 비워 둔다.
    """
    # 모델이 넘긴 값이라 원문 그대로 남겨도 된다. 학생 질문 원문은 아니다.
    logger.info("도구 호출: get_item_stock(item_name=%r)", item_name)
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

    try:
        items = _fetch_items()
    except Exception:
        # 예외를 모델 쪽으로 올리지 않는다. 대신 "조회 실패"를 값으로 돌려줘서,
        # 모델이 수량을 지어내지 않고 확인할 수 없다고 답하게 한다.
        logger.exception("재고 조회 실패")
        return {"조회성공": False, "안내": "지금 재고를 확인할 수 없어요. stream 빌릴게 화면에서 확인해 주세요."}

    keyword = item_name.strip()
    # 백엔드 검색과 같은 방식(이름에 검색어가 들어 있으면 일치)이다. 띄어쓰기 차이는 무시한다.
    matched = [i for i in items if keyword.replace(" ", "") in i["name"].replace(" ", "")]

    result = {
        "조회성공": True,
        "조회시각": now,
        "물품": [
            {
                "이름": i["name"],
                "종류": TYPE_LABELS.get(i["type"], i["type"]),
                "남은수량": i["count"],
                # 소모품은 반납 정책이 없다(null).
                "최대대여일": (i.get("returnPolicy") or {}).get("maxRentalDays"),
            }
            for i in (matched or items)
        ],
    }
    if not matched:
        # 없는 물품을 물었을 때 "없어요"로 끝내지 않고 있는 물품을 함께 보여줄 수 있게 한다.
        result["안내"] = f"'{keyword}'과(와) 이름이 맞는 물품이 없어요. 전체 목록을 대신 보냅니다."
    return result


# ─────────────────────────────────────────────────────────────
# 내 대여 (#79) — 개인별 데이터라 질문한 학생 본인 것만 조회한다
# ─────────────────────────────────────────────────────────────
# 백엔드 대여 상태값을 학생이 앱에서 보는 말로 바꾼다. 모델이 영어 상태값을 그대로 말하지 않게 한다.
RENTAL_STATUS_LABELS = {
    "PENDING": "신청함(승인 대기)",
    "CONFIRMED": "승인됨(아직 안 받아 감)",
    "REJECTED": "거절됨",
    "CANCEL": "신청 취소함",
    "RENTAL": "대여 중",
    "RETURN_PENDING": "반납 신청함(확인 대기)",
    "RETURN_CONFIRMED": "반납 승인됨(최종 확인 대기)",
    "RETURNED": "반납 완료",
}

# 끝난 대여(반납 완료·취소·거절)는 최근 몇 건만 보여준다. 다 넘기면 오래된 기록까지 모델 입력이 길어진다.
# "반납 처리됐어?" 같은 질문에 답할 만큼만 남긴다.
FINISHED_RENTAL_STATUSES = {"RETURNED", "CANCEL", "REJECTED"}
RECENT_FINISHED_LIMIT = 3


def _fake_my_rentals() -> tuple[list[dict], list[dict]]:
    """백엔드 주소가 없을 때(로컬 개발·평가) 쓸 가짜 대여 기록. (전체 이력, 반납해야 하는 목록) 순서다.

    모양은 `GET /histories`의 `data.histories`, `GET /histories/return-required`의 `data.rentalHistories`와 같다.
    날짜를 고정하면 며칠 뒤 평가에서 전부 "기한 지남"이 되므로 지금 시각 기준으로 만든다.
    """
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    rented = now - timedelta(days=1)
    due = (rented + timedelta(days=3)).replace(hour=17, minute=0)  # 빌린 날 + 3일, 17시 마감
    histories = [
        {"rentalHistoryId": 12, "itemName": "우산", "itemImageUrl": None,
         "rentAt": (now + timedelta(hours=2)).isoformat(timespec="seconds"), "returnedAt": None, "status": "PENDING"},
        {"rentalHistoryId": 11, "itemName": "보조배터리", "itemImageUrl": None,
         "rentAt": rented.isoformat(timespec="seconds"), "returnedAt": None, "status": "RENTAL"},
        {"rentalHistoryId": 9, "itemName": "HDMI 케이블", "itemImageUrl": None,
         "rentAt": (now - timedelta(days=10)).isoformat(timespec="seconds"),
         "returnedAt": (now - timedelta(days=8)).isoformat(timespec="seconds"), "status": "RETURNED"},
    ]
    return_required = [
        {"rentalHistoryId": 11, "itemName": "보조배터리", "itemImageUrl": None,
         "status": "RENTAL", "dueAt": due.isoformat(timespec="seconds")},
    ]
    return histories, return_required


def _fetch_my_rentals() -> tuple[list[dict], list[dict]]:
    """학생 본인의 대여 이력 전체와 반납해야 하는 목록을 받아 온다. 실패하면 예외를 던진다.

    두 API를 부르는 이유: 이력(`/histories`)에는 반납 기한이 없고, 반납 기한은 `/histories/return-required`만 준다.
    반대로 반납 목록에는 승인 대기 같은 아직 안 받아 간 신청이 없다. 그래서 둘을 대여 ID로 맞춰 합친다.
    """
    if not BACKEND_BASE_URL:
        return _fake_my_rentals()
    histories = (_backend_get("/v1/app/billilge/histories") or {}).get("histories") or []
    required = (_backend_get("/v1/app/billilge/histories/return-required") or {}).get("rentalHistories") or []
    return histories, required


def _to_datetime(value: str | None) -> datetime | None:
    """백엔드 시각 문자열('2026-11-17T17:00:00')을 한국 시각 datetime으로. 없으면 None."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Seoul"))


def _fmt(value: datetime | None) -> str | None:
    """모델에게 넘길 시각 표기. 요일을 붙여야 "내일까지예요"처럼 말하기 쉽다."""
    if value is None:
        return None
    weekday = "월화수목금토일"[value.weekday()]
    return value.strftime(f"%Y-%m-%d({weekday}) %H:%M")


def get_my_rentals() -> dict:
    """질문한 학생 본인이 빌렸거나 신청한 빌릴게 물품과 반납 기한을 조회한다.

    학생이 내가 빌린 물품, 반납 기한, 언제까지 돌려줘야 하는지, 연체됐는지,
    내 대여 신청이 승인됐는지, 반납 처리가 됐는지 물을 때 부른다.
    물품이 남았는지(재고)는 이 함수가 아니라 get_item_stock으로 조회한다.
    """
    # 결과는 개인정보라 로그에 남기지 않는다. 불렸다는 사실만 남긴다.
    logger.info("도구 호출: get_my_rentals()")
    now = datetime.now(ZoneInfo("Asia/Seoul"))

    try:
        histories, required = _fetch_my_rentals()
    except Exception:
        logger.exception("내 대여 조회 실패")
        return {"조회성공": False, "안내": "지금 대여 내역을 확인할 수 없어요. stream 빌릴게 화면의 내 대여에서 확인해 주세요."}

    due_by_id = {r["rentalHistoryId"]: _to_datetime(r.get("dueAt")) for r in required}

    in_progress, finished = [], []
    for h in histories:  # 백엔드가 최근 신청순으로 준다
        status = h["status"]
        rent_at = _to_datetime(h.get("rentAt"))
        item = {"물품": h.get("itemName") or "(삭제된 물품)", "상태": RENTAL_STATUS_LABELS.get(status, status)}
        if status in FINISHED_RENTAL_STATUSES:
            if len(finished) < RECENT_FINISHED_LIMIT:
                if status == "RETURNED":
                    item["반납시각"] = _fmt(_to_datetime(h.get("returnedAt")))
                finished.append(item)
            continue
        if status in ("PENDING", "CONFIRMED"):
            # 받아 가기 전에는 rentAt이 학생이 고른 희망 수령 시각이다. 받아 가면 실제 수령 시각으로 바뀐다.
            item["수령예정시각"] = _fmt(rent_at)
        else:
            item["수령시각"] = _fmt(rent_at)
            due = due_by_id.get(h["rentalHistoryId"])
            if due:
                item["반납기한"] = _fmt(due)
                item["기한지남"] = now > due
        in_progress.append(item)

    result = {"조회성공": True, "조회시각": _fmt(now), "진행중": in_progress, "최근끝난대여": finished}
    if not in_progress:
        result["안내"] = "지금 빌리고 있거나 신청해 둔 물품이 없어요."
    return result


# ─────────────────────────────────────────────────────────────
# 내 사물함 (#79) — 개인별 데이터라 질문한 학생 본인 것만 조회한다
# ─────────────────────────────────────────────────────────────
LOCKER_STATUS_LABELS = {"ASSIGNED": "사용 중", "EXPIRED": "사용 기간 끝남"}

# 가짜 응답. 모양은 `GET /v1/app/lockers/applications`의 data, `GET /v1/app/lockers/sections`의 data와 같다.
# 위치는 골든셋 32번 정답요지와 맞췄다.
_FAKE_LOCKER_APPLICATIONS = {"applications": [
    {"lockerApplicationId": 5, "lockerPeriodId": 2, "lockerPeriodName": "2026-2학기 사물함",
     "applicationStatus": "ASSIGNED", "appliedAt": "2026-09-01T10:03:00",
     "usageStartDate": "2026-09-01", "usageEndDate": "2026-12-19",
     "sectionId": 4, "lockerLabel": "17"},
]}
_FAKE_LOCKER_SECTIONS = {"sections": [
    {"sectionId": 3, "section": "미래관 3층 A구역", "availableCount": 2, "totalCount": 40,
     "availabilityStatus": "AVAILABLE", "hasMine": False},
    {"sectionId": 4, "section": "미래관 4층 B구역", "availableCount": 8, "totalCount": 40,
     "availabilityStatus": "AVAILABLE", "hasMine": True},
]}


def _fetch_my_lockers() -> tuple[list[dict], dict[int, str]]:
    """학생 본인의 사물함 배정 목록과 {구역ID: 구역 이름}을 받아 온다. 실패하면 예외를 던진다.

    배정 목록에는 구역 번호(sectionId)만 있고 "미래관 4층 B구역" 같은 이름이 없다.
    이름은 그 회차의 구역 목록(`/lockers/sections`)에만 있어서 한 번 더 부른다. 위치를 묻는 질문이라 이름이 꼭 필요하다.
    """
    if not BACKEND_BASE_URL:
        applications = _FAKE_LOCKER_APPLICATIONS["applications"]
        return applications, {s["sectionId"]: s["section"] for s in _FAKE_LOCKER_SECTIONS["sections"]}

    # 배정된 사물함이 없으면 백엔드가 data를 null로 준다.
    applications = (_backend_get("/v1/app/lockers/applications") or {}).get("applications") or []
    section_names = {}
    for period_id in {a["lockerPeriodId"] for a in applications}:
        sections = (_backend_get("/v1/app/lockers/sections", {"lockerPeriodId": period_id}) or {}).get("sections") or []
        section_names.update({s["sectionId"]: s["section"] for s in sections})
    return applications, section_names


def get_my_locker() -> dict:
    """질문한 학생 본인에게 배정된 사물함의 위치(구역·번호)와 사용 기간을 조회한다.

    학생이 내 사물함이 어디인지, 몇 번인지, 언제까지 쓸 수 있는지, 사물함 신청이 됐는지 물을 때 부른다.
    사물함 신청 방법이나 신청 기간 같은 안내는 이 함수가 아니라 참고 문서에 있다.
    """
    # 결과는 개인정보라 로그에 남기지 않는다. 불렸다는 사실만 남긴다.
    logger.info("도구 호출: get_my_locker()")

    try:
        applications, section_names = _fetch_my_lockers()
    except Exception:
        logger.exception("내 사물함 조회 실패")
        return {"조회성공": False, "안내": "지금 사물함 정보를 확인할 수 없어요. stream 사물함 화면에서 확인해 주세요."}

    if not applications:
        return {"조회성공": True, "사물함": [], "안내": "배정된 사물함이 없어요."}

    return {"조회성공": True, "사물함": [
        {
            "회차": a.get("lockerPeriodName"),
            "구역": section_names.get(a.get("sectionId")),
            "번호": a.get("lockerLabel"),
            "상태": LOCKER_STATUS_LABELS.get(a.get("applicationStatus"), a.get("applicationStatus")),
            "사용기간": f"{a.get('usageStartDate') or ''} ~ {a.get('usageEndDate') or ''}",
        }
        for a in applications
    ]}


# app.py가 모델에 넘기는 도구 목록. 새 도구를 만들면 여기에 추가한다.
TOOLS = [get_item_stock, get_my_rentals, get_my_locker]
