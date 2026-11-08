"""골든셋으로 챗봇을 채점한다.

`eval/golden.jsonl`을 읽어 세 가지를 잰다.

- **Recall@5** — 검색 상위 k건에 근거문서가 들어있나. 검색만 보므로 LLM을 부르지 않는다
- **함정 정답률** — 답이 없는 질문에 "모르겠다"고 했나. 환각을 재는 유일한 지표다
- **답변 채점** — `정답요지`가 답에 들어있고 `금지`가 안 들어있나

정답요지는 기본으로 **LLM 채점기**가 판정한다(`prompts/judge_v1.md`). 예전 낱말 비교 채점은
뜻이 같아도 표현이 다르면 놓쳐서, 기준 판정과 비교해 실제 충족의 61%만 잡았다
(`eval/grading-audit.md`). 채점기를 바꾸면 `--calibrate`로 기준 판정과 먼저 맞춰 본다.

세 지표는 성격이 다르다. Recall@5는 `search.py`를, 함정 정답률과 답변 채점은
프롬프트를 본다. **프롬프트를 고쳤는데 Recall@5가 그대로인 것은 정상이다.**
v1/v2를 비교할 때는 뒤의 둘로 판단한다.

실행:
    python eval.py                  전체 (LLM 호출 있음, 느리다)
    python eval.py --no-llm         검색 지표만 (빠르다)
    python eval.py --prompt v1      프롬프트 버전을 바꿔 비교
    python eval.py --out eval/run-v2.json   점수표를 파일로 남긴다
    python eval.py --answers eval/run-v2.json   저장된 답변을 다시 채점만 한다 (답변 생성 없음)
    python eval.py --grader rule    예전 낱말 비교 채점 (지난 기록과 비교할 때만)
    python eval.py --calibrate      채점기를 기준 판정(eval/judge-labels.jsonl)과 비교한다
    python eval.py --kmu-model claude-haiku-5-5 --out eval/kmu/claude-haiku-5-5.json
                                    학교 AI 게이트웨이 모델로 답변을 만든다 (채점은 Gemini)
    python eval.py --compare eval/kmu/*.json    모델별 점수표를 비교표로 출력한다
"""

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()

GOLDEN_PATH = "eval/golden.jsonl"
DEFAULT_K = 5

# 거절로 인정하는 표현. 프롬프트 v2가 실제로 내보내는 문구를 기준으로 골랐다.
# 넓게 잡으면 지어낸 답도 거절로 세어 함정 정답률이 부풀고, 좁게 잡으면 제대로
# 거절했는데 틀렸다고 센다. 새 문구가 보이면 여기에 추가하고 이유를 적는다.
REFUSAL = (
    "찾지 못했",
    "확인하지 못했",
    "안내해 드릴 수 없",
    "답변할 수 없",
    "답변드릴 수 없",
    "답할 수 있는 범위",
    "답변 가능한 범위",
    # v1·v2 모두 "학생회와 Stream에 관한 것만 답할 수 있어요"로 거절한다.
    # 이 문구를 빠뜨려 정확히 거절한 답변을 실패로 셌던 적이 있다.
    "만 답할 수 있",
    "관한 것만",
    "학생회가 관리하는 자료가 아니",
    "범위를 벗어",
    "알 수 없",
)


def load_golden(path: str = GOLDEN_PATH) -> tuple[dict, list[dict]]:
    """골든셋을 읽는다. 첫 줄은 메타, 나머지는 한 줄에 한 문항."""
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0]).get("_meta", {})
    rows = [json.loads(line) for line in lines[1:] if line.strip()]
    return meta, [r for r in rows if not r.get("제외")]


