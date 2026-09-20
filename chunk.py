"""청킹 + 메타데이터.

문서를 검색 단위(청크)로 자르고 메타데이터를 붙이는 일만 한다.
인덱싱과 검색은 하지 않는다. `build_index.py`가 이 모듈의 출력을 받아 인덱스를 만든다.

반환 모양을 `search.py`와 똑같이 맞췄다. 인덱스를 거쳐도 `app.py`가 받는 모양이
바뀌지 않아야 하기 때문이다.

백엔드 API 필드명(`noticeId`인지 `notice_id`인지 등)은 여기서 다루지 않는다.
그 변환은 `sync.py`가 맡는다. 이 모듈은 이미 우리 모양으로 정리된 문서만 받는다.
"""

import re

# AGENTS.md "청킹 규칙". 공지는 500~800자에 앞뒤 100자를 겹친다.
# 겹치는 이유는 문단 경계에서 잘린 문장이 어느 청크에서도 온전하지 않게 되는 것을 막기 위해서다.
#
# 실제 공지는 이 기준에 거의 닿지 않는다. 2026-09-20 실측으로 카톡 공지가 360자,
# 목업 공지 12건이 71~230자였다. 그래도 기준을 낮추지 않는다. 지금 공지는 "신청 기간",
# "참가비"처럼 짧은 항목이 나열된 형태라, 쪼개면 한 질문에 필요한 항목이 서로 다른
# 청크로 갈라져 반쪽 답이 나간다. 긴 공지가 올 때를 위한 안전망으로 남겨둔다.
MIN_CHARS = 500
MAX_CHARS = 800
OVERLAP = 100

# 메타데이터는 이 여섯 개로 고정한다. `app.py`의 build_context가 이 이름으로 읽는다.
META_KEYS = ("문서ID", "출처URL", "제목", "게시일", "유효기간", "문서유형")

# 표는 행 단위로 쪼개면 머리글을 잃어서 숫자만 남는다. 그래서 통째로 한 청크로 둔다.
_TABLE_LINE = re.compile(r"^\s*\|")

# 규정은 문자 수로 자르지 않는다. "제3조 2항"처럼 조항 번호로 인용되는 문서라
# 조항 중간에서 끊기면 인용이 깨진다.
_ARTICLE_HEAD = re.compile(r"(?=^제\s*\d+\s*조)", re.MULTILINE)

# 문단 경계가 없을 때 차선으로 쓰는 문장 끝. 한국어 종결어미는 마침표로 끝나므로
# 마침표 뒤 공백(줄바꿈 포함)만 봐도 충분하다.
_SENTENCE_END = re.compile(r"[.!?]\s")


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """본문을 표 블록과 글 블록으로 나눈다. 순서는 원문 그대로 유지한다.

    표를 먼저 떼어내지 않으면 500자 창이 표 중간을 지나가며 잘라버린다.
    빈 줄은 표가 아니므로 표 블록을 끝내는 역할도 한다.
    """
    blocks: list[tuple[str, str]] = []
    for line in text.split("\n"):
        kind = "표" if _TABLE_LINE.match(line) else "글"
        if blocks and blocks[-1][0] == kind:
            blocks[-1] = (kind, f"{blocks[-1][1]}\n{line}")
        else:
            blocks.append((kind, line))
    # 공백만 남은 블록은 버린다. 표 앞뒤의 빈 줄이 글 블록으로 잡히기 때문이다.
    return [(kind, body) for kind, body in blocks if body.strip()]


def _is_rule(doc_type: str, text: str) -> bool:
    """조항 단위로 잘라야 하는 문서인지 본다.

    문서유형만 보지 않는 이유는 두 가지다. "학생회 규정"처럼 이름이 늘어날 수 있고,
    반대로 유형이 규정인데 본문에 조항이 없으면 통째로 한 덩어리가 되어 검색이 안 된다.
    이름과 본문이 모두 맞을 때만 조항 분할을 쓰고, 아니면 글자 수로 자른다.
    """
    return "규정" in doc_type and _ARTICLE_HEAD.search(text) is not None


def _split_articles(text: str) -> list[str]:
    """규정을 조항 단위로 나눈다. 첫 조항 앞의 제목·전문은 따로 한 청크로 둔다."""
    return [part.strip() for part in _ARTICLE_HEAD.split(text) if part.strip()]


