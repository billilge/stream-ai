# STREAM DB 스키마 (챗봇 참조용)

> 출처: Flyway **V1–V14** / MySQL / 테이블 29개
> 확인일: 2026-10-04 (`billilge/stream-server` 마이그레이션 원문)
> 이 문서는 **챗봇(stream-ai)이 어떤 데이터를 어떻게 쓸지** 판단하기 위한 참조용이다.
> 챗봇은 DB에 직접 붙지 않는다. 백엔드(Spring) API를 통해서만 접근한다.

---

## 0. V5 이후 변경 이력 (2026-10-04 확인)

이 문서의 초안은 V1–V4 기준이었다. 그 뒤 마이그레이션이 V14까지 왔고,
챗봇 범위에 영향이 있는 것만 적는다. 나머지(V6·V7·V8·V11)는 인덱스 교체와
학생회비 테이블 개편이라 우리가 쓰는 데이터 모양을 바꾸지 않는다.

| 버전 | 변경 | 우리에게 미치는 영향 |
| --- | --- | --- |
| V5 | `events.is_published` 추가 | **필수 필터.** 목록 API가 `is_deleted = 0 AND is_published = 1`로 거른다 |
| V9 | `locker_sections` 신설 · `lockers.section` 문자열 → `section_id` FK · `locker_number` INT · **`locker_periods.is_published` 추가** | 사물함 신청 기간·방법은 문서형(범위 내)이다. **게시 필터가 하나 더 있다** |
| V10 | `items`에 `category` · `max_rental_days` · `return_deadline` 추가 | 대여 기간과 반납 마감이 **품목별 DB 값**이 됐다. 아래 `items` 절 참고 |
| V12 | `feedback_rounds` 신설 · `open_feedbacks`에 `year`·`questioned_at` 추가 · **`category` 삭제** · `question` VARCHAR(500) | 목업과 `build_index.py` 로더가 어긋난다 |
| V13 | `oauth_accounts` 신설 (provider 로그인) | 개인별 데이터(7주차 조건부)의 전제가 생겼다 |
| V14 | `members.academic_status` 추가 (재학·휴학·졸업 원문, NULL 허용) | 자격 조건 질문("휴학생도 신청 되나요")의 근거가 될 수 있다. 다만 **개인별**이라 인증 뒤 |

### 감사 컬럼은 상속이다

`created_at`·`updated_at`·`is_deleted`는 테이블 정의에 직접 안 보이는 경우가 많다.
엔티티가 `BaseTimeEntity` / `BaseCreatedTimeEntity` / `BaseSoftDeleteEntity`를 상속해 받기 때문이다.
예: `NoticeJpaEntity extends BaseSoftDeleteEntity`라서 `notices.created_at`이 존재한다.

스키마 그림이나 엔티티 파일에 그 컬럼이 없다고 "없다"고 판단하면 안 된다.
`created_at`은 `insertable = false`(DB 기본값으로만 채움)인 곳이 있어, 앱이 시각을 직접 알아야 하는
자리에는 전용 컬럼을 따로 둔다(`open_feedbacks.questioned_at`, `locker_applications.applied_at`).

## 1. 챗봇 관점 요약 (먼저 읽을 것)

### 답변 경로 분류

| 경로 | 테이블 | 이유 |
| --- | --- | --- |
| **RAG 인덱싱** | `notices`, `events`, `archives`, `archive_related_links`, `items`(품목 목록), `open_feedbacks` | 글 덩어리. "뭐라고 써 있어?" 류 |
| **실시간 조회** | `items.count`, `rental_histories`, `lockers`·`locker_periods`(구역별 잔여 수량·신청 기간) | 수량·상태가 계속 바뀜. 인덱싱하면 틀린 답이 나감 |
| **제외 (인증 필요)** | `members`, `event_applications`, `locker_applications`, `student_fees`, `payers`, `chat_messages`, `notifications` | 개인 데이터. 6주 범위 밖. 사물함도 "내 사물함 위치"만 제외하고 구역별 잔여 수량은 위 실시간 조회로 다룬다 |
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

**1-2. 게시 여부 필터는 두 테이블에 있다** ⚠️
`is_deleted`와 **별개의 필터**다. 기본값이 `0`이라 발행하지 않은 것은 학생에게 보이지 않아야 한다.

| 테이블 | 추가 버전 | 목록 조회 조건 |
| --- | --- | --- |
| `events` | V5 | `is_deleted = 0 AND is_published = 1` |
| `locker_periods` | V9 | `is_published = 1` |