def recall_at_k(rows: list[dict], k: int, search_fn) -> dict:
    """검색 상위 k건에 근거문서가 들어있는지 센다.

    근거문서를 여러 개 적어둔 문항은 **하나라도** 들어오면 적중으로 본다.
    "사물함 신청 기간"처럼 공지와 시드 문서 어느 쪽으로 답해도 되는 질문이 있다.

    대상은 **문서형뿐이다.** 학교공식(`LNK-*`)은 링크를 골라 안내하는 일이라
    BM25 인덱스에 들어가지 않는다. 섞어서 세면 검색이 못 푸는 게 당연한 문항까지
    실패로 잡혀 Recall@5가 실제보다 낮게 나온다. 링크 안내는 5주차 과제이고
    그때 별도 지표로 잰다. 실시간·개인별도 같은 이유로 뺀다.
    """
    targets = [r for r in rows
               if r["근거문서ID"] and "문서형" in r.get("출처구분", "")]
    hits, misses = 0, []
    for r in targets:
        got = {d["meta"]["문서ID"] for d in search_fn(r["질문"], k=k)}
        want = set(r["근거문서ID"])
        if got & want:
            hits += 1
        else:
            misses.append({"id": r["id"], "질문": r["질문"],
                           "기대": sorted(want), "검색": sorted(got)})
    return {
        "대상": len(targets),
        "적중": hits,
        "점수": round(hits / len(targets), 4) if targets else 0.0,
        "실패": misses,
    }


def _refused(answer: str) -> bool:
    return any(k in answer for k in REFUSAL)


# 낱말 단위로 쪼갤 때 무시할 조각. 어디에나 나와서 충족 판정을 부풀린다.
_TRIVIAL = {"및", "또는", "그리고", "등", "개", "건", "시", "분", "일", "월",
            "까지", "부터", "에서", "제공", "안내", "확인", "가능"}
_TOKEN = re.compile(r"[가-힣A-Za-z]{2,}|\d+")
_MATCH_RATIO = 0.7


def _tokens(text: str) -> list[str]:
    """비교용 낱말로 쪼갠다.

    정답요지는 사람이 쓴 메모라 답변 문장과 글자가 그대로 겹치지 않는다.
    `"9/21 23:59"`와 `"9월 21일(월) 23:59"`, `"평일 10~17시"`와
    `"평일 10:00~17:00"`이 같은 뜻인데 부분 문자열로는 하나도 안 맞는다.
    그래서 숫자와 낱말만 뽑아 비교한다.
    """
    return [t for t in _TOKEN.findall(text) if t not in _TRIVIAL]


def _covered(need: str, answer: str) -> bool:
    """정답요지 한 항목이 답변에 담겼는지 본다.

    낱말의 70% 이상이 답변에 나오면 담긴 것으로 센다. 전부를 요구하면 표현이
    조금만 달라도 떨어지고, 절반으로 낮추면 우연히 겹친 것까지 맞다고 센다.

    낱말이 같은지는 **포함**으로 본다. 한국어는 조사가 붙어서 낱말이 그대로
    떨어지지 않는다. `로비`와 `로비예요`, `Stream`과 `Stream의`가 같은 말인데
    토큰을 정확히 비교하면 어긋난다.

    **이 채점은 근사값이다.** 절대 점수를 품질이라고 읽으면 안 된다. 쓰임새는
    같은 잣대로 잰 v1과 v2의 **차이**를 보는 것이다.
    """
    toks = _tokens(need)
    if not toks:
        return False

    # 숫자는 하나라도 틀리면 틀린 답이다. 전부 70%로 재면 "9/21 23:59"를 요구하는
    # 문항에서 "9/21 23:50"이 넷 중 셋을 맞혀 통과한다. 마감 시각이 틀린 답을
    # 정답으로 세면 그 지표는 쓸 수 없다. 낱말만 비율로 보고 숫자는 전부 요구한다.
    nums = [t for t in toks if t.isdigit()]
    words = [t for t in toks if not t.isdigit()]

    if any(n not in answer for n in nums):
        return False
    if not words:
        return True
    return sum(1 for w in words if w in answer) / len(words) >= _MATCH_RATIO


