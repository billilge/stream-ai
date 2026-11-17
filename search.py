"""검색 모듈.

질문과 관련된 문서를 찾아 돌려주는 일만 한다. 답변 생성은 하지 않는다.

스텁을 BM25 검색으로 바꿨다. `search(query)`의 이름과 반환 모양은 그대로라
`app.py`는 고치지 않아도 된다. 상위 몇 건을 받을지 고르고 싶을 때만 `k`를 넘긴다.

인덱스는 `build_index.py`가 만들어 둔 JSON 파일을 읽는다. 라이브러리는 쓰지 않는다.
BM25는 식이 짧아 직접 구현하는 편이 의존성을 늘리는 것보다 낫고, 점수 계산을
눈으로 확인할 수 있어야 2주차 전후 비교에서 무엇이 달라졌는지 설명할 수 있다.
"""

import json
import math
import os
import re

INDEX_PATH = os.environ.get("STREAM_INDEX_PATH", "index/bm25.json")

# BM25 상수. k1은 같은 낱말이 여러 번 나올 때 점수가 얼마나 더 오르는지,
# b는 문서 길이로 얼마나 깎을지를 정한다. 일반적인 기본값을 쓴다.
K1 = 1.5
B = 0.75

DEFAULT_K = 5

_HANGUL_OR_WORD = re.compile(r"[가-힣]+|[A-Za-z]+|\d+")
_HANGUL = re.compile(r"^[가-힣]+$")

# 조사·접미사만 떨어져 나온 토큰은 버린다. 어느 문서에나 있어서 변별력이 없다.
# 줄임말·다른 이름. 질문에만 적용하고 인덱스에는 넣지 않는다. 인덱스까지 부풀리면
# 엉뚱한 문서가 같은 낱말을 갖게 되어 점수가 흐려진다.
# 한 줄이 한 묶음이고, 묶음 안의 낱말은 서로 바꿔 쓴다.
SYNONYMS = (
    ("개총", "개강총회"),
    ("종총", "종강총회"),
    ("체전", "체육대회"),
    ("학회비", "학생회비"),
    ("과방", "학생회실"),
    ("대여", "빌릴게", "빌리", "빌려"),
    ("사물함", "락커"),
    ("슬랑제", "선배와의 만남"),
)

_SYNONYM_MAP: dict[str, tuple[str, ...]] = {}
for _group in SYNONYMS:
    for _word in _group:
        _SYNONYM_MAP[_word] = _group

_STOP = {
    "은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "도", "만",
    "와", "과", "하고", "이나", "인가", "인지", "있나", "있어", "있나요", "뭐", "뭔가",
    "어떻게", "어디", "언제", "알려줘", "알려", "주세요", "해줘", "좀", "그리고",
}


def tokenize(text: str) -> list[str]:
    """한국어 검색용 토큰을 만든다.

    낱말을 그대로 쓰면 "사물함"과 "사물함은"이 다른 낱말이 되어 검색이 어긋난다.
    형태소 분석기를 붙이면 정확하지만 설치가 무겁고 배포가 까다로워, 한글 덩어리는
    낱말과 2글자 조각을 함께 넣는 방식으로 대신한다. "사물함은"이 "사물함"과
    "사물", "물함", "함은"을 공유하므로 조사가 붙어도 걸린다.

    2주차에 정규화·검색 점수를 비교할 때 이 함수가 기준선이다. 형태소 분석기로
    바꾸고 싶으면 여기만 갈아끼우면 된다.
    """
    tokens: list[str] = []
    for word in _HANGUL_OR_WORD.findall(text.lower()):
        if word in _STOP:
            continue
        tokens.append(word)
        if _HANGUL.match(word) and len(word) > 2:
            tokens += [word[i:i + 2] for i in range(len(word) - 1)]
    return tokens


def expand(tokens: list[str]) -> list[str]:
    """질문 토큰에 줄임말의 다른 이름을 더한다.

    "개총언제얌"의 개총과 문서의 개강총회가 글자를 공유하지 않아 BM25가 못 찾는다.
    형태소 분석기를 붙여도 해결되지 않는 문제라 사전으로 다룬다.
    """
    extra: list[str] = []
    for token in tokens:
        for word in _SYNONYM_MAP.get(token, ()):
            if word != token:
                extra += tokenize(word)
    return tokens + extra


# "최근", "요즘", "한달간"처럼 최신순을 묻는 말. BM25는 낱말이 겹치는 정도만 보므로
# 이런 질문에서 최신 공지를 찾지 못한다. 날짜로 정렬해 주는 경로가 따로 필요하다.
_RECENCY = re.compile(r"최근|최신|요즘|근래|새로\s*올라온|한\s*달|이번\s*주|며칠")
# 날짜순으로 돌려줄 문서 종류. 행사·안내는 최신순이 의미가 없어 넣지 않는다.
_DATED_TYPES = ("공지", "아카이빙")


def is_recency_query(query: str) -> bool:
    """최신순으로 답해야 하는 질문인지."""
    return bool(_RECENCY.search(query))