V9 주석의 표현 그대로다 — "운영진이 준비 중인 회차가 학생에게 노출되지 않도록".
**`locker_periods`를 빠뜨리면 아직 공개하지 않은 사물함 신청 기간을 챗봇이 먼저 알려준다.**
관리자 게시 API가 생기기 전까지는 운영진이 DB에서 직접 켜므로, 목업에도 양쪽 값이 섞여 있어야 한다.

**2. 학년도·학기 필드가 없다** ⚠️
`notices`에 대상 학년도/학기 컬럼이 **존재하지 않는다.**
3주차 "지난 학기 공지 거르기"를 `created_at`만으로 처리해야 한다.

대응 방안 (택1, 서준과 협의 필요):
- (a) 백엔드에 컬럼 추가 요청 — 근본 해결이지만 마이그레이션 필요
- (b) 청킹 단계에서 제목·본문에서 "2026-2학기" 등을 정규식 추출 → 메타데이터에 저장
- (c) `created_at` 기준 N개월 이상 지난 공지는 답변 시 "오래된 공지" 경고 부착

현실적으로 **(b) + (c) 조합**이 6주 안에 가능하다.

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
| `is_published` | TINYINT(1) | **필수 필터**. 기본값 `0` (V5 마이그레이션에서 추가) |

**행사는 필터가 두 개다**: 목록 API가 `is_deleted = 0 AND is_published = 1`로 거른다.
`is_published` 기본값이 `0`이라 발행하지 않은 행사는 나오지 않는다. 행사 수가 예상보다
적게 오면 이 컬럼을 먼저 의심한다.

**주의**: `recruit_status`와 `apply_end_at`이 어긋날 수 있다 (배치가 늦게 돌면).
"지금 신청 가능?"은 `apply_end_at`과 현재 시각을 비교하는 쪽이 안전하다.

**RAG vs 실시간 경계**: 행사 소개글은 RAG, "이번 주 행사 목록"은 날짜 기반 조회.
같은 테이블이지만 질문 유형이 다르다. 4주차 function calling 설계 시 구분할 것.

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
| `category` | VARCHAR(30) | V10 추가. ELECTRONICS / DAILY_SUPPLIES / MEDICINE / HYGIENE |
| `type` | VARCHAR(30) | CONSUMABLE / RENTAL |
| `count` | INT | **실시간 전용. 절대 인덱싱 금지** |
| `image_key` | VARCHAR(255) | 이미지 |
| `max_rental_days` | INT NULL | V10 추가. 대여일부터 최대 대여 가능 일수(`0`이면 당일 반납). RENTAL만 값이 있다 |
| `return_deadline` | TIME NULL | V10 추가. 반납 마감 시각(KST). RENTAL만 값이 있다 |

**이 테이블이 두 경로로 갈리는 지점이다.**
- 품목 목록·이용 안내 → RAG 인덱싱 OK
- `count` (재고) → 매번 백엔드 조회. 4주차 function calling 대상

`count`를 인덱싱하면 "우산 3개 남음"이라고 답했는데 실제로 0개인 상황이 나온다.
발표 장면 2("재고를 바꾼 뒤 다시 물으면 답이 바뀐다")가 이 구분에 걸려 있다.

`is_deleted` 없음. `updated_at`은 있음.

#### ⚠️ 대여 기간이 품목별 값이 됐다 (V10)

`docs/seed/borrow-guide.md`는 지금 이렇게 단정한다.

> 대여 기간은 최대 3일이다.

V10 이후 이건 **품목마다 다른 값**(`max_rental_days`)이고, 반납 마감 시각도 품목별(`return_deadline`)이다.
게다가 마이그레이션 주석에 경고가 있다 — 기존 RENTAL 행은 둘 다 `NULL`로 남아
"반납 마감이 계산되지 않는다"고 적혀 있다. 운영진이 직접 채워야 하는 값이다.

그래서 질문 단위로 갈린다(AGENTS.md 데이터 분류).

| 질문 | 분류 | 근거 |
| --- | --- | --- |
| "빌릴게 어떻게 빌려요" | 문서형 | 예약 → 학생회실 방문 흐름. 품목과 무관하다 |
| "우산 며칠까지 빌릴 수 있어요" | **실시간** | `max_rental_days`가 품목별이다. 시드 문서에 쓰면 틀린 답이 고정된다 |
| "몇 시까지 반납해야 해요" | **실시간** | `return_deadline`이 품목별이고 NULL일 수 있다 |