def grade_answer(row: dict, answer: str, met: list[bool] | None = None) -> dict:
    """답변 하나를 채점한다.

    함정은 거절했는지만 본다. 그 외에는 `정답요지`가 담겼는지와 `금지`를
    어겼는지를 따로 센다.

    `met`은 LLM 채점기가 정답요지마다 낸 판정이다. 없으면 낱말 비교(`_covered`)로 센다.

    금지는 정답요지보다 엄하게 본다. "조형대학"처럼 **틀린 답을 실제로 말했는지**를
    보는 것이라 느슨하게 잡으면 사고를 놓친다. 그래서 부분 문자열 그대로 쓴다.

    그 대가로 **글자로 찾을 수 없는 금지는 여기서 잴 수 없다.** "추측으로 답하기"처럼
    행동을 설명한 것, 살아 있는 문서에도 있어 맞는 답에 섞일 수 있는 문구가 그렇다.
    그런 항목은 `금지판정` 칸에 두고 사람이 본다. 자동 채점에 섞으면 둘 중 하나가 된다 —
    영원히 안 걸리거나(0건을 0건으로 보고), 멀쩡한 답을 위반으로 잡거나.
    """
    checks = row.get("금지판정") or []
    if row.get("함정"):
        return {"함정": True, "거절": _refused(answer), "판정": len(checks)}

    need = row.get("정답요지") or []
    if met is None:
        met = [_covered(n, answer) for n in need]
    flat = answer.replace(" ", "")
    banned = [b for b in (row.get("금지") or []) if b.replace(" ", "") in flat]
    return {
        "함정": False,
        "정답요지": len(need),
        "충족": sum(met),
        "위반": banned,
        "판정": len(checks),
        "거절": _refused(answer),
    }


# ─────────────────────────────────────────────────────────────
# LLM 채점기 — 정답요지가 답변에 담겼는지 모델에게 판정시킨다
# ─────────────────────────────────────────────────────────────
# 낱말 비교는 "따로 설치할 앱은 없어요"와 "별도 설치 없음"이 같은 말인지 모른다.
# 그래서 뜻을 이해하는 모델에게 맡긴다. 대신 모델 판정도 틀릴 수 있으므로,
# 미리 판정해 둔 기준표(eval/judge-labels.jsonl)와 맞춰 보고(--calibrate) 쓴다.

JUDGE_PROMPT_PATH = "prompts/judge_v1.md"
LABELS_PATH = "eval/judge-labels.jsonl"


