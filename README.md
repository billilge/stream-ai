# stream-ai

국민대학교 소프트웨어융합대학 통합 플랫폼 **Stream**의 학생회 챗봇 서버입니다.
학생회 공지, 행사, 물품 대여, 사물함에 관한 질문에 답합니다.

백엔드([billilge/stream-server](https://github.com/billilge/stream-server))를 거친 요청만 받는 내부 서버이며, 현재 개발 중입니다.

## 구조

```
프론트 (React)
  → 백엔드 (Spring Boot)        JWT 인증, 대화 이력 관리
  → AI 서버 (이 저장소, FastAPI)  내부 토큰 검증, 답변을 SSE로 전송
      ├─ search.py   문서 검색 (BM25)
      ├─ tools.py    실시간 조회 (Gemini function calling)
      └─ Gemini      검색·조회 결과를 근거로 답변 생성
```

AI 서버는 대화 상태를 저장하지 않습니다. 대화 이력은 백엔드가 요청마다 보내 줍니다.

## 질문을 나눠서 처리합니다

같은 서비스에 대한 질문이라도 답을 얻는 방법이 다릅니다.

| 질문 예시 | 종류 | 처리 |
| --- | --- | --- |
| 우산 어떻게 빌려요? | 문서형 | 공지·안내 문서를 검색해서 답변 |
| 우산 지금 남았어요? | 실시간 | 재고를 그때그때 조회 |
| 내 사물함 번호 뭐야? | 개인별 | 인증된 사용자 기준으로만 조회 (예정) |
| 수강신청 언제야? | 학교 공식 | 직접 답하지 않고 원문 링크 안내 |

재고처럼 계속 바뀌는 정보는 검색 인덱스에 넣지 않습니다. 넣으면 지난 값으로 답하게 됩니다.
학사 일정처럼 틀리면 피해가 큰 정보는 날짜나 금액을 만들어 내지 않고 원문으로 안내합니다.

## 평가

답변 품질은 `eval/golden.jsonl`의 질문 78건으로 잽니다.
학생이 실제로 쓰는 표현으로 적었고, 답이 없는 함정 질문 20건이 들어 있습니다.

| 지표 | 보는 것 | 결과 |
| --- | --- | --- |
| Recall@5 | 근거 문서를 찾아오는가 | 100% (문서형 32건) |
| 함정 질문 거절률 | 모르는 걸 지어내지 않는가 | 85% (20건) |
| 정답요지 충족률 | 답변에 필요한 내용이 다 있는가 | 78% (문서형 46요지) |

거절률과 충족률은 현재 모델(Gemini)을 3번 돌린 평균이고, 함정 질문은 LLM 채점기로 판정했습니다. (2026-11 기준)

처음에는 문자열 비교로 채점했는데, `18:00`과 `18시`처럼 표현만 다른 정답을 틀렸다고 셌습니다.
같은 답변을 LLM 채점기로 다시 채점하자 충족률이 37%에서 63%로 바로잡혔습니다.
자세한 내용은 [채점 기준 점검](eval/grading-audit.md)에 있습니다.

## 실행

Python 3.10 이상, Google Cloud 인증(ADC)이 필요합니다.

```bash
cp .env.example .env              # 값 채우기
gcloud auth application-default login
pip install -r requirements.txt
python build_index.py             # 검색 인덱스 생성
uvicorn app:app --reload
```

`ENV=dev`이면 http://localhost:8000 에서 데모 페이지로 대화해 볼 수 있습니다.

평가는 `python eval.py`로 돌립니다. 도커 실행과 배포는 [docker.md](docs/guides/docker.md), [deploy.md](docs/guides/deploy.md)를 참고하세요.

## API

```
GET  /health    → { "status": "ok" }
POST /chat      헤더: X-Internal-Token, X-User-Id
                본문: { "messages": [ { "role": "user" | "model", "text": "..." } ] }
                응답: SSE (delta, bubble, done, error)
```

`bubble` 이벤트는 문단이 바뀌는 지점을 알려 줍니다. 프론트는 이 이벤트마다 말풍선을 새로 그립니다.

## 현재 상태

- 공지·행사 검색은 직접 작성한 시드 문서와 테스트 데이터로 동작합니다.
- 재고 조회는 실제 API와 같은 모양의 가짜 응답을 씁니다. 백엔드 내부 API가 나오면 연결합니다.
- 개인별 조회는 아직 없습니다.

## 팀

| 이름 | 담당 |
| --- | --- |
| 이상현 ([@overhyeon](https://github.com/overhyeon)) | 서버, LLM 연동, 도구, 프롬프트, 평가, 배포 |
| 이승빈 ([@seungbye](https://github.com/seungbye)) | 청킹, 검색 인덱스, 검색, 시드 문서, 가짜 백엔드 API |

개발 규칙과 설계 배경은 [AGENTS.md](AGENTS.md)에 정리되어 있습니다.
