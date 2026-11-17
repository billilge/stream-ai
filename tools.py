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
from datetime import datetime
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
# 모양은 stream-server의 `GET /v1/app/billilge/items` 실제 응답과 같다
# (ApiResponse → CursorSliceResponse → ItemListItemResponse).
# 그래서 실제 API가 생기면 `_fetch_items`만 HTTP 호출로 바꾸고 나머지는 그대로 둔다.
#
# 품목과 수량은 test_data/billilge.json(골든셋 기준 데이터)과 맞췄다.
# 그 파일은 예전 빌릴게 백엔드 모양(itemName, itemType)이라 직접 읽지 않는다.
# 학생 앱 API는 학생 로그인(JWT)이 있어야 한다. 그래서 백엔드가 중계할 때 학생 JWT를 같이 넘겨 준다(#69).
_FAKE_RESPONSE = {
    "success": True,
    "code": "SUCCESS",
    "message": "요청에 성공했습니다.",
    "data": {
        "content": [
            {"itemId": 3, "name": "C타입 충전기", "category": "ELECTRONICS", "type": "RENTAL", "count": 7, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 6, "name": "HDMI 케이블", "category": "ELECTRONICS", "type": "RENTAL", "count": 6, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 5, "name": "공학용 계산기", "category": "ELECTRONICS", "type": "RENTAL", "count": 0, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 7, "name": "노트북 거치대", "category": "ELECTRONICS", "type": "RENTAL", "count": 3, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 4, "name": "멀티탭", "category": "ELECTRONICS", "type": "RENTAL", "count": 8, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 2, "name": "보조배터리", "category": "ELECTRONICS", "type": "RENTAL", "count": 4, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
            {"itemId": 1, "name": "우산", "category": "DAILY_SUPPLIES", "type": "RENTAL", "count": 22, "imageUrl": None, "returnPolicy": {"maxRentalDays": 3, "returnDeadline": "17:00"}},
        ],
        "hasNext": False,
        "nextCursor": None,
    },
}


def _fetch_items() -> list[dict]:
    """백엔드에서 물품 목록을 받아 온다. 백엔드 주소가 없으면 가짜 응답을 돌려준다.

    학생 앱이 쓰는 `GET /v1/app/billilge/items`를 그 학생의 JWT로 부른다.
    백엔드 목록은 이름순이고 한 번에 최대 100개다. 품목이 100개를 넘을 일은 당분간 없으므로 첫 페이지만 받는다.

    실패하면 예외를 던진다. 부르는 쪽(`get_item_stock`)이 잡아서 "확인할 수 없다"는 안내로 바꾼다.
    """
    if not BACKEND_BASE_URL:
        response = _FAKE_RESPONSE
    else:
        token = user_token.get()
        if not token:
            raise RuntimeError("학생 토큰(X-User-Token)이 없어 재고를 조회할 수 없습니다")
        # 토큰은 로그에 남기지 않는다. 예외 메시지에도 주소만 들어가고 헤더는 들어가지 않는다.
        r = httpx.get(
            f"{BACKEND_BASE_URL}/v1/app/billilge/items",
            params={"size": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=BACKEND_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        response = r.json()
    if not response["success"]:
        raise RuntimeError(f"재고 API 실패: {response['code']}")
    return response["data"]["content"]


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
        return {"조회성공": False, "안내": "지금 재고를 확인할 수 없어요. Stream 빌릴게 화면에서 확인해 주세요."}

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


# app.py가 모델에 넘기는 도구 목록. 새 도구를 만들면 여기에 추가한다.
TOOLS = [get_item_stock]