async def judge_rows(items: list[dict], concurrency: int = 3) -> dict[int, list[bool] | None]:
    """문항마다 정답요지 판정 목록을 돌려준다. 판정에 실패한 문항은 None이다.

    `items`의 각 항목은 `id`, `질문`, `정답요지`, `답변`을 갖는다.

    temperature 0으로 부른다. 같은 답변을 두 번 채점해서 결과가 다르면 점수 차이가
    프롬프트 때문인지 채점기 기분 때문인지 구분할 수 없다.
    응답은 JSON 모양을 정해 받는다(response_schema). 자유 문장으로 받으면 파싱이 깨진다.
    """
    from google import genai
    from google.genai import types
    from pydantic import BaseModel

    class Point(BaseModel):
        reason: str
        met: bool

    class Verdict(BaseModel):
        points: list[Point]

    client = genai.Client()
    model = os.getenv("JUDGE_MODEL") or os.getenv("GEMINI_MODEL")
    system = pathlib.Path(JUDGE_PROMPT_PATH).read_text(encoding="utf-8")
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0,
        response_mime_type="application/json",
        response_schema=Verdict,
    )
    sem = asyncio.Semaphore(concurrency)
    out: dict[int, list[bool] | None] = {}

    async def one(item):
        need = item["정답요지"]
        listed = "\n".join(f"{i}. {n}" for i, n in enumerate(need, 1))
        text = f"# 질문\n{item['질문']}\n\n# 정답요지\n{listed}\n\n# 답변\n{item['답변']}"
        async with sem:
            try:
                # 응답 없이 멈추는 호출이 실제로 있었다. 하나 때문에 전체가 끝나지 않으면 안 되므로 끊는다.
                resp = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=text, config=cfg),
                    timeout=60,
                )
                verdict = resp.parsed
            except Exception as exc:
                print(f"  채점 실패 #{item['id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
                out[item["id"]] = None
                return
        # 개수가 다르면 어느 판정이 어느 요지 것인지 알 수 없다. 추측해서 맞추지 않고 실패로 둔다.
        if verdict is None or len(verdict.points) != len(need):
            print(f"  채점 실패 #{item['id']}: 판정 개수가 요지 개수와 다름", file=sys.stderr)
            out[item["id"]] = None
            return
        out[item["id"]] = [p.met for p in verdict.points]

    await asyncio.gather(*(one(it) for it in items))
    return out


def judge_answers(rows: list[dict], answers: dict) -> dict[int, list[bool] | None]:
    """골든셋 답변 중 정답요지가 있는 문항만 골라 채점기에 넘긴다."""
    items = [
        {"id": r["id"], "질문": r["질문"], "정답요지": r["정답요지"], "답변": answers[r["id"]]}
        for r in rows
        if not r.get("함정") and r.get("정답요지")
        and r["id"] in answers and not answers[r["id"]].startswith("__ERROR__")
    ]
    print(f"LLM 채점 {len(items)}건...")
    return asyncio.run(judge_rows(items))


def calibrate(path: str = LABELS_PATH) -> None:
    """채점기 판정을 기준 판정과 비교한다. 채점기나 판정 프롬프트를 고친 뒤 먼저 돌린다.

    두 번 돌려 같은지도 본다. 일치율이 높아도 돌릴 때마다 판정이 바뀌면 점수 차이를 믿을 수 없다.
    """
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines[1:] if line.strip()]
    first = asyncio.run(judge_rows(items))
    second = asyncio.run(judge_rows(items))

    total = llm_ok = rule_ok = stable = failed = 0
    diffs = []
    for it in items:
        a, b = first.get(it["id"]), second.get(it["id"])
        if a is None or b is None:
            failed += len(it["정답요지"])
            continue
        for need, human, x, y in zip(it["정답요지"], it["기준판정"], a, b):
            total += 1
            llm_ok += x == human
            rule_ok += _covered(need, it["답변"]) == human
            stable += x == y
            if x != human:
                diffs.append((it["id"], need, human, x))

    print(f"\n기준 판정 {total}요지 (채점 실패 {failed}요지 제외)")
    print(f"  LLM 채점기 일치  {llm_ok}/{total} = {llm_ok / total:.1%}")
    print(f"  낱말 비교 일치   {rule_ok}/{total} = {rule_ok / total:.1%}")
    print(f"  두 번 돌린 판정이 같음  {stable}/{total} = {stable / total:.1%}")
    for i, need, human, got in diffs:
        print(f"    불일치 #{i:>2} {need!r}  기준={'O' if human else 'X'} 채점기={'O' if got else 'X'}")


async def answer_all(rows: list[dict], today: str, search_fn, concurrency: int = 3) -> dict:
    """각 문항을 실제 서버와 같은 경로로 한 번씩 물어본다.

    `app.build_system_instruction`을 그대로 쓴다. 평가가 실제 코드와 다른
    프롬프트를 보면 그 점수는 아무것도 보장하지 않는다.
    """
    from google import genai
    from google.genai import types

    import app

    client = genai.Client()
    model = os.getenv("GEMINI_MODEL")
    sem = asyncio.Semaphore(concurrency)
    out: dict[int, str] = {}

    async def one(row):
        async with sem:
            docs = search_fn(row["질문"], k=DEFAULT_K)
            # 도구 목록도 서버와 같은 것을 넘긴다. 빠지면 재고 질문을 서버와 다르게 답한다.
            cfg = types.GenerateContentConfig(
                system_instruction=app.build_system_instruction(docs, today=today),
                tools=app.TOOLS,
            )
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=model,
                    contents=[types.Content(role="user",
                                            parts=[types.Part(text=row["질문"])])],
                    config=cfg,
                )
                text = ""
                async for chunk in stream:
                    if chunk.text:
                        text += chunk.text
                out[row["id"]] = text.strip()
            except Exception as exc:  # 한 문항이 실패해도 전체를 멈추지 않는다
                out[row["id"]] = f"__ERROR__ {type(exc).__name__}: {exc}"

    await asyncio.gather(*(one(r) for r in rows))
    return out


