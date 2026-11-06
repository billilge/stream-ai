"""가짜 백엔드 API.

`tools.py`의 function calling과 `sync.py`가 진짜 백엔드 대신 부를 서버다.
JSON 파일을 직접 읽는 것과 다른 점은 주소로 부른다는 것이다. 진짜 API가 나오면
주소만 바꾸면 되고, 그러려고 목업을 만든 것이다.

경로와 응답 모양은 stream-server의 `GET /v1/app/billilge/items`를 본떴고 나머지는
같은 규칙으로 맞췄다. 서준 확인 전이라 바뀔 수 있다(docs/guides/mock-api.md 참고).

실행:
    uvicorn mock_api.server:app --port 8002

AI 서버와 포트가 겹치지 않게 8002를 쓴다.
"""

from fastapi import FastAPI, Query

from . import store

app = FastAPI(title="Stream 가짜 백엔드 API", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "now": store.now()}


@app.get("/v1/app/notices")
def notices(
    updatedAfter: str | None = Query(None, description="이 시각 이후에 바뀐 것만. ISO8601"),
    includeDeleted: bool = Query(False, description="삭제된 공지도 포함. 동기화용"),
    page: int = 0,
    size: int = store.DEFAULT_SIZE,
) -> dict:
    return store.list_notices(updatedAfter, includeDeleted, page, size)


@app.get("/v1/app/events")
def events(
    includeDeleted: bool = Query(False, description="삭제된 행사도 포함. 동기화용"),
    includeUnpublished: bool = Query(False, description="게시 전 회차도 포함. 운영 확인용"),
    page: int = 0,
    size: int = store.DEFAULT_SIZE,
) -> dict:
    return store.list_events(includeDeleted, includeUnpublished, page, size)


@app.get("/v1/app/billilge/items")
def items(name: str | None = Query(None, description="품목 이름 부분 일치")) -> dict:
    return store.list_items(name)


@app.get("/v1/app/archives")
def archives(page: int = 0, size: int = store.DEFAULT_SIZE) -> dict:
    return store.list_archives(page, size)


@app.get("/v1/app/open-feedbacks")
def open_feedbacks(
    includeDeleted: bool = Query(False),
    page: int = 0,
    size: int = store.DEFAULT_SIZE,
) -> dict:
    return store.list_open_feedbacks(includeDeleted, page, size)
