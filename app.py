import json
import logging
import os
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger(__name__)

# 모델명이 없으면 서버를 아예 띄우지 않는다.
# 그냥 두면 서버는 정상으로 뜨고 요청이 들어올 때마다 실패해서 원인 찾기가 어렵다.
MODEL = os.getenv("GEMINI_MODEL")
if not MODEL:
    raise RuntimeError(
        "환경변수 GEMINI_MODEL이 없습니다. .env.example을 복사해 .env를 만드세요."
    )

app = FastAPI()
client = genai.Client()


class Msg(BaseModel):
    # Literal = 정해진 값만 허용. 다른 문자열이 오면 FastAPI가 422로 거절한다.
    role: Literal["user", "model"]
    text: str


class ChatReq(BaseModel):
    messages: list[Msg]


@app.get("/health")
def health():
    return {"status": "ok"}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def relay(req: Request, messages: list[Msg]):
    contents = [
        types.Content(role=m.role, parts=[types.Part(text=m.text)])
        for m in messages
    ]
    try:
        stream = await client.aio.models.generate_content_stream(
            model=MODEL,
            contents=contents,
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
        logger.exception("Gemini 스트리밍 실패")
        yield sse("error", {"message": "답변 생성 중 오류가 발생했습니다."})


@app.post("/chat")
async def chat(req: Request, body: ChatReq):
    return StreamingResponse(
        relay(req, body.messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