def _find_break(text: str, start: int) -> int:
    """`start`에서 시작하는 청크를 어디서 끊을지 고른다.

    500자 지점부터 800자 지점 사이에서 문단 경계를 먼저 찾고, 없으면 문장 끝을 찾는다.
    둘 다 없으면 800자에서 자른다. 표 없는 긴 한 문단이면 어쩔 수 없다.
    """
    lo, hi = start + MIN_CHARS, start + MAX_CHARS
    window = text[lo:hi]

    paragraph = window.rfind("\n\n")
    if paragraph != -1:
        return lo + paragraph + 2

    last = -1
    for match in _SENTENCE_END.finditer(window):
        last = match.end()
    if last != -1:
        return lo + last

    return hi


def _window(text: str) -> list[str]:
    """글 블록을 500~800자로 자르며 앞뒤 100자를 겹친다."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_CHARS:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        # 남은 길이가 한 청크에 들어가면 더 자르지 않는다.
        # 억지로 자르면 마지막 청크가 몇 글자만 남는다.
        if len(text) - start <= MAX_CHARS:
            chunks.append(text[start:].strip())
            break
        end = _find_break(text, start)
        chunks.append(text[start:end].strip())
        # max로 감싸 진행을 보장한다. 겹침 때문에 start가 뒤로 가면 무한 반복이 된다.
        start = max(end - OVERLAP, start + 1)
    return [piece for piece in chunks if piece]


def _normalize_meta(meta: dict) -> dict:
    """메타데이터를 여섯 칸으로 맞춘다.

    게시일이 없으면 여기서 막는다. 지난 학기 공지가 올해 답으로 나가는 것이
    가장 흔한 사고인데, 게시일이 없으면 오래된 공지인지 판단할 근거가 사라진다.
    인덱스를 만든 뒤에 알아차리면 이미 틀린 답이 나간 상태다.

    유효기간은 비어 있어도 된다. 빌릴게 이용 방법처럼 만료가 없는 고정 정보가 있다.
    """
    doc_id = str(meta.get("문서ID") or "").strip()
    if not doc_id:
        raise ValueError("문서ID가 없습니다. 출처를 표시할 수 없어 인덱싱하지 않습니다.")
    if not str(meta.get("게시일") or "").strip():
        raise ValueError(f"[{doc_id}] 게시일이 없습니다. 유효기간을 판단할 수 없습니다.")

    return {key: meta.get(key) or "" for key in META_KEYS}


def chunk(doc: dict) -> list[dict]:
    """문서 하나를 청크 목록으로 바꾼다.

    Args:
        doc: {"text": 본문, "meta": {문서ID, 출처URL, 제목, 게시일, 유효기간, 문서유형}}
             `문서유형`에 "규정"이 들어가고 본문에 `제N조`가 있으면 조항 단위로,
             그 외에는 500~800자로 자른다.

    Returns:
        [{"text": 청크 본문, "meta": {...META_KEYS, 청크번호, 청크수}}, ...]
        본문이 비어 있으면 빈 리스트.

    Raises:
        ValueError: 문서ID나 게시일이 없을 때.
    """
    meta = _normalize_meta(doc.get("meta", {}))
    text = (doc.get("text") or "").strip()
    if not text:
        return []

    pieces: list[str] = []
    for kind, body in _split_blocks(text):
        if kind == "표":
            pieces.append(body.strip())
        elif _is_rule(meta["문서유형"], body):
            pieces += _split_articles(body)
        else:
            pieces += _window(body)

    # 청크번호를 붙이는 이유는 같은 문서에서 나온 청크가 여러 건 검색됐을 때
    # 사람이 원문 위치를 찾아갈 수 있게 하기 위해서다. 출처 표시에는 문서ID만 쓴다.
    total = len(pieces)
    return [
        {"text": piece, "meta": {**meta, "청크번호": i, "청크수": total}}
        for i, piece in enumerate(pieces, start=1)
    ]


def chunk_docs(docs: list[dict]) -> list[dict]:
    """문서 여러 건을 한 번에 청킹한다."""
    chunks = []
    for doc in docs:
        chunks += chunk(doc)
    return chunks