시드 문서에서 "최대 3일"을 빼고 "품목마다 다르니 빌릴게 화면에서 확인"으로 바꾸는 것이 맞다.
`count`와 같은 이유다 — 바뀌는 값을 문서에 적으면 인덱스를 다시 만들 때까지 틀린 답을 계속 한다.
다만 `max_rental_days`는 `count`만큼 자주 바뀌지 않으므로, 4주차 `tools.py`에 넣을지
시드 문서에서 단정만 빼고 둘지는 점수로 판단한다.

### open_feedbacks — 공개 피드백 Q&A

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `feedback_id` | BIGINT PK | → `doc_id` = `feedback-{id}` |
| `year` | INT | **V12 추가.** 회차가 연도별로 1차부터 다시 시작한다 |
| `round` | INT | 회차. `year`와 함께 봐야 식별된다 |
| `question` | VARCHAR(500) | V12에서 1000 → 500 |
| `questioned_at` | DATETIME | **V12 추가.** 게시일로 이걸 쓴다. `created_at`이 아니다 |
| `answer` | TEXT | 답변. **NULL 가능 → 미답변 건은 인덱싱 제외** |
| `answered_at` | DATETIME | 답변 시각 |
| `is_deleted` | TINYINT(1) | **필수 필터** |

**`category`는 V12에서 삭제됐다.** "화면에 카테고리 선택 UI가 없어서" 제거했다는 주석이 있다.
목업(`test_data/notices.json`의 `openFeedbacks`)에는 아직 `category`가 있고 `year`·`questionedAt`이 없다.

`build_index.py`가 게시일로 `createdAt`을 읽는데, 이 테이블은 `questioned_at`이 전용 컬럼이다.
`created_at`은 `insertable = false`라 앱이 값을 모르는 자리여서 따로 둔 것이다.

이미 Q&A 형태라 RAG에 잘 맞는다. `answer IS NULL`인 행은 제외.
단, 데이터가 아직 1건뿐이라 우선순위는 낮다.

### feedback_rounds — 피드백 회차 (V12 신설)

| 컬럼 | 타입 | 챗봇 용도 |
| --- | --- | --- |
| `feedback_round_id` | BIGINT PK | |
| `year`, `round` | INT | UNIQUE (year, round) |
| `opens_at`, `closes_at` | DATETIME | **"열린피드백 언제까지 받아요?"의 근거** |

접수 기간이 있는 테이블이라 **실시간**으로 갈린다. "지금 남긴 수 있나"는 현재 시각과 비교해야 한다.
인덱싱하면 지난 회차 기간을 답한다.

### lockers · locker_sections · locker_periods — 사물함 (V9 개편)

사물함은 질문에 따라 세 갈래로 갈린다(10주 로드맵 "사물함은 개인별 항목만 뺀다").

| 테이블 | 주요 컬럼 | 챗봇 용도 |
| --- | --- | --- |
| `locker_periods` | `name`, `apply_start_at`, `apply_end_at`, `usage_start_at`, `usage_end_at`, **`is_published`** | 신청 기간. **문서형 + 실시간** |
| `locker_sections` | `locker_section_id`, `label` UNIQUE | 구역 라벨("A-1", "A-2", "C"). V9에서 문자열 컬럼에서 테이블로 승격 |
| `lockers` | `section_id` FK, `locker_label`, `locker_number` INT, `row_no`, `column_no`, `status`, `is_deleted` | 구역별 잔여 수량 → **실시간** |

- "사물함 신청 언제 어떻게 해요" → **문서형**. 공지에 있다
- "A-1 구역 몇 자리 남았어요" → **실시간**. `lockers.status`를 세야 한다. 인덱싱 금지
- "내 사물함 어디예요" → **개인별**. `locker_applications`. 인증 필요(7주차 조건부)

`locker_number`가 V9에서 INT로 바뀐 이유가 주석에 있다 — 문자열이면 정렬이 `"1","10","2"`가 된다.
`locker_applications`는 회차당 회원 1개, 회차당 사물함 1명으로 UNIQUE가 걸려 있고 **취소가 없다.**
"신청 취소 어떻게 해요"의 답이 "사물함은 취소가 없다"일 수 있으므로 시드 문서와 대조할 것.

---

## 3. 실시간 조회 대상

### items.count — 재고

4주차 function calling 대상. Gemini tool로 노출할 함수 형태 (제안):

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
| `member_id` | **개인 데이터 → 6주 범위 제외** |

"내 대여 현황"은 인증이 필요해서 제외. 단 집계는 가능할 수 있다
(예: "우산은 보통 얼마나 대여되나요") — 필요성 낮으므로 후순위.

---