# ─────────────────────────────────────────────────────────────
# 학교 AI 게이트웨이(ai.cs.kookmin.ac.kr) — 모델 비교용 (#61)
# ─────────────────────────────────────────────────────────────
# 게이트웨이는 OpenAI와 같은 요청 형식을 받는다. 그래서 모델 이름만 바꿔 여러 회사 모델을
# 같은 코드로 부를 수 있다. 새 라이브러리를 들이지 않고 이미 설치된 httpx로 직접 부른다.
# 서버(app.py)는 아직 Gemini를 쓰므로 이 경로는 평가에서만 쓴다. 모델을 고른 뒤 서버 교체는 따로 한다.
KMU_AI_URL = "https://ai.cs.kookmin.ac.kr/v1/chat/completions"

# 도구를 부르고 결과를 다시 넘기는 왕복의 최대 횟수. 모델이 같은 도구를 계속 부르며 멈추지 않는 것을 막는다.
MAX_TOOL_ROUNDS = 3


def _openai_tools(functions) -> list[dict]:
    """`tools.TOOLS`의 파이썬 함수를 OpenAI 형식 도구 설명으로 바꾼다.

    Gemini는 함수를 그대로 받아 설명을 알아서 만들지만 OpenAI 형식은 JSON으로 적어 줘야 한다.
    손으로 따로 적으면 tools.py를 고칠 때 한쪽만 바뀌므로 함수의 설명(docstring)과 인자에서 만든다.
    지금 도구의 인자는 모두 문자열이라 타입은 string으로 둔다.
    """
    import inspect

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


async def answer_all_kmu(rows: list[dict], today: str, search_fn, model: str,
                         concurrency: int = 3) -> tuple[dict, dict]:
    """`answer_all`과 같은 질문을 학교 게이트웨이 모델로 물어본다.

    시스템 지시문·검색 문서·도구는 Gemini 경로와 똑같이 넘긴다. 다른 것은 모델뿐이어야
    점수 차이를 모델 차이로 읽을 수 있다.
    답변과 함께 문항별 응답 시간(초)과 토큰 수를 돌려준다. 모델을 고를 때 정확도만큼 중요하다.
    """
    import time

    import httpx

    import app

    key = os.getenv("KMU_AI_API_KEY")
    if not key:
        sys.exit(".env에 KMU_AI_API_KEY가 없습니다.")
    headers = {"Authorization": f"Bearer {key}"}
    tool_specs = _openai_tools(app.TOOLS)
    tool_fns = {fn.__name__: fn for fn in app.TOOLS}
    sem = asyncio.Semaphore(concurrency)
    out: dict[int, str] = {}
    stats: dict[int, dict] = {}

    async def one(client, row):
        async with sem:
            docs = search_fn(row["질문"], k=DEFAULT_K)
            messages = [
                {"role": "system", "content": app.build_system_instruction(docs, today=today)},
                {"role": "user", "content": row["질문"]},
            ]
            started = time.monotonic()
            tokens_in = tokens_out = 0
            try:
                for _ in range(MAX_TOOL_ROUNDS + 1):
                    resp = await client.post(KMU_AI_URL, headers=headers, json={
                        "model": model, "messages": messages, "tools": tool_specs,
                    })
                    resp.raise_for_status()
                    data = resp.json()
                    usage = data.get("usage") or {}
                    tokens_in += usage.get("prompt_tokens") or 0
                    tokens_out += usage.get("completion_tokens") or 0
                    msg = data["choices"][0]["message"]
                    calls = msg.get("tool_calls") or []
                    if not calls:
                        out[row["id"]] = (msg.get("content") or "").strip()
                        break
                    # 모델이 도구를 불렀다. 실제로 실행해 결과를 붙이고 다시 묻는다
                    messages.append(msg)
                    for call in calls:
                        fn = tool_fns.get(call["function"]["name"])
                        args = json.loads(call["function"].get("arguments") or "{}")
                        result = fn(**args) if fn else {"오류": "없는 도구입니다."}
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
                else:
                    out[row["id"]] = "__ERROR__ 도구 호출이 끝나지 않았습니다."
            except Exception as exc:  # 한 문항이 실패해도 전체를 멈추지 않는다
                detail = exc.response.text[:200] if isinstance(exc, httpx.HTTPStatusError) else exc
                out[row["id"]] = f"__ERROR__ {type(exc).__name__}: {detail}"
            stats[row["id"]] = {"초": round(time.monotonic() - started, 2),
                                "입력토큰": tokens_in, "출력토큰": tokens_out}

    # 생각(reasoning)을 길게 하는 모델이 있어 넉넉히 기다린다
    async with httpx.AsyncClient(timeout=120) as client:
        await asyncio.gather(*(one(client, r) for r in rows))
    return out, stats


