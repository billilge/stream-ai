import json
import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from search import search

load_dotenv()

logger = logging.getLogger(__name__)

# 모델명이 없으면 서버를 아예 띄우지 않는다.
# 그냥 두면 서버는 정상으로 뜨고 요청이 들어올 때마다 실패해서 원인 찾기가 어렵다.
MODEL = os.getenv("GEMINI_MODEL")
if not MODEL:
    raise RuntimeError(
        "환경변수 GEMINI_MODEL이 없습니다. .env.example을 복사해 .env를 만드세요."
    )

# 프롬프트는 코드에 문자열로 넣지 않고 파일에서 읽는다.
# 비개발자가 말투를 고칠 수 있어야 하기 때문이다. 수정하면 서버를 다시 띄워야 반영된다.
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system_v1.md").read_text(
    encoding="utf-8"
)

app = FastAPI()
client = genai.Client()


class Msg(BaseModel):
    # Literal = 정해진 값만 허용. 다른 문자열이 오면 FastAPI가 422로 거절한다.
    role: Literal["user", "model"]
    text: str


class ChatReq(BaseModel):
    # 최소 1개. 빈 배열이 오면 마지막 발화를 꺼낼 수 없어 터진다.
    messages: list[Msg] = Field(min_length=1)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def demo():
    """개발 중 동작 확인용 페이지.

    실제 사용자는 프론트(React)를 통해 들어오므로 이 페이지는 배포 전에 제거한다.
    파일로 열면 CORS에 막혀서, 서버가 직접 내려준다.
    """
    return FileResponse(Path(__file__).parent / "static" / "demo.html")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


async def relay(req: Request, messages: list[Msg]):
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

        contents = [
            types.Content(role=m.role, parts=[types.Part(text=m.text)])
            for m in messages
        ]
        config = types.GenerateContentConfig(
            system_instruction=f"{SYSTEM_PROMPT}\n\n# 참고 문서\n\n{build_context(docs)}"
        )

        stream = await client.aio.models.generate_content_stream(
            model=MODEL,
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            if await req.is_disconnected():
                break
            if chunk.text:
                yield sse("delta", {"text": chunk.text})
        yield sse("done", {})
    except Exception:
        # 상세 원인은 서버 로그에만 남긴다.
        # 예외 메시지를 그대로 내려보내면 내부 경로나 설정이 클라이언트에 노출된다.
        logger.exception("답변 생성 실패")
        yield sse("error", {"message": "답변 생성 중 오류가 발생했습니다."})


@app.post("/chat")
async def chat(req: Request, body: ChatReq):
    return StreamingResponse(
        relay(req, body.messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
