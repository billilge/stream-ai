# STREAM DB 스키마 (챗봇 참조용)

> 출처: Flyway V1–V4 / MySQL / 26테이블 213컬럼
> 이 문서는 **챗봇(stream-ai)이 어떤 데이터를 어떻게 쓸지** 판단하기 위한 참조용이다.
> 챗봇은 DB에 직접 붙지 않는다. 백엔드(Spring) API를 통해서만 접근한다.

---

## 1. 챗봇 관점 요약 (먼저 읽을 것)

### 답변 경로 분류

| 경로 | 테이블 | 이유 |
| --- | --- | --- |
| **RAG 인덱싱** | `notices`, `events`, `archives`, `archive_related_links`, `items`(품목 목록), `open_feedbacks` | 글 덩어리. "뭐라고 써 있어?" 류 |
| **실시간 조회** | `items.count`, `rental_histories` | 수량·상태가 계속 바뀜. 인덱싱하면 틀린 답이 나감 |
| **제외 (인증 필요)** | `members`, `event_applications`, `locker_applications`, `student_fees`, `payers`, `chat_messages`, `notifications` | 개인 데이터. 8주 범위 밖 |
| **제외 (내부 운영)** | `work_schedules`, `admin_config_values`, `display_*`, `rental_status_worker_logs` | 학생 대상 질문과 무관 |

같은 테이블도 질문에 따라 갈린다.
- "우산 어떻게 빌려요" → `items` RAG
- "우산 지금 남았어요" → `items.count` 실시간

### 반드시 지켜야 할 제약

**1. 소프트 삭제 필터링**
`is_deleted = 1`인 행은 절대 인덱싱하거나 답변에 쓰지 않는다.
적용 테이블: `members`, `events`, `lockers`, `notices`, `open_feedbacks`

삭제된 공지를 챗봇이 계속 답하는 것이 가장 흔한 사고다.
동기화(`sync.py`) 시 삭제 감지 로직 필수.

**2. 학년도·학기 필드가 없다** ⚠️
`notices`에 대상 학년도/학기 컬럼이 **존재하지 않는다.**
7주차 "지난 학기 공지 거르기"를 `created_at`만으로 처리해야 한다.

대응 방안 (택1, 서준과 협의 필요):
- (a) 백엔드에 컬럼 추가 요청 — 근본 해결이지만 마이그레이션 필요
- (b) 청킹 단계에서 제목·본문에서 "2026-2학기" 등을 정규식 추출 → 메타데이터에 저장
- (c) `created_at` 기준 N개월 이상 지난 공지는 답변 시 "오래된 공지" 경고 부착

현실적으로 **(b) + (c) 조합**이 8주 안에 가능하다.

**3. FK 제약이 없다**
참조는 모두 논리 관계. 무결성은 애플리케이션이 보장한다.
→ 챗봇은 조인 결과를 신뢰하지 말고, 참조가 끊긴 데이터(고아 행)를 만날 수 있다고 가정한다.

**4. 파일은 JSON 배열 참조**
`image_ids`, `attachment_ids`는 `files.file_id` 배열. 조인 테이블 없음.
→ 첨부파일(HWP/PDF)은 **파싱하지 않는다**. 링크로만 안내.
→ 본문이 이미지뿐인 공지는 RAG로 검색이 안 됨. 실제 비율 조사 필요.

**5. `updated_at`은 모든 주요 테이블에 있다** ✅
`ON UPDATE CURRENT_TIMESTAMP`로 자동 갱신.
→ `sync.py`의 증분 동기화(`updatedAfter` 필터)가 가능하다.
단, 로그성 테이블(`notifications`, `chat_messages`, `rental_status_worker_logs`, `display_posters`)은 `created_at`만 있음.

**6. PK 이름이 테이블마다 다르다**
대부분 `{단수}_id`지만 예외가 많다:
| 테이블 | PK |
| --- | --- |
| `items`, `admin_config_values`, `display_posters`, `display_calendar_schedules` | `id` |
| `rental_histories` | `history_id` |
| `chat_messages` | `message_id` |
| `open_feedbacks` | `feedback_id` |
| `work_schedules` | `schedule_id` |