def summarize_stats(stats: dict) -> dict:
    """문항별 시간·토큰을 모델 비교표에 쓸 숫자로 줄인다."""
    if not stats:
        return {}
    secs = sorted(s["초"] for s in stats.values())
    return {
        "평균초": round(sum(secs) / len(secs), 2),
        # 느린 문항 몇 개가 체감을 좌우한다. 평균만 보면 가려진다
        "상위10%초": secs[int(len(secs) * 0.9)],
        "입력토큰합": sum(s["입력토큰"] for s in stats.values()),
        "출력토큰합": sum(s["출력토큰"] for s in stats.values()),
    }


# 비교표에서 `출처구분`을 "챗봇이 하는 일" 이름으로 보여준다. 발표에서 모델별로 무엇을 잘하는지 읽히게 하려고
# 이름만 바꾼다. 묶는 기준은 골든셋의 `출처구분`(AGENTS.md 데이터 분류) 그대로다.
TASK_LABELS = {
    "문서형": "문서 답변 (검색)",
    "실시간": "실시간 조회 (도구 호출)",
    "문서형+실시간": "복합 질문 (검색+도구)",
    "학교공식": "학교 공식 안내 (링크)",
}


def compare(paths: list[str]) -> None:
    """저장된 점수표 여러 개를 모델 비교표(마크다운)로 출력한다. LLM을 부르지 않는다.

    모델마다 `--out`으로 남긴 JSON을 넘긴다. 칸 하나가 모델 하나다.
    문항 수가 적은 칸(7문항 등)은 한두 문항 차이로 크게 흔들리므로 문항 수를 같이 적는다.
    """
    runs = []
    for p in paths:
        d = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
        # 게이트웨이 실행은 "모델"이 있고, Gemini 실행은 없어서 파일 이름으로 대신한다
        runs.append((d.get("모델") or pathlib.Path(p).stem, d))

    def pct(x):
        return f"{x * 100:.0f}%" if x is not None else "-"

    print("| 항목 | " + " | ".join(name for name, _ in runs) + " |")
    print("| --- |" + " --- |" * len(runs))

    for src, label in TASK_LABELS.items():
        cells = []
        n = None
        for _, d in runs:
            row = d["요약"].get("출처구분별", {}).get(src)
            n = n or (row or {}).get("문항")
            cells.append(pct(row["요지충족률"]) if row else "-")
        print(f"| {label} 요지충족률 ({n}문항) | " + " | ".join(cells) + " |")

    cells = [pct(d["요약"]["함정"]["점수"]) for _, d in runs]
    n = runs[0][1]["요약"]["함정"]["대상"]
    print(f"| 함정 거절률 — 모르면 모른다 ({n}문항) | " + " | ".join(cells) + " |")
    print("| 금지 위반 (건) | " + " | ".join(str(len(d["요약"].get("금지위반", []))) for _, d in runs) + " |")
    print("| 오류 (건) | " + " | ".join(str(len(d["요약"].get("오류", []))) for _, d in runs) + " |")

    def stat(d, key):
        v = (d.get("속도·토큰") or {}).get(key)
        return "-" if v is None else f"{v:,}"

    for key in ("평균초", "상위10%초", "입력토큰합", "출력토큰합"):
        print(f"| {key} | " + " | ".join(stat(d, key) for _, d in runs) + " |")


