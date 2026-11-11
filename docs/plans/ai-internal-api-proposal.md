# AI 서버용 읽기 전용 내부 API 제안

> 작성일: 2026-11-09
> 작성: 이상현
> 대상: 서준(백엔드 리뷰), 승빈(`sync.py`)
> 상태: **구현 중** (stream-server #311). 결정 ①~④는 아래 제안대로 진행하고 PR 리뷰에서 확인받는다
> 관련: stream-server #310(학생 JWT 전달), stream-ai #70(재고 실데이터), `docs/plans/10-week-roadmap.md` 「학생 토큰 전달」

## 한 줄 요약

챗봇이 공지·행사·아카이빙·열린피드백을 **미리 모아 인덱싱**할 수 있게, AI 서버만 부를 수 있는 **읽기 전용** API를 백엔드에 추가한다.
학생 화면이 쓰는 조회 서비스를 그대로 재사용해서, **학생에게 보이는 것만** 내보낸다.

## 왜 필요한가

챗봇이 다루는 데이터는 두 갈래다(stream-ai `AGENTS.md` 데이터 분류).

| 갈래 | 예 | 방식 | 권한 |
| --- | --- | --- | --- |
| 질문할 때 조회 | 재고, 행사 모집 현황, 내 대여·내 신청 | 도구 호출 | **학생 JWT** (#310으로 해결) |
| 미리 모아 인덱싱 | 공지, 행사 소개, 아카이빙, 열린피드백 답변 | `sync.py`가 주기적으로 수집 | **없음 ← 이 문서** |

`sync.py`는 학생 질문 없이 혼자 돈다. 넘겨받을 학생 JWT가 없어서 지금 백엔드 API로는 공지를 가져올 수 없다.

검토한 다른 방법:

| 방법 | 채택하지 않은 이유 |
| --- | --- |
| 챗봇 전용 학생 JWT 발급 | 학생 권한이 통째로 들어 있다(대여 신청 등 쓰기 포함). 새면 막는 방법이 JWT 비밀키 교체(전체 로그아웃)뿐이다 |
| 공지를 질문할 때마다 실시간 조회 | 백엔드 공지 검색은 제목 LIKE라 "코딩 대회"로 "해커톤"을 못 찾는다. 모델을 한 번 더 불러 1~2초 느려진다 |
| 내부 토큰이면 기존 `/v1/app/**` GET을 열어 주기 | 컨트롤러는 안 늘지만 `AppApiUser`(학생 ID)를 기대하는 엔드포인트가 깨지고, 학생 role 모델에 예외가 생긴다 |

## 설계

### 1. 위치: `api/ai-api` 모듈 신설, 경로 `/v1/ai/**`

컨벤션(architecture.md 2-2절)이 `api:*`를 **클라이언트 기준**으로 나눈다. AI 서버는 학생 앱·운영진 콘솔과 다른 세 번째 클라이언트다.

```
api/
├── common-api
├── admin-api      # ADMIN     /v1/admin/**
├── app-api        # STUDENT   /v1/app/**
└── ai-api         # AI 서버   /v1/ai/**     ← 신규
    └── kr.ac.kookmin.stream.api.ai
        ├── welfare/notice       AiNoticeController
        ├── welfare/feedback     AiFeedbackController
        ├── event/event          AiEventController
        └── event/archive        AiArchiveController
```

- 컨트롤러는 **조회(GET)만** 둔다. 쓰기 엔드포인트는 이 모듈에 만들지 않는다
- 도메인 서비스는 **학생 화면이 쓰는 것을 그대로** 쓴다. 노출 규칙(삭제·미게시·미노출 제외)이 학생 화면과 자동으로 같아진다
- `bootstrap`에 `implementation(project(":api:ai-api"))` 추가, `settings.gradle.kts`에 include

### 2. 인증: 별도 `SecurityFilterChain` + 내부 토큰 필터

기존 학생·운영진 규칙(`SecurityConfig`)은 **건드리지 않고** 옆에 체인을 하나 더 둔다.

```java
// gateway:auth — AiServerSecurityConfig (신규)
@Bean
@Order(1)  // 기존 체인보다 먼저. /v1/ai/** 만 이 체인이 맡는다
SecurityFilterChain aiServerFilterChain(HttpSecurity http) throws Exception {
    return http
        .securityMatcher("/v1/ai/**")
        .csrf(AbstractHttpConfigurer::disable)
        .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
        .authorizeHttpRequests(r -> r.anyRequest().hasAuthority(AI_SERVER_AUTHORITY))
        .addFilterBefore(InternalTokenAuthFilter.of(properties), AuthorizationFilter.class)
        .exceptionHandling(...)   // 기존 EntryPoint 재사용 → 401 형식 동일
        .build();
}
```

- `InternalTokenAuthFilter`: `X-Internal-Token`을 설정값과 **상수 시간 비교**(`MessageDigest.isEqual`)한다. 맞으면 `AI_SERVER` 권한으로 인증한다
- `AI_SERVER`는 `Role` enum에 넣지 않고 `gateway:auth` 안의 상수로 둔다. `core:common`과 JWT 구조를 건드리지 않기 위해서다
- 설정값이 비어 있으면 **모든 요청을 401**로 막는다(fail closed). 설정 누락으로 열리는 일이 없다
- CORS는 설정하지 않는다. 브라우저가 부르는 API가 아니다
- 토큰 값은 로그에 남기지 않는다

### 3. 토큰: 기존 `STREAM_AI_INTERNAL_TOKEN` 재사용 (제안)

```yaml
# application-gateway-auth.yml
ai-server:
  internal-token: ${STREAM_AI_INTERNAL_TOKEN:}
```

- 지금 백엔드 → AI 서버 방향에 쓰는 값과 같은 값을 반대 방향에도 쓴다. AI 서버의 `INTERNAL_TOKEN`도 이미 같은 값이다
- 좋은 점: 새 비밀값·senv 항목이 없다. 배포 실수가 줄어든다
- 나쁜 점: 하나가 새면 양방향이 다 열린다. 따로 두려면 `AI_INBOUND_TOKEN`을 새로 만든다 → **결정 필요 ①**

### 4. 엔드포인트

공통: `ApiResponse` 래퍼, 날짜는 ISO8601(KST). 양이 적으므로(각 수십~수백 건) 페이지 없이 **전체 목록을 본문까지** 한 번에 준다.
`sync.py`가 1회 수집에 4번만 부르면 된다. 500건을 넘으면 그때 페이지를 붙인다.

| 경로 | 재사용할 서비스 | 포함 조건 | 응답 필드 |
| --- | --- | --- | --- |
| `GET /v1/ai/notices` | `NoticeService` | 삭제되지 않은 공지 | `noticeId`, `category`, `title`, `content`, `createdAt`, `updatedAt` |
| `GET /v1/ai/events` | `EventService.getPublishedEvents` + 상세 | 게시된 행사 | `eventId`, `title`, `description`, `place`, `applyStartAt`, `applyEndAt`, `eventStartAt`, `eventEndAt`, `updatedAt` |
| `GET /v1/ai/archives` | `ArchiveService` (목록 + 상세) | 삭제되지 않은 아카이빙 (학생 목록과 같음. `is_exposed`는 홈 노출용이라 거르지 않는다) | `archiveId`, `title`, `content`, `location`, `departmentName`, `startDate`, `endDate` |
| `GET /v1/ai/feedbacks` | `FeedbackService.search` | 답변이 공개된 회차의 답변된 질문 | `feedbackId`, `question`, `answer`, `answeredAt` |

**내보내지 않는 것** (개인정보·불필요):

- `createdBy`, `answeredBy` — 누가 썼는지. 특히 열린피드백 질문자는 절대 내보내지 않는다
- 이미지·첨부파일 ID — 챗봇은 본문 글만 쓴다
- 행사 신청자 수, 신청서 질문 — 실시간 값은 질문할 때 학생 JWT로 조회한다

예시:

```json
GET /v1/ai/notices
{
  "success": true,
  "code": "SUCCESS",
  "data": [
    {
      "noticeId": 11,
      "category": "GENERAL",
      "title": "2026-2학기 소프트웨어융합대학 학생회비 납부 안내",
      "content": "안녕하세요, 소프트웨어융합대학 학생회입니다. ...",
      "createdAt": "2026-10-16T10:00:00",
      "updatedAt": "2026-10-16T10:00:00"
    }
  ]
}
```

### 5. 삭제 반영

`sync.py`는 매번 **전체를 다시 받아 인덱스를 통째로 재구축**한다(stream-ai `AGENTS.md` 인덱스 갱신). 응답에 없는 문서는 인덱스에서 자연히 빠지므로 삭제 목록 API는 필요 없다.

### 6. 노출 범위

- 백엔드 서버는 인터넷에 열려 있으므로 `/v1/ai/**`도 밖에서 **주소는 닿는다.** 막는 것은 내부 토큰 하나다
- 내보내는 데이터가 **학생이 로그인하면 볼 수 있는 공개 글**뿐이라, 새도 피해는 "로그인 없이 공지를 읽는다" 수준이다
- 더 막고 싶으면 프록시에서 `/v1/ai/**`를 내부망 IP로만 허용한다 → **결정 필요 ③**

## 결정 필요

| # | 내용 | 제안 |
| --- | --- | --- |
| ① | 토큰을 기존 `STREAM_AI_INTERNAL_TOKEN`과 같이 쓸지, 새로 둘지 | 같이 쓴다 (운영 단순) |
| ② | 새 모듈 `api/ai-api` vs `app-api` 안에 패키지로 | 새 모듈 (클라이언트 기준 컨벤션) |
| ③ | `/v1/ai/**`를 프록시에서 내부망으로 제한할지 | 발표 전엔 토큰만, 이후 검토 |
| ④ | 행사 응답에 신청 기간·장소를 넣을지 (실시간성) | 넣는다. 날짜는 거의 안 바뀌고, 모집 현황(인원)만 실시간 조회 |

## 작업 분담·일정 (슬랑제 11/21 기준)

| 날짜 | 할 일 | 담당 |
| --- | --- | --- |
| 11/9~10 | 이 문서 합의 | 서준·승빈·이상현 |
| 11/10~11 | `ai-api` 모듈 + 보안 체인 + 엔드포인트 4개 PR | 이상현 (리뷰: 서준) |
| 11/11~13 | `sync.py`를 이 API에 연결, 인덱스 갱신 주기 설정 | 승빈 |
| 11/10~11 | "내 대여" 조회 도구 (학생 JWT, 백엔드 변경 없음) | 이상현 |
| 11/14~15 | 골든셋 근거 문서 재매핑 + 점수 측정 | 공동 |
| 11/16 | 발표 시나리오 확정 | 공동 |

**응답 모양(4절)을 먼저 확정하면** 승빈이 백엔드 PR을 기다리지 않고 가짜 응답으로 `sync.py`를 먼저 만들 수 있다.

## 배포 시 확인

- 백엔드: `STREAM_AI_INTERNAL_TOKEN`이 이미 있으므로(①을 "같이 쓴다"로 하면) 추가 환경변수 없음
- AI 서버: `sync.py`가 쓸 `BACKEND_BASE_URL`은 #70에서 추가한 값을 그대로 쓴다
- 확인: 토큰 없이 `GET /v1/ai/notices` → 401, 토큰 넣고 → 200
