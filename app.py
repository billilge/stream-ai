import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import llm
from search import search
import tools
from tools import TOOLS

load_dotenv()

logger = logging.getLogger(__name__)

# 모델 설정(LLM_MODEL, KMU_AI_API_KEY)은 llm.py가 읽고, 없으면 import 시점에 서버를 멈춘다.

# 프롬프트는 코드에 문자열로 넣지 않고 파일에서 읽는다.
# 비개발자가 말투를 고칠 수 있어야 하기 때문이다. 수정하면 서버를 다시 띄워야 반영된다.
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system_v2.md").read_text(
    encoding="utf-8"
)

# 개발 환경에서만 데모 페이지를 연다. 배포할 때 ENV=prod 로 둔다.
ENV = os.getenv("ENV", "dev")

# 백엔드와 나눠 가진 비밀값. 백엔드는 이 값을 X-Internal-Token 헤더에 실어 보낸다.
# 이 값을 아는 쪽(백엔드)에서 온 요청만 처리한다. 학생 로그인 확인은 백엔드가 이미 끝냈다.
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")
if ENV == "prod" and not INTERNAL_TOKEN:
    # 배포에서 빠뜨리면 누구나 우리 LLM 비용으로 질문할 수 있다. 조용히 뜨지 않고 멈춘다.
    raise RuntimeError(
        "ENV=prod 인데 환경변수 INTERNAL_TOKEN이 없습니다. 백엔드와 같은 값을 넣으세요."
    )

app = FastAPI()


class Msg(BaseModel):
    # Literal = 정해진 값만 허용. 다른 문자열이 오면 FastAPI가 422로 거절한다.
    role: Literal["user", "model"]
    text: str


class ChatReq(BaseModel):
    # 최소 1개. 빈 배열이 오면 마지막 발화를 꺼낼 수 없어 터진다.
    messages: list[Msg] = Field(min_length=1)