→ `doc_id` 생성 시 `{table}-{pk}` 형태로 정규화할 것. 예: `notice-42`, `event-7`, `item-3`

---

## 2. RAG 대상 테이블 상세

### notices — 공지사항 (최우선)

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `notice_id` | BIGINT PK | → `doc_id` = `notice-{id}` |
| `title` | VARCHAR(200) | **출처 표시용**. 검색 가중치 높게 |
| `content` | TEXT | **청킹 대상** |
| `category` | VARCHAR(30) | GENERAL / PARTNERSHIP. 필터링에 사용 가능 |
| `pinned` | TINYINT(1) | 고정 공지. 검색 점수 보정에 쓸 수 있음 |
| `attachment_ids` | JSON | → `files[]`. **파싱 안 함**, 링크만 |
| `image_ids` | JSON | → `files[]`. 본문이 이미지뿐인 경우 주의 |
| `is_deleted` | TINYINT(1) | **필수 필터** |
| `created_at` | DATETIME | → `published_at`. 유효기간 판단의 유일한 단서 |
| `updated_at` | DATETIME | → 증분 동기화 기준 |

인덱스: `idx_notices_category_is_deleted (category, is_deleted)`
→ 카테고리 + 삭제여부 조회가 최적화돼 있음. 동기화 쿼리 설계 시 활용.

**없는 것**: 학년도/학기, 게시 종료일, 조회수, 작성자명(`created_by`는 member_id뿐)

### events — 행사

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `event_id` | BIGINT PK | → `doc_id` = `event-{id}` |
| `title` | VARCHAR(200) | 출처 표시 |
| `description` | TEXT | **청킹 대상** |
| `target` | VARCHAR(255) | 대상 (예: "AI학부 재학생") |
| `place` | VARCHAR(255) | 장소 |
| `event_start_at` / `event_end_at` | DATETIME | 행사 일시 |
| `apply_start_at` / `apply_end_at` | DATETIME | **신청 기간. "언제까지 신청?" 질문의 답** |
| `recruit_type` | VARCHAR(30) | FIRST_COME / OPEN |
| `capacity` | INT | 정원 |
| `recruit_status` | VARCHAR(30) | BEFORE_OPEN / OPEN / CLOSED |
| `is_deleted` | TINYINT(1) | **필수 필터** |

**주의**: `recruit_status`와 `apply_end_at`이 어긋날 수 있다 (배치가 늦게 돌면).
"지금 신청 가능?"은 `apply_end_at`과 현재 시각을 비교하는 쪽이 안전하다.

**RAG vs 실시간 경계**: 행사 소개글은 RAG, "이번 주 행사 목록"은 날짜 기반 조회.
같은 테이블이지만 질문 유형이 다르다. 6주차 function calling 설계 시 구분할 것.

### archives — 학생회 활동 아카이브

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `archive_id` | BIGINT PK | → `doc_id` = `archive-{id}` |
| `title` | VARCHAR(200) | 출처 표시 |
| `content` | TEXT | **청킹 대상** |
| `start_date` / `end_date` | DATE | 활동 기간 |
| `location` | VARCHAR(255) | 장소 |
| `department_name` | VARCHAR(100) | 주관 부서 |
| `image_ids` | JSON | → `files[]` |

**`is_deleted` 없음** — 아카이브는 소프트 삭제 대상이 아니다. 하드 삭제되면 동기화에서 사라진다.

관련 링크는 `archive_related_links`에 별도 저장 (`title`, `url`, `display_order`).
→ 답변에 참고 링크로 함께 노출 가능.

### items — 복지 물품 (RAG + 실시간 혼합) ⚠️

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `id` | BIGINT PK | → `doc_id` = `item-{id}` (PK 이름이 `id`임에 주의) |
| `name` | VARCHAR(255) | **RAG 대상**. "무슨 물품 있어요?" |
| `type` | VARCHAR(30) | CONSUMABLE / RENTAL |
| `count` | INT | **실시간 전용. 절대 인덱싱 금지** |
| `image_key` | VARCHAR(255) | 이미지 |

