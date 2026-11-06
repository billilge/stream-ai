# 가짜 백엔드 API

`tools.py`의 function calling과 나중의 `sync.py`가 진짜 백엔드 대신 부르는 서버다.
JSON 파일을 직접 읽지 않고 주소로 부르는 것이 전부인데, 그래야 진짜 API가 나왔을 때
주소만 바꾸면 된다.

## 실행

```bash
uvicorn mock_api.server:app --port 8002 --reload
```

AI 서버(8000)와 도커 테스트(8001)에 겹치지 않게 8002를 쓴다.
`--reload`를 붙이면 `test_data/*.json`을 고치고 바로 다시 부를 수 있다. 재고를 바꾼 뒤
다시 물으면 답이 바뀌는지 확인할 때 쓴다.

데이터 위치는 환경변수로 바꾼다.

```bash
STREAM_MOCK_DATA_DIR=test_data uvicorn mock_api.server:app --port 8002
```

## 엔드포인트

| 경로 | 돌려주는 것 | 파라미터 |
| --- | --- | --- |
| `GET /health` | 상태와 현재 시각 | — |
| `GET /v1/app/notices` | 학생회 공지, 최신순 | `updatedAfter`, `includeDeleted`, `page`, `size` |
| `GET /v1/app/events` | 행사, 시작 이른 순 | `includeDeleted`, `includeUnpublished`, `page`, `size` |
| `GET /v1/app/billilge/items` | 품목과 현재 수량 | `name` (부분 일치) |
| `GET /v1/app/archives` | 아카이빙 글 | `page`, `size` |
| `GET /v1/app/open-feedbacks` | 답변이 달린 열린피드백 | `includeDeleted`, `page`, `size` |

목록 응답은 Spring Page를 흉내 냈다.

```json
{ "content": [ ... ], "page": 0, "size": 20, "totalElements": 10, "last": true }
```

재고만 모양이 다르다. `{ "items": [ ... ] }`.

## 기본으로 걸러지는 것

db-schema.md의 제약을 그대로 옮겼다. 파라미터로 켜지 않는 한 응답에 들어가지 않는다.

- `isDeleted`가 참인 행 — 삭제된 공지를 챗봇이 계속 답하는 것이 가장 흔한 사고다.
  동기화가 "삭제됐다"는 사실을 받아야 할 때만 `includeDeleted=true`로 켠다.
- `isPublished`가 거짓인 행사 — 운영진이 준비 중인 회차가 학생에게 먼저 보이면 안 된다.
- 답변이 없는 열린피드백 — 질문만 검색에 걸리면 답이 없는 문서를 근거로 삼게 된다.

필터가 빠졌는지 확인할 데이터를 목업에 심어 뒀다.

| 행 | 상태 | 필터가 없으면 |
| --- | --- | --- |
| `event 9` | `isDeleted: true` | 체육대회 날짜가 10/7과 10/8 두 개로 나온다 |
| `event 10` | `isPublished: false` | 아직 공개 안 한 송년의 밤을 챗봇이 먼저 알려준다 |
| `notice 13` | `isDeleted: true` | 슬랑제 신청 기간을 9/21부터로 잘못 답한다 |
| `archive 2` | 지난 학기 글 | 삭제가 아니라 날짜로 걸러야 한다. 서버는 거르지 않는다 |

## 확인해 봤을 때

```bash
curl -s "localhost:8002/v1/app/notices?size=3" | jq '.content[].title'
curl -s "localhost:8002/v1/app/events" | jq '[.content[].eventId]'            # 9, 10 빠짐
curl -s "localhost:8002/v1/app/billilge/items?name=우산" | jq '.items[0].count'
curl -s "localhost:8002/v1/app/billilge/items?name=드라이기" | jq '.items'      # 빈 목록
```

없는 품목에 404를 주지 않고 빈 목록을 주는 이유는, 부르는 쪽이 "없는 물품"과
"조회 실패"를 구분해야 하기 때문이다. 조회 실패일 때 모델이 수량을 지어내면 안 된다.

## 아직 정해지지 않은 것

진짜 API가 나오면 바뀔 수 있는 부분이다. 지금은 가정으로 두고 맞춰 뒀다.

- **응답 껍데기** — Spring Page(`content`/`page`/`size`)로 가정했다. 재고만 `items`인데
  이건 `tools.py`가 이미 쓰는 모양에 맞춘 것이다. 통일할지 정해야 한다.
- **필드명과 날짜** — camelCase, ISO8601 `+09:00`. Jackson 기본값을 근거로 한 가정이다.
  특히 `isDeleted`는 Jackson이 `deleted`로 내보내는 경우가 흔해서 따로 물어야 한다.
- **엔드포인트 주소** — `/v1/app/billilge/items` 외에는 같은 규칙으로 지었다.
- **`count`의 뜻** — 지금 빌릴 수 있는 수량으로 가정했다. 전체 보유 수량일 수도 있다.
- **`maxRentalDays`·`returnDeadline`** — V10에서 품목별 값이 됐는데 기존 행은 비어 있다.
  목업도 일부러 일부만 채웠다. 비어 있는 품목은 반납 기한을 단정하면 안 된다.
- **사물함** — `lockers.json`은 아직 옛 `zones` 구조 그대로다. V9에서 `locker_sections`가
  생겼고 구역별 잔여 수량을 백엔드가 집계해 줄지 행을 그대로 줄지 모른다. 그 답이 온 뒤에
  고치고 엔드포인트를 붙인다.