def verify_backend(
    x_internal_token: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> int | None:
    """백엔드를 거친 요청인지 확인하고, 질문한 학생의 회원 ID를 돌려준다.

    FastAPI가 `/chat`을 실행하기 **전에** 이 함수를 먼저 부른다(Depends).
    그래서 여기서 거절하면 스트림이 열리기 전이라 401·400 상태 코드로 깔끔하게 끝난다.

    개발 환경에서 INTERNAL_TOKEN을 비워 두면 검사하지 않는다. 데모 페이지는 브라우저에서
    바로 부르므로 토큰을 실을 수 없다. 배포(prod)에서는 비워 둘 수 없다(위에서 막는다).
    """
    if not INTERNAL_TOKEN:
        return None

    # compare_digest: 앞에서부터 한 글자씩 비교하다 다르면 바로 멈추는 일반 비교(==)는
    # 응답 시간 차이로 토큰을 한 글자씩 알아낼 수 있다. 이 함수는 항상 같은 시간이 걸린다.
    # bytes로 바꾸는 이유는 헤더에 한글 같은 문자가 오면 str 비교가 예외를 내기 때문이다.
    if x_internal_token is None or not secrets.compare_digest(
        x_internal_token.encode(), INTERNAL_TOKEN.encode()
    ):
        # 왜 틀렸는지(없음/불일치)는 알려주지 않는다. 알려주면 공격하는 쪽만 돕는다.
        raise HTTPException(status_code=401, detail="인증되지 않은 요청입니다")

    # isascii를 같이 보는 이유: isdigit은 "²" 같은 문자도 숫자로 친다.
    if x_user_id is None or not (x_user_id.isascii() and x_user_id.isdigit()) or int(x_user_id) <= 0:
        raise HTTPException(status_code=400, detail="사용자 식별자가 올바르지 않습니다")
    return int(x_user_id)


@app.get("/health")
def health():
    # 배포 도구가 서버 상태를 확인하는 주소라 검증하지 않는다. 비밀 정보도 없다.
    return {"status": "ok"}


if ENV != "prod":
    # 실제 사용자는 프론트(React)를 통해 들어온다. 이 페이지는 개발 중 확인용이다.
    # 라우트 자체를 등록하지 않으므로 ENV=prod 면 존재하지 않는다.
    # 파일로 열면 CORS에 막혀서 서버가 직접 내려준다.
    @app.get("/")
    def demo():
        path = Path(__file__).parent / "static" / "demo.html"
        if not path.exists():
            # 응답이 시작된 뒤 터져서 깨진 500이 나가는 것보다 명확한 404가 낫다.
            raise HTTPException(status_code=404, detail="데모 페이지 파일이 없습니다")
        return FileResponse(path)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class BubbleSplitter:
    """스트리밍 텍스트를 문단(빈 줄) 단위 말풍선으로 나눈다.

    프론트가 빈 줄을 직접 찾게 하면 불안정하다. 스트림 조각이 "\n" 하나에서 끊기면
    빈 줄인지 아닌지 알 수 없기 때문이다. 그래서 서버가 조각을 이어 붙여 보면서
    문단이 바뀌는 지점에 bubble 이벤트를 따로 보낸다. 프론트는 이 이벤트만 보고 말풍선을 나눈다.
    """

    def __init__(self):
        self.buf = ""            # 아직 내보내지 않은 텍스트
        self.has_text = False    # 지금까지 한 글자라도 내보냈는지
        self.need_bubble = False # 다음 글자를 내보내기 전에 말풍선을 나눠야 하는지

    def _emit(self, text: str) -> list[str]:
        if self.need_bubble:
            # 새 문단의 앞쪽 줄바꿈은 버린다. 말풍선이 빈 줄로 시작하지 않게 한다.
            text = text.lstrip("\n")
        if not text:
            return []
        events = []
        if self.need_bubble and self.has_text:
            events.append(sse("bubble", {}))
        self.need_bubble = False
        self.has_text = True
        events.append(sse("delta", {"text": text}))
        return events

    def feed(self, text: str) -> list[str]:
        """모델이 보낸 조각을 받아 지금 내보낼 수 있는 SSE 이벤트를 돌려준다."""
        self.buf += text
        events = []
        while "\n\n" in self.buf:
            para, self.buf = self.buf.split("\n\n", 1)
            events += self._emit(para)
            if self.has_text:
                self.need_bubble = True
        # 끝의 줄바꿈은 다음 조각과 합쳐 빈 줄이 될 수 있어 붙잡아 둔다.
        keep = len(self.buf) - len(self.buf.rstrip("\n"))
        if keep < len(self.buf):
            events += self._emit(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep :]
        return events

    def flush(self) -> list[str]:
        """스트림이 끝났을 때 남은 텍스트를 내보낸다. 끝의 줄바꿈은 버린다."""
        rest, self.buf = self.buf.rstrip("\n"), ""
        return self._emit(rest)


def build_context(docs: list[dict]) -> str:
    """검색된 문서를 프롬프트에 넣을 형태로 바꾼다.

    문서가 없으면 그 사실을 명시한다. 빈 문자열을 넘기면 모델이
    참고 문서가 없다는 걸 모르고 아무 말이나 지어낼 수 있다.
    """
    if not docs:
        return "관련 문서를 찾지 못했습니다."

    blocks = []
    for d in docs:
        # 메타데이터 한 칸이 비었다고 답변 전체를 실패시키지 않는다.
        m = d.get("meta", {})
        blocks.append(
            f"[{m.get('문서ID', '?')}]\n"
            f"제목: {m.get('제목', '(제목 없음)')}\n"
            f"게시일: {m.get('게시일', '(알 수 없음)')}\n"
            f"유효기간: {m.get('유효기간') or '없음'}\n"
            f"링크: {m.get('출처URL', '')}\n"
            f"내용: {d.get('text', '')}"
        )
    return "\n\n".join(blocks)


def build_system_instruction(docs: list[dict], today: str | None = None) -> str:
    """모델에 넘길 시스템 지시문을 조립한다.

    `today`를 인자로 받는 이유는 평가 때문이다. 골든셋은 기준 시각을 고정해 두고
    "이번 달 행사" 같은 상대 날짜 질문을 재현하는데, 함수 안에서 현재 시각을 직접
    계산하면 밖에서 고정할 수가 없다. 그렇다고 `eval.py`가 프롬프트를 따로 조립하면
    실제 코드와 다른 것을 평가하게 된다.

    기본값은 한국 시간 기준 오늘이다. 서버가 해외 리전에 떠도 날짜가 하루 밀리지 않는다.
    """
    if today is None:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"오늘 날짜: {today}\n\n"
        f"# 참고 문서\n\n{build_context(docs)}"
    )


