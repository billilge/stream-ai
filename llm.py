"""LLM 호출 래퍼. 답변 모델을 바꿀 때 이 파일만 고친다.

지금은 학교 AI 게이트웨이(ai.cs.kookmin.ac.kr)의 gpt-oss-120b를 쓴다(#61에서 선정, #65에서 교체).
게이트웨이는 OpenAI와 같은 요청 형식(`/v1/chat/completions`)을 받으므로, 모델 이름만 바꾸면
게이트웨이의 다른 모델로도 그대로 돌아간다.

새 라이브러리를 들이지 않고 이미 설치된 httpx로 직접 부른다. 요청 모양이 단순해서
SDK를 쓰는 것보다 무엇을 보내고 받는지 코드에 그대로 보이는 쪽이 읽기 쉽다.

용어
- 스트리밍: 답을 다 만든 뒤 한 번에 받지 않고 만들어지는 대로 조각(chunk)으로 받는다
- 도구 호출(function calling): 모델이 "재고 조회 함수를 이 값으로 불러 달라"고 요청하면
  우리가 함수를 실행해 결과를 돌려주고, 모델은 그 결과를 보고 답을 이어 쓴다
"""

import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Callable

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LLM_BASE_URL", "https://ai.cs.kookmin.ac.kr/v1")
MODEL = os.getenv("LLM_MODEL")
API_KEY = os.getenv("KMU_AI_API_KEY")

# 값이 없으면 서버를 아예 띄우지 않는다.
# 그냥 두면 서버는 정상으로 뜨고 질문이 들어올 때마다 실패해서 원인 찾기가 어렵다.
if not MODEL or not API_KEY:
    raise RuntimeError(
        "환경변수 LLM_MODEL 또는 KMU_AI_API_KEY가 없습니다. .env.example을 복사해 .env를 만드세요."
    )

# 도구를 부르고 결과를 돌려주는 왕복의 최대 횟수.
# 모델이 같은 도구를 계속 부르며 답을 끝내지 않는 경우를 막는다.
MAX_TOOL_ROUNDS = 3

# 생각(reasoning)을 하는 모델이라 첫 글자까지 몇 초 걸린다. 넉넉히 기다린다.
TIMEOUT_SECONDS = 120

# 대화 이력의 역할 이름. 백엔드 계약은 Gemini 시절 이름(model)을 쓰고 OpenAI 형식은 assistant를 쓴다.
# 계약을 바꾸면 백엔드(DB의 sender_type)까지 고쳐야 하므로 여기서만 바꿔 보낸다.
ROLE_MAP = {"user": "user", "model": "assistant"}


def tool_specs(functions: list[Callable]) -> list[dict]:
    """`tools.TOOLS`의 파이썬 함수를 OpenAI 형식 도구 설명으로 바꾼다.

    손으로 JSON을 따로 적으면 tools.py를 고칠 때 한쪽만 바뀌므로 함수의 설명(docstring)과
    인자에서 만든다. 지금 도구의 인자는 모두 문자열이라 타입은 string으로 둔다.
    """
    specs = []
    for fn in functions:
        params = inspect.signature(fn).parameters
        specs.append({
            "type": "function",
            "function": {
                "name": fn.__name__,
                "description": inspect.getdoc(fn) or "",
                "parameters": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in params},
                    # 기본값이 없는 인자만 필수로 둔다
                    "required": [n for n, p in params.items() if p.default is inspect.Parameter.empty],
                },
            },
        })
    return specs


def _run_tool(functions: dict[str, Callable], name: str, arguments: str) -> str:
    """모델이 요청한 도구를 실행하고 결과를 JSON 글자로 돌려준다.

    모델이 없는 도구 이름이나 깨진 인자를 보낼 수 있다. 예외로 답변 전체를 끊지 않고
    오류를 결과로 돌려줘서, 모델이 "확인할 수 없다"고 답하게 한다.
    """
    fn = functions.get(name)
    if fn is None:
        return json.dumps({"오류": f"없는 도구입니다: {name}"}, ensure_ascii=False)
    try:
        result = fn(**json.loads(arguments or "{}"))
    except Exception:
        logger.exception("도구 실행 실패: %s", name)
        result = {"조회성공": False, "안내": "지금 확인할 수 없어요."}
    return json.dumps(result, ensure_ascii=False)


async def _stream_once(client: httpx.AsyncClient, payload: dict) -> AsyncIterator[tuple[str, object]]:
    """한 번 부르고 받은 조각을 ("text", 글자) 또는 ("tool_calls", 목록)으로 흘려보낸다.

    스트리밍에서 도구 호출 요청은 이름과 인자가 여러 조각으로 쪼개져 온다.
    번호(index)별로 이어 붙였다가 스트림이 끝나면 한 번에 넘긴다.
    """
    calls: dict[int, dict] = {}
    async with client.stream(
        "POST", f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=payload,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            # SSE 형식이라 "data: {...}" 줄만 본다. 빈 줄·주석 줄은 건너뛴다
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if not chunk.get("choices"):
                continue
            delta = chunk["choices"][0].get("delta") or {}
            if delta.get("content"):
                yield "text", delta["content"]
            for tc in delta.get("tool_calls") or []:
                call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                call["id"] = tc.get("id") or call["id"]
                fn = tc.get("function") or {}
                call["name"] = fn.get("name") or call["name"]
                call["arguments"] += fn.get("arguments") or ""
    if calls:
        yield "tool_calls", [calls[i] for i in sorted(calls)]


async def stream_answer(
    system: str,
    history: list[dict],
    functions: list[Callable],
) -> AsyncIterator[str]:
    """답변을 글자 조각으로 흘려보낸다.

    - system: 시스템 지시문 (프롬프트 + 오늘 날짜 + 참고 문서)
    - history: [{"role": "user" | "model", "text": ...}, ...] — 백엔드 계약 그대로
    - functions: 모델이 부를 수 있는 함수 목록 (tools.TOOLS)

    모델이 도구를 부르면 실행해 결과를 붙이고 다시 부른다. 그래서 받는 쪽(app.py)에는
    최종 답변 글자만 나온다. 도구 호출이 있었는지는 신경 쓰지 않아도 된다.
    """
    messages = [{"role": "system", "content": system}]
    messages += [{"role": ROLE_MAP[m["role"]], "content": m["text"]} for m in history]
    by_name = {fn.__name__: fn for fn in functions}
    specs = tool_specs(functions)

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            text = ""
            calls = []
            async for kind, value in _stream_once(client, {
                "model": MODEL, "messages": messages, "tools": specs, "stream": True,
            }):
                if kind == "text":
                    text += value
                    yield value
                else:
                    calls = value
            if not calls:
                return

            # 모델이 도구를 불렀다. 요청 내용을 대화에 남기고, 실행 결과를 붙여 다시 묻는다
            logger.info("도구 호출 %s", [c["name"] for c in calls])
            messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in calls
                ],
            })
            for c in calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": _run_tool(by_name, c["name"], c["arguments"]),
                })

    # 왕복 횟수를 다 썼는데도 도구만 부르고 있다. app.py가 error 이벤트로 바꿔 보낸다
    raise RuntimeError(f"도구 호출이 {MAX_TOOL_ROUNDS}번을 넘어 답변을 끝내지 못했습니다")