**이 테이블이 두 경로로 갈리는 지점이다.**
- 품목 목록·이용 안내 → RAG 인덱싱 OK
- `count` (재고) → 매번 백엔드 조회. 6주차 function calling 대상

`count`를 인덱싱하면 "우산 3개 남음"이라고 답했는데 실제로 0개인 상황이 나온다.
발표 장면 2("재고를 바꾼 뒤 다시 물으면 답이 바뀐다")가 이 구분에 걸려 있다.

`is_deleted` 없음. `updated_at`은 있음.

### open_feedbacks — 공개 피드백 Q&A

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `feedback_id` | BIGINT PK | → `doc_id` = `feedback-{id}` |
| `round` | INT | 회차 |
| `category` | VARCHAR(20) | **허용 값이 스키마에 정의돼 있지 않음** |
| `question` | VARCHAR(1000) | 질문 |
| `answer` | TEXT | 답변. **NULL 가능 → 미답변 건은 인덱싱 제외** |
| `is_deleted` | TINYINT(1) | **필수 필터** |

이미 Q&A 형태라 RAG에 잘 맞는다. `answer IS NULL`인 행은 제외.
단, 로드맵 8주 범위에서는 제외 항목("데이터가 아직 없음")이므로 우선순위 낮음.

---

## 3. 실시간 조회 대상

### items.count — 재고

6주차 function calling 대상. Gemini tool로 노출할 함수 형태 (제안):

```python
def get_item_stock(item_name: str) -> dict:
    """복지 물품의 현재 재고를 조회한다."""
    # → GET /api/items?name={item_name}
    return {"name": "우산", "type": "RENTAL", "count": 3}
```

### rental_histories — 대여 이력

| 컬럼 | 비고 |
| --- | --- |
| `rental_status` | CANCEL / CONFIRMED / PENDING / REJECTED / RENTAL / RETURNED / RETURN_CONFIRMED / RETURN_PENDING (8종) |
| `member_id` | **개인 데이터 → 8주 범위 제외** |

"내 대여 현황"은 인증이 필요해서 제외. 단 집계는 가능할 수 있다
(예: "우산은 보통 얼마나 대여되나요") — 필요성 낮으므로 후순위.

---

## 4. sync.py 설계 메모 (6주차)

### 증분 동기화 쿼리 모양

```
GET /api/notices?updatedAfter=2026-09-01T00:00:00&includeDeleted=true
```

`includeDeleted=true`가 중요하다. 삭제된 공지를 인덱스에서 제거하려면
"삭제됐다"는 사실을 전달받아야 한다. 목록에서 빠지기만 하면 챗봇은 알 수 없다.

### 처리 흐름

```
1. updatedAfter 이후 변경분 조회
2. is_deleted = 1 → 인덱스에서 제거
3. 나머지 → 청킹 후 인덱스 갱신
4. 마지막 성공 시각 저장
```

### 하드 삭제 대응

`archives`, `items`는 `is_deleted`가 없어 하드 삭제 가능성이 있다.
증분 동기화로는 감지 불가 → 주기적 전체 ID 대조가 필요할 수 있다.
8주 범위에서는 수동 재빌드로 충분.

---

## 5. 서준에게 확인할 것

백엔드에 물어볼 것은 이 목록으로 모은다. 로드맵에 흩어져 있던 백엔드 항목도 여기로 옮겼다.

### 응답 형식

DB 스키마만으로는 답이 나오지 않는 항목이다. 백엔드가 Spring이라 컬럼명과 JSON 필드명이
다르고, 그 차이는 `sync.py`가 흡수한다.