async def relay(req: Request, messages: list[Msg], user_token: str | None):
    # 학생 JWT를 이 요청 동안만 도구가 볼 수 있게 넣어 둔다(tools.user_token 설명 참고).
    # 여기(스트림을 실제로 만드는 곳)에서 넣어야 도구가 호출될 때 같은 값이 보인다.
    tools.user_token.set(user_token)

    # 검색부터 전부 try 안에서 한다.
    # StreamingResponse는 이 함수가 돌기 전에 이미 200과 헤더를 보냈기 때문에,
    # 여기서 밖으로 튀는 예외는 error 이벤트도 없이 연결만 끊는다. 클라이언트는 이유를 모른다.
    try:
        # 마지막 발화가 이번 질문이다. 검색은 이 문장으로 한다.
        question = messages[-1].text
        docs = search(question)
        # 질문 원문은 로그에 남기지 않는다. 학생의 개인 사정이 섞여 들어올 수 있다.
        logger.info("검색 %d건 %s", len(docs), [d.get("meta", {}).get("문서ID") for d in docs])
        logger.debug("질문: %s", question)

        # 모델은 오늘이 며칠인지 모른다. 알려주지 않으면 유효기간이 지났는지 짐작으로 답한다.
        # TOOLS: 모델이 필요할 때 부를 수 있는 조회 함수(tools.py). llm.py가 호출과 결과 전달을
        # 알아서 처리하고, 여기에는 최종 답변 글자만 나온다. 그래서 아래 말풍선 처리는 그대로다.
        stream = llm.stream_answer(
            system=build_system_instruction(docs),
            history=[m.model_dump() for m in messages],
            functions=TOOLS,
        )
        splitter = BubbleSplitter()
        async for text in stream:
            if await req.is_disconnected():
                # 학생이 창을 닫았다. 게이트웨이 연결도 바로 닫아 남은 답변 생성 비용을 아낀다
                await stream.aclose()
                break
            for event in splitter.feed(text):
                yield event
        for event in splitter.flush():
            yield event
        yield sse("done", {})
    except Exception:
        # 상세 원인은 서버 로그에만 남긴다.
        # 예외 메시지를 그대로 내려보내면 내부 경로나 설정이 클라이언트에 노출된다.
        logger.exception("답변 생성 실패")
        yield sse("error", {"message": "답변 생성 중 오류가 발생했습니다."})


@app.post("/chat")
async def chat(
    req: Request,
    body: ChatReq,
    user_id: int | None = Depends(verify_backend),
    x_user_token: str | None = Header(default=None),
):
    # user_id는 아직 쓰지 않는다. 개인 데이터 조회 도구(#52)가 이 값으로만 조회한다.
    # 모델이 정한 값을 쓰면 다른 학생 정보를 물어 꺼낼 수 있기 때문이다.
    # x_user_token: 백엔드가 넘겨준 학생 JWT. 도구가 백엔드 학생 API를 조회할 때 쓴다(#69).
    # 검사는 백엔드가 이미 끝냈다. 여기서는 그대로 전달만 하고, 로그에 남기지 않는다.
    return StreamingResponse(
        relay(req, body.messages, x_user_token),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