## 4. sync.py 설계 메모 (5주차)

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
6주 범위에서는 수동 재빌드로 충분.

---

## 5. API 계약

2026-09-21에 백엔드 저장소를 직접 읽어 대부분을 확정했다. 두 곳 모두 public이라 물어보지 않고
응답 DTO와 컨트롤러를 그대로 확인할 수 있었다.

- `billilge/stream-server` — 공지·행사 (Java, Spring)
- `billilge/backend` — 빌릴게 재고 (Kotlin, Spring). **별도 저장소이고 응답 모양도 다르다**

### 확인된 것

**필드명은 camelCase다.** Jackson naming strategy를 설정한 곳이 없어 기본값이 그대로 적용된다.

```java
// NoticeListItemResponse.java
Long noticeId, String title, NoticeCategory category,
LocalDateTime createdAt, String thumbnailUrl, boolean pinned

// EventListItemResponse.java
Long eventId, String title, String target,
LocalDateTime eventStartAt, String thumbnailUrl,
LocalDateTime applyStartAt, LocalDateTime applyEndAt,
RecruitStatus recruitStatus, Integer daysUntilDeadline
```

```kotlin
// ItemDetail.kt (빌릴게)
val itemId: Long, val itemName: String, val itemType: ItemType,
val count: Int, val imageUrl: String
```

빌릴게는 이름 필드가 `name`이 아니라 **`itemName`**이다.

**날짜는 ISO-8601이지만 타임존 오프셋이 없다.** 타입이 `LocalDateTime`이라 존 정보를 갖지 않는다.
`"2026-09-04T18:00:00"` 형태로 온다. 가정하던 `+09:00`은 붙지 않으므로 목업도 빼야 한다.

`app.py`는 오늘 날짜를 `Asia/Seoul`로 계산해 프롬프트에 넣는다. 응답이 존 없는 값이므로
비교할 때 한국 시간으로 간주한다. 백엔드가 한국에서만 쓰이므로 당장은 문제가 없지만,
서버가 해외 리전에 뜨면 어긋날 수 있다. 비교 코드에서 존을 명시한다.

**엔드포인트**

| 대상 | 주소 | 비고 |
| --- | --- | --- |
| 공지 목록 | `GET /v1/app/notices` | `?category= &cursor= &size=20` |
| 공지 상세 | `GET /v1/app/notices/{noticeId}` | |
| 행사 목록 | `GET /v1/app/events` | `?recruitStatus= &cursor= &size=` |
| 행사 상세 | `GET /v1/app/events/{eventId}` | |
| 아카이브 | `GET /v1/app/archives` | |
| 빌릴게 재고 | `GET /items` | **다른 서버** |

**응답 껍데기가 두 저장소에서 다르다.** `sync.py`가 각각 다르게 벗겨야 한다.

```jsonc
// stream-server — 두 겹
{ "success": true, "code": "SUCCESS", "message": "요청에 성공했습니다.",
  "data": { "content": [ ... ], "hasNext": true, "nextCursor": "eyJ..." } }

// 빌릴게 — 껍데기 없음
{ "items": [ ... ] }
```

**페이지네이션은 커서 기반이다.** `updatedAfter` 필터가 아니다. `hasNext`가 `false`가 될 때까지
`nextCursor`를 따라가며 전부 가져온다. AGENTS.md대로 매번 전체를 다시 만드는 방식과 맞는다.

**`items.count`는 지금 빌릴 수 있는 수량이다.** 전체 보유 수가 아니다.
`RentalService`가 대여 시 `subtractCount`, 반납 시 `addCount`를 호출한다.
단 `ItemType.CONSUMPTION`(소모품)은 반납해도 복구되지 않는다.

**`recruitStatus`는 `BEFORE_OPEN` / `OPEN` / `CLOSED` 셋뿐이다.** 취소 상태가 없다.

**삭제 필터는 백엔드가 한다.** 응답 DTO에 `isDeleted`가 없고, 조회 쿼리가 직접 거른다.

```java
// NoticeJpaRepository
WHERE n.isDeleted = false

// EventJpaRepository
WHERE e.isDeleted = false
AND e.isPublished = true
```

**행사에는 필터가 두 개다.** `is_published`는 스키마 문서 초안에 없던 컬럼이다.

```sql
-- V5__add_is_published_to_events.sql
ALTER TABLE events ADD COLUMN is_published TINYINT(1) NOT NULL DEFAULT 0;
```

**기본값이 `0`이라 발행하지 않은 행사는 목록에 나오지 않는다.** 우리는 목록 API만 쓰므로
따로 걸 필요가 없지만, 행사 수가 예상보다 적게 오면 이 컬럼을 먼저 의심한다.