- [x] **JSON 필드명은 camelCase** (2026-09-20 확인).
      DB 컬럼 `notice_id` → 응답 `noticeId`, `apply_end_at` → `applyEndAt`,
      `recruit_status` → `recruitStatus`, `is_deleted` → `isDeleted`.
      **가짜 API 응답도 camelCase로 만든다.** 목업의 존재 이유가 진짜 API가 나왔을 때
      주소만 바꾸는 것이므로, 목업이 DB 컬럼명을 쓰면 교체 시점에 `sync.py`를 다시 써야 한다
- [x] **날짜·시간은 ISO8601** (2026-09-20 확인). 예: `2026-09-04T18:00:00`
- [ ] 조회 엔드포인트 주소 — 아직 모름. 3주차 `sync.py` 전까지 필요하다

아래 표는 **DB 컬럼 기준**이다. API 응답 필드명은 위 규칙대로 camelCase로 바꿔 읽는다.

### 조회 범위

- [ ] 조회 API에 `updatedAfter` 필터 제공 가능 여부
- [ ] 삭제된 항목을 동기화에 포함하는 방법 (`includeDeleted` 파라미터 등).
      응답에 `is_deleted`가 실려 오는지, 아니면 백엔드가 이미 걸러서 주는지
- [ ] `archives`, `items` 하드 삭제 여부
- [ ] 사물함 구역별 잔여 수량을 인증 없이 조회할 수 있는지.
      `lockers` · `locker_periods`에서 구역별 전체 수와 사용 중인 수를 받고 싶다

### 필드 의미

- [ ] `items.count`가 지금 빌릴 수 있는 수량인지, 전체 보유 수량인지
- [ ] `notices`에 대상 학년도·학기 컬럼 추가 가능 여부 (불가하면 정규식 추출로 우회)
- [ ] `events.recruit_status` 갱신 주기 (배치인지 실시간인지)
- [ ] `events.recruit_status`에 취소 상태가 있는지.
      스키마에는 `BEFORE_OPEN / OPEN / CLOSED`뿐인데 취소된 행사가 실제로 존재한다

### 인증

- [ ] 서버 간 인증 방식 (내부 토큰 등)

---

## 6. 전체 테이블 목록 (참조)

### member (V1)
`members` · `member_term_agreements` · `member_notification_settings` · `notifications`

### event (V2)
`events` · `event_questions` · `event_applications` · `event_application_answers` ·
`lockers` · `locker_periods` · `locker_applications` · `archives` · `archive_related_links`

### welfare (V3)
`items` · `rental_histories` · `rental_status_worker_logs` · `student_fees` · `payers` ·
`notices` · `chat_messages` · `open_feedbacks`

### internal (V4)
`work_schedules` · `admin_config_values` · `display_posters` · `display_calendar_schedules` · `files`

---

## 7. 공통 규칙 (원문)

- **FK 제약 없음** — 논리 관계만. 무결성은 애플리케이션 책임, 조회용 인덱스만 존재
- **타임스탬프** — `created_at`은 `CURRENT_TIMESTAMP` 기본값, `updated_at`은 `ON UPDATE`로 자동 갱신. 로그성 테이블은 `created_at`만
- **소프트 삭제** — `is_deleted TINYINT(1)`. `members`, `events`, `lockers`, `notices`, `open_feedbacks`에만 적용
- **enum은 문자열** — 상태·유형은 VARCHAR에 enum 이름 그대로 저장
- **파일 참조는 JSON 배열** — `image_ids`, `attachment_ids`는 `files.file_id` 배열, 조인 테이블 없음
- **boolean** — `TINYINT(1)`, 0/1

### 허용 값이 스키마에 정의되지 않은 컬럼
`chat_messages.sender_type` · `open_feedbacks.category` ·
`work_schedules.day_of_week` · `notifications.type`

→ 값 종류를 백엔드 코드에서 확인해야 한다.

### 유니크가 아닌 일반 인덱스
`event_applications (event_id, member_id)`, `locker_applications (locker_period_id, locker_id)`
→ 중복 신청이 DB에서 강제 차단되지 않는다. 데이터에 중복이 있을 수 있다고 가정.