def summarize(rows: list[dict], answers: dict, recall: dict,
              judged: dict[int, list[bool] | None] | None = None) -> dict:
    """점수표를 만든다. 출처구분별로 나눠 낸다.

    전체 평균만 보면 오해한다. 2주차에는 실시간·학교공식 케이스를 아직 못 푸는데,
    그것들까지 섞은 평균은 프롬프트를 고쳐도 잘 움직이지 않는다.

    `judged`가 있으면 LLM 채점기 판정으로 센다. 채점기가 실패한 문항은 점수에서 빼고
    "채점실패"로 따로 남긴다. 낱말 비교로 대신 세면 두 잣대가 섞인 숫자가 된다.
    """
    traps = [r for r in rows if r.get("함정")]
    refused = sum(1 for r in traps if grade_answer(r, answers.get(r["id"], ""))["거절"])

    by_src = defaultdict(lambda: {"문항": 0, "충족": 0, "요지": 0, "위반": 0})
    violations, errors, judge_failed = [], [], []
    # 사람이 봐야 하는 금지. 자동 채점에서 뺀 것이므로 "0건"으로 조용히 사라지지 않게 센다.
    pending = [
        {"id": r["id"], "질문": r["질문"], "함정": bool(r.get("함정")), "판정": r["금지판정"]}
        for r in rows if r.get("금지판정")
    ]
    for r in rows:
        ans = answers.get(r["id"], "")
        if ans.startswith("__ERROR__"):
            errors.append({"id": r["id"], "오류": ans})
            continue
        if r.get("함정"):
            continue
        met = None
        if judged is not None and r.get("정답요지"):
            met = judged.get(r["id"])
            if met is None:
                judge_failed.append(r["id"])
                continue
        g = grade_answer(r, ans, met)
        b = by_src[r["출처구분"]]
        b["문항"] += 1
        b["요지"] += g["정답요지"]
        b["충족"] += g["충족"]
        if g["위반"]:
            b["위반"] += 1
            violations.append({"id": r["id"], "질문": r["질문"], "위반": g["위반"]})

    for b in by_src.values():
        b["요지충족률"] = round(b["충족"] / b["요지"], 4) if b["요지"] else None

    return {
        "문항수": len(rows),
        "채점방식": "llm" if judged is not None else "rule",
        "recall@5": recall,
        "함정": {
            "대상": len(traps),
            "거절": refused,
            "점수": round(refused / len(traps), 4) if traps else 0.0,
        },
        "출처구분별": dict(by_src),
        "금지위반": violations,
        "금지판정대기": pending,
        "오류": errors,
        "채점실패": judge_failed,
    }


