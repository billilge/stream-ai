"""가짜 API가 돌려줄 데이터를 읽고 거르는 부분.

서버(`server.py`)와 나눠 둔 이유는 두 가지다. 이 파일은 FastAPI 없이도 돌아가서
필터 동작을 바로 확인할 수 있고, 진짜 백엔드가 나오면 서버만 버리고 이 규칙들을
`sync.py` 쪽과 대조하면 된다.

필터 규칙은 db-schema.md의 제약을 그대로 옮겼다.
- `isDeleted`가 참인 행은 기본적으로 내보내지 않는다. 동기화 쪽에서 "삭제됐다"는
  사실을 받아야 할 때만 `includeDeleted`로 켠다.
- `isPublished`가 거짓인 행도 내보내지 않는다. 운영진이 준비 중인 회차가 학생에게
  먼저 보이면 안 된다.
"""

import json
import os
from datetime import datetime

DATA_DIR = os.environ.get("STREAM_MOCK_DATA_DIR", "test_data")

DEFAULT_SIZE = 20
MAX_SIZE = 100


def load(name: str) -> dict:
    """test_data의 JSON 한 건을 읽는다. 호출할 때마다 읽어 파일을 고치면 바로 반영된다."""
    with open(os.path.join(DATA_DIR, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def _after(value: str | None, updated_after: str | None) -> bool:
    """updatedAfter 필터. 둘 다 ISO8601이라 문자열 비교로 충분하다."""
    if not updated_after:
        return True
    if not value:
        return False
    return value > updated_after


def _page(rows: list[dict], page: int, size: int) -> dict:
    """Spring Page 응답을 흉내 낸다. 실제 모양은 서준 확인 전이라 바뀔 수 있다."""
    size = max(1, min(size, MAX_SIZE))
    start = max(0, page) * size
    chunk = rows[start:start + size]
    return {
        "content": chunk,
        "page": max(0, page),
        "size": size,
        "totalElements": len(rows),
        "last": start + len(chunk) >= len(rows),
    }


def list_notices(updated_after: str | None = None, include_deleted: bool = False,
                 page: int = 0, size: int = DEFAULT_SIZE) -> dict:
    """공지 목록. 최신순으로 돌려준다.

    최신순으로 정렬해 주는 이유는 "최근 공지 3개" 질문 때문이다. 검색은 낱말이
    겹치는 정도만 보기 때문에 최신순은 조회 쪽에서 와야 한다.
    """
    rows = [n for n in load("notices").get("notices", [])
            if (include_deleted or not n.get("isDeleted"))
            and _after(n.get("updatedAt"), updated_after)]
    rows.sort(key=lambda n: (n.get("pinned", False), n.get("createdAt") or ""), reverse=True)
    return _page(rows, page, size)


def list_events(include_deleted: bool = False, include_unpublished: bool = False,
                page: int = 0, size: int = DEFAULT_SIZE) -> dict:
    """행사 목록. 행사 시작이 이른 순으로 돌려준다.

    `recruitStatus`로 거르지 않는다. 배치가 늦게 돌면 `applyEndAt`과 어긋날 수 있어서,
    "지금 신청 가능?"은 받는 쪽이 현재 시각과 `applyEndAt`을 비교하는 편이 안전하다.
    """
    rows = [e for e in load("events").get("events", [])
            if (include_deleted or not e.get("isDeleted"))
            and (include_unpublished or e.get("isPublished", True))]
    rows.sort(key=lambda e: e.get("eventStartAt") or "")
    return _page(rows, page, size)


def list_items(name: str | None = None) -> dict:
    """빌릴게 품목과 현재 수량. 이름은 부분 일치로 찾는다(백엔드 검색과 같은 방식).

    찾는 이름이 없어도 빈 목록을 돌려준다. 404로 막으면 부르는 쪽이 "없는 물품"과
    "조회 실패"를 구분하지 못한다.
    """
    rows = load("billilge").get("items", [])
    if name:
        key = name.replace(" ", "").lower()
        rows = [i for i in rows if key in i["name"].replace(" ", "").lower()]
    return {"items": rows}


def list_archives(page: int = 0, size: int = DEFAULT_SIZE) -> dict:
    """아카이빙 글. isDeleted가 없는 테이블이라 거를 것이 없다."""
    rows = sorted(load("notices").get("archives", []),
                  key=lambda a: a.get("createdAt") or "", reverse=True)
    return _page(rows, page, size)


def list_open_feedbacks(include_deleted: bool = False, page: int = 0,
                        size: int = DEFAULT_SIZE) -> dict:
    """열린피드백. 답변이 없는 글은 내보내지 않는다.

    질문만 검색에 걸리면 답이 없는 문서를 근거로 삼게 된다.
    """
    rows = [f for f in load("notices").get("openFeedbacks", [])
            if (include_deleted or not f.get("isDeleted")) and f.get("answer")]
    rows.sort(key=lambda f: f.get("questionedAt") or "", reverse=True)
    return _page(rows, page, size)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