그래서 우리 쪽 `is_deleted` 필터는 **안전망으로만** 둔다. 백엔드를 거치지 않는 경로(가짜 API,
시드 문서)가 있는 동안에는 여전히 필요하다.

### 확정된 정책 (2026-09-23, 서준 확인)

- [x] **AI 서버가 `/v1/app/` 아래를 그대로 호출한다.** 서버 간 전용 엔드포인트를 따로 내지 않는다
- [x] **서버 간 인증은 서준이 붙인다.** 방식은 아직 정해지지 않았다.
      3주차 `sync.py`가 실제 호출을 하기 전까지 형태를 받아야 한다
- [x] **삭제 필터는 백엔드가 한다** (저장소 확인). 서준도 확답을 주지 못했으나 쿼리로 확인됐다

### 목업이 실제와 어긋난 것 (2026-10-04 확인)

필드명 정정은 `test_data/`를 만드는 쪽(이승빈)이 하고, 여기 목록을 근거로 쓴다.

| 파일 | 지금 | 실제 |
| --- | --- | --- |
| `test_data/billilge.json` | `itemId`, `itemName`, `itemType`, `imageUrl` | `id`, `name`, `type`, `imageKey` |
| `test_data/billilge.json` | 없음 | `category`, `maxRentalDays`, `returnDeadline` 추가 필요 |
| `test_data/notices.json` `openFeedbacks` | `category`, `createdAt` | `category` 삭제됨 · `year`, `questionedAt`, `answeredAt` 필요 |
| `test_data/events.json` | `isPublished` 없음 | **필수 필터.** 발행/미발행 섞어 넣어야 한다 |
| `test_data/lockers.json` | `zones`(`zoneId`, `building`, `floor`, `occupied`, `total`) | `lockers` + `locker_sections` 구조. `locker_periods.isPublished`도 필요 |

`build_index.py`도 함께 봐야 한다 — `openFeedbacks`의 게시일을 `createdAt`에서 읽는데
실제 전용 컬럼은 `questioned_at`이다.

### 남은 질문

- [ ] 취소된 행사는 어떻게 표현되는지. `recruitStatus`에 `BEFORE_OPEN` / `OPEN` / `CLOSED`뿐이다.
      `is_published`를 0으로 내려 목록에서 빼는 방식일 수도 있다
- [ ] `notices`에 대상 학년도·학기 컬럼 추가 가능 여부 (불가하면 정규식 추출로 우회)
- [ ] `events.recruit_status` 갱신 주기 (배치인지 실시간인지)
- [ ] `archives`, `items` 하드 삭제 여부
- [ ] 사물함 구역별 잔여 수량을 인증 없이 조회할 수 있는지.
      `lockers` · `locker_periods` 관련 컨트롤러가 아직 stream-server에 없다

아래 표는 **DB 컬럼 기준**이다. API 응답 필드명은 위 규칙대로 camelCase로 바꿔 읽는다.

---

## 6. 전체 테이블 목록 (참조)

### member (V1)
`members` · `member_term_agreements` · `member_notification_settings` · `notifications`

### event (V2, V9에서 추가)
`events` · `event_questions` · `event_applications` · `event_application_answers` ·
`lockers` · `locker_periods` · `locker_applications` · `archives` · `archive_related_links` ·
**`locker_sections`**(V9)

### welfare (V3, V12에서 추가)
`items` · `rental_histories` · `rental_status_worker_logs` · `payers` ·
`notices` · `chat_messages` · `open_feedbacks` · **`feedback_rounds`**(V12)

`student_fees`는 V8에서 `student_transfer_statuses`로 이름이 바뀌었다.

### internal (V4)
`work_schedules` · `admin_config_values` · `display_posters` · `display_calendar_schedules` · `files`

### auth (V13 신설)
`oauth_accounts`

### 챗봇 관련 테이블은 없다

`chat_messages`(V3, 변경 없음)가 유일하다. 컬럼은 `message_id`·`member_id`·`sender_type`·`content`이고
`created_at`은 `BaseCreatedTimeEntity`에서 온다. **FAQ·추천 질문 테이블은 V14까지 없다.**
`GET /chat/suggested-questions`를 7주차로 미룬 판단이 2026-10-04에도 유효하다(10주 로드맵 참고).

`sender_type`이 user/model을 구분하므로 **백엔드가 대화 이력을 꺼내 보낼 수 있다.**
AI 서버는 요청마다 `messages`로 이력을 통째로 받는다.

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