def report(meta: dict, summary: dict, k: int, with_llm: bool) -> None:
    rc = summary["recall@5"]
    print(f"\n{'=' * 56}")
    print(f"골든셋 {summary['문항수']}문항  ·  기준시각 {meta.get('기준시각', '?')}")
    print("=" * 56)

    print(f"\n[검색] Recall@{k}")
    print(f"  {rc['적중']} / {rc['대상']}  =  {rc['점수']:.1%}")
    for m in rc["실패"]:
        print(f"    실패 #{m['id']:>2} {m['질문'][:38]}")
        print(f"          기대 {m['기대']}")

    if not with_llm:
        print("\n[답변] --no-llm 이라 건너뜀")
        return

    tr = summary["함정"]
    print(f"\n[답변] 함정 정답률")
    print(f"  {tr['거절']} / {tr['대상']}  =  {tr['점수']:.1%}", end="")
    print(f"   (완료 기준 20건 중 16건 = 80%)")

    print(f"\n[답변] 정답요지 충족률 — 출처구분별  (채점: {summary['채점방식']})")
    for src, b in sorted(summary["출처구분별"].items()):
        rate = f"{b['요지충족률']:.1%}" if b["요지충족률"] is not None else "-"
        print(f"  {src:14} {b['충족']:>3}/{b['요지']:<3} = {rate:>6}   ({b['문항']}문항, 금지위반 {b['위반']})")

    if summary["금지위반"]:
        print(f"\n[경고] 금지 표현 위반 {len(summary['금지위반'])}건")
        for v in summary["금지위반"]:
            print(f"    #{v['id']:>2} {v['질문'][:34]} → {v['위반']}")

    pending = summary["금지판정대기"]
    if pending:
        # 이 숫자를 지우면 "위반 0건"이 다시 거짓말이 된다. 자동으로 못 재는 몫이다.
        trap_n = sum(1 for p in pending if p["함정"])
        print(f"\n[판정] 사람이 봐야 하는 금지 {len(pending)}문항"
              f"  (함정 {trap_n}건은 함정 정답률이 따로 잼)")
        for p in pending:
            if p["함정"]:
                continue
            print(f"    #{p['id']:>2} {p['질문'][:34]}")
            for c in p["판정"]:
                print(f"          {c}")

    if summary.get("채점실패"):
        print(f"\n[채점실패] {len(summary['채점실패'])}문항은 점수에서 뺐다: {summary['채점실패']}")

    if summary["오류"]:
        print(f"\n[오류] {len(summary['오류'])}건")
        for e in summary["오류"]:
            print(f"    #{e['id']:>2} {e['오류'][:70]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="골든셋으로 챗봇을 채점한다.")
    ap.add_argument("--golden", default=GOLDEN_PATH)
    ap.add_argument("-k", type=int, default=DEFAULT_K, help="Recall@k 의 k")
    ap.add_argument("--no-llm", action="store_true",
                    help="LLM을 부르지 않고 검색 지표만 낸다")
    ap.add_argument("--prompt", default=None,
                    help="프롬프트 버전 (예: v1). prompts/system_{버전}.md 를 읽는다")
    ap.add_argument("--out", default=None, help="점수표를 저장할 JSON 경로")
    ap.add_argument("--answers", default=None,
                    help="저장된 점수표(JSON)의 답변을 다시 채점한다. 답변을 새로 만들지 않는다")
    ap.add_argument("--grader", choices=["llm", "rule"], default="llm",
                    help="정답요지 채점 방식. rule은 예전 낱말 비교 (지난 기록과 비교할 때만)")
    ap.add_argument("--calibrate", action="store_true",
                    help="채점기를 기준 판정과 비교하고 끝낸다")
    ap.add_argument("--kmu-model", default=None,
                    help="학교 AI 게이트웨이 모델로 답변을 만든다 (예: claude-haiku-5-5). 채점은 그대로 Gemini")
    ap.add_argument("--compare", nargs="+", default=None, metavar="JSON",
                    help="저장된 점수표 여러 개를 모델 비교표로 출력하고 끝낸다")
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        return

    if args.calibrate:
        calibrate()
        return

    # 프롬프트 버전을 바꿔 끼운다. app 을 import 하기 전에 해야 모듈 로드 시점에 반영된다.
    if args.prompt:
        path = pathlib.Path("prompts") / f"system_{args.prompt}.md"
        if not path.exists():
            sys.exit(f"프롬프트 파일이 없습니다: {path}")
        import app
        app.SYSTEM_PROMPT = path.read_text(encoding="utf-8")

    from search import search as search_fn

    meta, rows = load_golden(args.golden)
    today = (meta.get("기준시각") or "")[:10] or None

    recall = recall_at_k(rows, args.k, search_fn)

    answers = {}
    stats = {}
    if args.answers:
        saved = json.loads(pathlib.Path(args.answers).read_text(encoding="utf-8"))
        answers = {int(k): v for k, v in saved.get("답변", {}).items()}
        print(f"저장된 답변 {len(answers)}건을 다시 채점한다: {args.answers}")
    elif not args.no_llm:
        if today is None:
            sys.exit("골든셋 메타에 기준시각이 없습니다. 상대 날짜 질문을 재현할 수 없습니다.")
        if args.kmu_model:
            print(f"게이트웨이 {args.kmu_model} 호출 {len(rows)}건... (기준시각 {today})")
            answers, stats = asyncio.run(answer_all_kmu(rows, today, search_fn, args.kmu_model))
        else:
            print(f"LLM 호출 {len(rows)}건... (기준시각 {today})")
            answers = asyncio.run(answer_all(rows, today, search_fn))

    judged = None
    if answers and args.grader == "llm":
        judged = judge_answers(rows, answers)

    summary = summarize(rows, answers, recall, judged)
    report(meta, summary, args.k, bool(answers))

    if args.out:
        payload = {"메타": meta, "프롬프트": args.prompt or "현재", "요약": summary}
        if args.kmu_model:
            payload["모델"] = args.kmu_model
            payload["속도·토큰"] = summarize_stats(stats)
            payload["문항별속도"] = stats
        if answers:
            payload["답변"] = answers
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n점수표 저장: {args.out}")


if __name__ == "__main__":
    main()
