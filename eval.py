"""골든셋으로 챗봇을 채점한다.

`eval/golden.jsonl`을 읽어 세 가지를 잰다.

- **Recall@5** — 검색 상위 k건에 근거문서가 들어있나. 검색만 보므로 LLM을 부르지 않는다
- **함정 정답률** — 답이 없는 질문에 "모르겠다"고 했나. 환각을 재는 유일한 지표다
- **답변 채점** — `정답요지`가 답에 들어있고 `금지`가 안 들어있나

세 지표는 성격이 다르다. Recall@5는 `search.py`를, 함정 정답률과 답변 채점은
프롬프트를 본다. **프롬프트를 고쳤는데 Recall@5가 그대로인 것은 정상이다.**
v1/v2를 비교할 때는 뒤의 둘로 판단한다.

실행:
    python eval.py                  전체 (LLM 호출 있음, 느리다)
    python eval.py --no-llm         검색 지표만 (빠르다)
    python eval.py --prompt v1      프롬프트 버전을 바꿔 비교
    python eval.py --out eval/run-v2.json   점수표를 파일로 남긴다
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


def grade_answer(row: dict, answer: str) -> dict:
    """답변 하나를 채점한다.

    함정은 거절했는지만 본다. 그 외에는 `정답요지`가 담겼는지와 `금지`를
    어겼는지를 따로 센다.

    금지는 정답요지보다 엄하게 본다. "조형대학"처럼 **틀린 답을 실제로 말했는지**를
    보는 것이라 느슨하게 잡으면 사고를 놓친다. 그래서 부분 문자열 그대로 쓴다.
    """
    if row.get("함정"):
        return {"함정": True, "거절": _refused(answer)}

    need = row.get("정답요지") or []
    met = [n for n in need if _covered(n, answer)]
    flat = answer.replace(" ", "")
    banned = [b for b in (row.get("금지") or []) if b.replace(" ", "") in flat]
    return {
        "함정": False,
        "정답요지": len(need),
        "충족": len(met),
        "위반": banned,
        "거절": _refused(answer),
    }


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
            cfg = types.GenerateContentConfig(
                system_instruction=app.build_system_instruction(docs, today=today)
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


def summarize(rows: list[dict], answers: dict, recall: dict) -> dict:
    """점수표를 만든다. 출처구분별로 나눠 낸다.

    전체 평균만 보면 오해한다. 2주차에는 실시간·학교공식 케이스를 아직 못 푸는데,
    그것들까지 섞은 평균은 프롬프트를 고쳐도 잘 움직이지 않는다.
    """
    traps = [r for r in rows if r.get("함정")]
    refused = sum(1 for r in traps if grade_answer(r, answers.get(r["id"], ""))["거절"])

    by_src = defaultdict(lambda: {"문항": 0, "충족": 0, "요지": 0, "위반": 0})
    violations, errors = [], []
    for r in rows:
        ans = answers.get(r["id"], "")
        if ans.startswith("__ERROR__"):
            errors.append({"id": r["id"], "오류": ans})
            continue
        if r.get("함정"):
            continue
        g = grade_answer(r, ans)
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
        "recall@5": recall,
        "함정": {
            "대상": len(traps),
            "거절": refused,
            "점수": round(refused / len(traps), 4) if traps else 0.0,
        },
        "출처구분별": dict(by_src),
        "금지위반": violations,
        "오류": errors,
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

    print(f"\n[답변] 정답요지 충족률 — 출처구분별")
    for src, b in sorted(summary["출처구분별"].items()):
        rate = f"{b['요지충족률']:.1%}" if b["요지충족률"] is not None else "-"
        print(f"  {src:14} {b['충족']:>3}/{b['요지']:<3} = {rate:>6}   ({b['문항']}문항, 금지위반 {b['위반']})")

    if summary["금지위반"]:
        print(f"\n[경고] 금지 표현 위반 {len(summary['금지위반'])}건")
        for v in summary["금지위반"]:
            print(f"    #{v['id']:>2} {v['질문'][:34]} → {v['위반']}")

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
    args = ap.parse_args()

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
    if not args.no_llm:
        if today is None:
            sys.exit("골든셋 메타에 기준시각이 없습니다. 상대 날짜 질문을 재현할 수 없습니다.")
        print(f"LLM 호출 {len(rows)}건... (기준시각 {today})")
        answers = asyncio.run(answer_all(rows, today, search_fn))

    summary = summarize(rows, answers, recall)
    report(meta, summary, args.k, not args.no_llm)

    if args.out:
        payload = {"메타": meta, "프롬프트": args.prompt or "현재", "요약": summary}
        if answers:
            payload["답변"] = answers
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n점수표 저장: {args.out}")


if __name__ == "__main__":
    main()
