"""백엔드 내부 API에서 공지·행사·아카이빙·열린피드백을 받아 인덱싱할 문서 모양으로 바꾼다.

이 모듈은 받아서 바꾸는 일만 한다. 인덱스를 만들고 바꿔 끼우는 일은 `refresh.py`가 한다.

부르는 곳은 백엔드의 AI 서버 전용 조회 API `/v1/ai/**`다(stream-server #312).
학생 JWT가 아니라 내부 토큰(`X-Internal-Token`)으로 부른다. 이 작업은 학생 질문 없이 혼자 돌기 때문이다.
백엔드가 학생 화면과 같은 규칙으로 걸러서 주므로(삭제·미게시 제외), 여기서 다시 거르지 않는다.

돌려주는 모양은 `build_index.py`의 로더(`load_notices`, `load_events`)와 같다.
`{"text": 본문, "meta": {문서ID, 출처URL, 제목, 게시일, 유효기간, 문서유형}}`
문서ID 접두어(notice-, event-, archive-, feedback-)도 같게 맞췄다. 골든셋의 근거문서ID를 다시 맞출 때 이 이름을 쓴다.

담당: AGENTS.md상 승빈 담당 파일이다. 내부 API 연결은 이상현이 먼저 만들었다(2026-11-09 합의).
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# 백엔드 내부 주소(예: http://<백엔드 컨테이너 이름>:8080). tools.py의 재고 조회와 같은 값을 쓴다.
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
# 백엔드 → AI 서버 방향에 쓰는 값과 같다. 백엔드도 같은 값으로 /v1/ai/**를 확인한다.
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

# 네 번 부르는 동안 하나라도 이 시간을 넘기면 이번 동기화는 실패로 보고, 기존 인덱스를 그대로 쓴다.
TIMEOUT_SECONDS = 10


def is_configured() -> bool:
    """동기화에 필요한 값이 다 있는지. 없으면 동기화를 켜지 않는다(로컬 개발·평가)."""
    return bool(BACKEND_BASE_URL and INTERNAL_TOKEN)


def _date(value: str | None) -> str:
    """'2026-10-16T10:00:00'이든 '2026-10-16'이든 날짜 부분만 남긴다. 없으면 빈 문자열."""
    return (value or "")[:10]


def _datetime(value: str | None) -> str:
    """'2026-11-20T14:00:00' → '2026-11-20 14:00'. 사람이 읽는 본문에 넣을 때 쓴다."""
    return (value or "")[:16].replace("T", " ")


def _get(client: httpx.Client, path: str) -> list[dict]:
    """내부 API 하나를 불러 data 목록을 돌려준다. 실패하면 예외를 던진다.

    한 종류라도 실패하면 이번 동기화 전체를 실패로 본다. 공지만 빠진 인덱스로 바꿔 끼우면
    챗봇이 갑자기 공지를 하나도 모르게 되기 때문이다. 실패하면 `refresh.py`가 기존 인덱스를 그대로 둔다.
    """
    response = client.get(f"{BACKEND_BASE_URL}{path}")
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"내부 API 실패: {path} code={body.get('code')}")
    return body.get("data") or []


def _notice(n: dict) -> dict:
    return {"text": n["content"], "meta": {
        "문서ID": f"notice-{n['noticeId']}", "제목": n["title"],
        "게시일": _date(n.get("createdAt")), "유효기간": "",
        "출처URL": "", "문서유형": "공지"}}


def _event(e: dict) -> dict:
    """행사 소개. 날짜·장소도 본문에 넣는다 — "해커톤 언제야", "어디서 해" 같은 질문이 이 문서로 답된다.

    모집 인원·남은 자리처럼 계속 바뀌는 값은 백엔드가 주지 않는다(질문할 때 따로 조회한다).
    행사에는 작성일이 없어 게시일은 신청 시작일(없으면 행사 시작일)로 둔다. 기존 로더와 같다.
    """
    lines = [e["title"], e.get("description") or ""]
    if e.get("applyStartAt") or e.get("applyEndAt"):
        lines.append(f"신청 기간: {_datetime(e.get('applyStartAt'))} ~ {_datetime(e.get('applyEndAt'))}")
    if e.get("eventStartAt") or e.get("eventEndAt"):
        lines.append(f"행사 일시: {_datetime(e.get('eventStartAt'))} ~ {_datetime(e.get('eventEndAt'))}")
    if e.get("place"):
        lines.append(f"장소: {e['place']}")
    return {"text": "\n".join(lines), "meta": {
        "문서ID": f"event-{e['eventId']}", "제목": e["title"],
        "게시일": _date(e.get("applyStartAt") or e.get("eventStartAt")),
        "유효기간": _date(e.get("eventEndAt")),
        "출처URL": "", "문서유형": "행사"}}


def _archive(a: dict) -> dict:
    """지난 활동 기록. 아카이빙에는 작성일이 없어 게시일은 활동 시작일로 둔다."""
    lines = [a.get("content") or ""]
    if a.get("startDate"):
        lines.append(f"활동 기간: {a['startDate']} ~ {a.get('endDate') or ''}")
    if a.get("location"):
        lines.append(f"장소: {a['location']}")
    if a.get("departmentName"):
        lines.append(f"담당: {a['departmentName']}")
    return {"text": "\n".join(lines), "meta": {
        "문서ID": f"archive-{a['archiveId']}", "제목": a["title"],
        "게시일": _date(a.get("startDate")), "유효기간": _date(a.get("endDate")),
        "출처URL": "", "문서유형": "아카이빙"}}


def _feedback(f: dict) -> dict:
    """질문 문장을 본문에 함께 넣는다. 학생이 묻는 말로도 검색되게 하려는 것이다(기존 로더와 같다)."""
    return {"text": f"질문: {f['question']}\n답변: {f['answer']}", "meta": {
        "문서ID": f"feedback-{f['feedbackId']}", "제목": f["question"],
        "게시일": _date(f.get("answeredAt")), "유효기간": "",
        "출처URL": "", "문서유형": "열린피드백"}}


def fetch_documents() -> list[dict]:
    """공지·행사·아카이빙·열린피드백 전체를 받아 인덱싱 문서 목록으로 돌려준다.

    실패하면 예외를 던진다(이유는 `_get` 참고). 토큰은 로그에 남기지 않는다.
    """
    if not is_configured():
        raise RuntimeError("BACKEND_BASE_URL 또는 INTERNAL_TOKEN이 없어 동기화할 수 없습니다")

    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    with httpx.Client(headers=headers, timeout=TIMEOUT_SECONDS) as client:
        notices = _get(client, "/v1/ai/notices")
        events = _get(client, "/v1/ai/events")
        archives = _get(client, "/v1/ai/archives")
        feedbacks = _get(client, "/v1/ai/feedbacks")

    logger.info("동기화: 공지 %d, 행사 %d, 아카이빙 %d, 열린피드백 %d",
                len(notices), len(events), len(archives), len(feedbacks))
    return ([_notice(n) for n in notices] + [_event(e) for e in events]
            + [_archive(a) for a in archives] + [_feedback(f) for f in feedbacks])