def recent_chunks(index: dict, k: int) -> list[int]:
    """게시일이 늦은 순으로 공지 청크 위치를 돌려준다."""
    dated = [
        (i, c["meta"].get("게시일", ""))
        for i, c in enumerate(index["chunks"])
        if c["meta"].get("문서유형") in _DATED_TYPES and c["meta"].get("게시일")
    ]
    dated.sort(key=lambda x: x[1], reverse=True)
    return [i for i, _ in dated[:k]]


def load_index(path: str = INDEX_PATH) -> dict:
    """인덱스 파일을 읽어 BM25 계산에 필요한 값까지 미리 만들어 둔다."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    chunks = raw["chunks"]
    freqs = []
    lengths = []
    doc_freq: dict[str, int] = {}
    for c in chunks:
        tokens = c.get("tokens") or tokenize(c["text"])
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        freqs.append(tf)
        lengths.append(len(tokens))
        for t in tf:
            doc_freq[t] = doc_freq.get(t, 0) + 1

    total = len(chunks)
    avg_len = (sum(lengths) / total) if total else 0.0
    # idf는 질문마다 다시 계산할 필요가 없으므로 여기서 한 번만 구한다.
    idf = {
        t: math.log(1 + (total - df + 0.5) / (df + 0.5))
        for t, df in doc_freq.items()
    }
    return {
        "chunks": chunks,
        "freqs": freqs,
        "lengths": lengths,
        "avg_len": avg_len,
        "idf": idf,
        "built_at": raw.get("built_at", ""),
    }


_INDEX: dict | None = None


def _index() -> dict:
    global _INDEX
    if _INDEX is None:
        _INDEX = load_index()
    return _INDEX


def use_index(path: str) -> None:
    """검색에 쓸 인덱스를 다른 파일로 바꿔 끼운다. `refresh.py`가 새 인덱스를 만든 뒤 부른다.

    새 파일을 끝까지 읽고 계산까지 마친 뒤에 한 번에 바꾼다. 그래서 바꾸는 도중에 들어온 질문도
    옛 인덱스나 새 인덱스 중 하나로 온전히 검색된다. 반쯤 바뀐 인덱스를 보는 일은 없다.
    파일이 깨져 읽다가 실패하면 예외가 나고 기존 인덱스는 그대로 남는다.
    """
    global _INDEX
    _INDEX = load_index(path)


def score(query: str, index: dict | None = None) -> list[tuple[int, float]]:
    """질문에 대한 청크별 BM25 점수를 (청크 위치, 점수)로 돌려준다.

    점수가 0인 청크는 낱말이 하나도 겹치지 않은 것이라 뺀다. 관련 문서가 없을 때
    빈 목록이 나와야 "모르겠습니다" 경로가 살아 있다.
    """
    idx = index or _index()
    q_tokens = expand(tokenize(query))
    if not q_tokens:
        return []

    results = []
    for i, tf in enumerate(idx["freqs"]):
        length = idx["lengths"][i]
        total = 0.0
        for t in q_tokens:
            f = tf.get(t)
            if not f:
                continue
            denom = f + K1 * (1 - B + B * length / (idx["avg_len"] or 1))
            total += idx["idf"].get(t, 0.0) * f * (K1 + 1) / denom
        if total > 0:
            results.append((i, total))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def search(query: str, k: int = DEFAULT_K, index: dict | None = None) -> list[dict]:
    """질문과 관련된 문서를 점수 높은 순으로 반환한다.

    Args:
        query: 학생이 물어본 질문 원문.
        k: 최대 몇 건을 돌려줄지. 기본 5건이며 eval의 Recall@5와 같은 값이다.
        index: 테스트에서 다른 인덱스를 끼워 넣고 싶을 때만 쓴다.

    Returns:
        [{"text": 본문, "meta": {문서ID, 제목, 게시일, 유효기간, 출처URL, 문서유형,
          청크번호, 청크수, 점수}}, ...]
        관련 문서가 없으면 빈 리스트.

    같은 문서에서 여러 청크가 걸리면 가장 점수가 높은 청크 하나만 남긴다. 한 문서가
    상위 5건을 채워 다른 문서를 밀어내면, 두 가지를 함께 물어보는 질문에서 반쪽 답이 된다.
    """
    idx = index or _index()
    chunks = idx["chunks"]

    picked: list[dict] = []
    seen: set[str] = set()

    # "최근 공지" 류는 날짜순을 먼저 채우고 남은 자리를 BM25로 메운다. 둘을 합치는 이유는
    # "한달간 공지랑 가까운 행사"처럼 최신순과 내용 검색이 한 질문에 같이 오기 때문이다.
    ordered: list[tuple[int, float]] = []
    if is_recency_query(query):
        # 날짜순이 상위를 다 차지하면 "한달간 공지랑 가까운 행사"에서 행사가 밀린다.
        # 절반만 날짜순으로 채우고 나머지는 내용 검색에 맡긴다.
        ordered += [(i, 0.0) for i in recent_chunks(idx, (k + 1) // 2)]
    ordered += score(query, idx)

    for i, value in ordered:
        chunk = chunks[i]
        doc_id = chunk["meta"].get("문서ID", "")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        picked.append({
            "text": chunk["text"],
            "meta": {**chunk["meta"], "점수": round(value, 4)},
        })
        if len(picked) >= k:
            break
    return picked
