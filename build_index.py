"""BM25 인덱스 빌더.

문서를 모아 `chunk.py`로 자르고, 색인용 텍스트를 `normalize.py`로 다듬어
`search.py`가 읽을 인덱스 JSON을 만든다.

색인 대상은 db-schema.md의 "RAG 인덱싱" 경로를 따른다.
- docs/seed/ 고정 정보 문서
- notices, archives, open_feedbacks (test_data/notices.json)
- events 소개글 (test_data/events.json)

실시간으로 바뀌는 값은 넣지 않는다. 빌릴게 재고와 사물함 잔여 수량은 인덱싱하면
"우산 3개 남음"이라고 답한 뒤 실제로 0개인 상황이 생긴다.

사용:
    python build_index.py                      # 기본 경로로 빌드
    python build_index.py --out index/bm25.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import chunk as chunker
import normalize
import search as searcher

SEED_DIR = "docs/seed"
DATA_DIR = "test_data"
OUT_PATH = "index/bm25.json"

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _date(value: str) -> str:
    """ISO8601이든 'YYYY-MM-DD'든 날짜 부분만 남긴다."""
    return (value or "")[:10]


def load_seed(seed_dir: str = SEED_DIR) -> list[dict]:
    """docs/seed/*.md 를 읽는다. 메타데이터는 front matter에 적혀 있다."""
    docs = []
    if not os.path.isdir(seed_dir):
        _log(f"[건너뜀] 시드 폴더가 없습니다: {seed_dir}")
        return docs

    for name in sorted(os.listdir(seed_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(seed_dir, name)
        raw = open(path, encoding="utf-8").read()
        matched = _FRONT_MATTER.match(raw)
        if not matched:
            _log(f"[건너뜀] front matter가 없습니다: {path}")
            continue
        meta = {
            key: value.strip().strip('"')
            for key, value in re.findall(r"^(\S+):\s*(.*)$", matched.group(1), re.M)
        }
        docs.append({"text": matched.group(2), "meta": meta})
    return docs


def load_notices(data_dir: str = DATA_DIR) -> list[dict]:
    """notices / archives / open_feedbacks 를 읽는다.

    isDeleted가 참인 행은 여기서 거른다. 삭제된 공지를 챗봇이 계속 답하는 것이
    가장 흔한 사고라, 인덱스에 들어가기 전에 막는다.
    """
    path = os.path.join(data_dir, "notices.json")
    raw = json.load(open(path, encoding="utf-8"))
    docs = []

    for n in raw.get("notices", []):
        if n.get("isDeleted"):
            continue
        docs.append({"text": n["content"], "meta": {
            "문서ID": f"notice-{n['noticeId']}", "제목": n["title"],
            "게시일": _date(n.get("createdAt")), "유효기간": "",
            "출처URL": "", "문서유형": "공지"}})

    for a in raw.get("archives", []):
        docs.append({"text": a["content"], "meta": {
            "문서ID": f"archive-{a['archiveId']}", "제목": a["title"],
            "게시일": _date(a.get("createdAt")), "유효기간": _date(a.get("endDate")),
            "출처URL": "", "문서유형": "아카이빙"}})

    for f in raw.get("openFeedbacks", []):
        # 답변이 없는 글은 넣지 않는다. 질문만 검색되면 답이 없는 문서를 근거로 삼게 된다.
        if f.get("isDeleted") or not f.get("answer"):
            continue
        docs.append({"text": f"질문: {f['question']}\n답변: {f['answer']}", "meta": {
            "문서ID": f"feedback-{f['feedbackId']}", "제목": f["question"],
            "게시일": _date(f.get("createdAt")), "유효기간": "",
            "출처URL": "", "문서유형": "열린피드백"}})

    return docs


def load_events(data_dir: str = DATA_DIR) -> list[dict]:
    """행사 소개글을 읽는다.

    날짜·정원처럼 바뀌는 값은 function calling으로 조회하고, 여기서는 소개글만 넣는다.
    events에는 작성일 컬럼이 없어 게시일은 신청 시작일(없으면 행사 시작일)로 둔다.
    """
    path = os.path.join(data_dir, "events.json")
    raw = json.load(open(path, encoding="utf-8"))
    docs = []
    for e in raw.get("events", []):
        if e.get("isDeleted"):
            continue
        published = _date(e.get("applyStartAt") or e.get("eventStartAt"))
        docs.append({
            "text": f"{e['title']}\n{e.get('description', '')}\n장소: {e.get('place', '')}",
            "meta": {
                "문서ID": f"event-{e['eventId']}", "제목": e["title"],
                "게시일": published, "유효기간": _date(e.get("eventEndAt")),
                "출처URL": "", "문서유형": "행사"},
        })
    return docs


def build(docs: list[dict], use_normalize: bool = True) -> dict:
    """문서를 청킹해 인덱스 구조를 만든다.

    문서 한 건이 실패해도 전체를 멈추지 않는다. 게시일이 빠진 문서 하나 때문에
    인덱싱이 통째로 죽으면, 급할 때 원인을 찾기 어렵다. 어떤 문서가 빠졌는지만 남긴다.

    Args:
        use_normalize: 색인용 텍스트에서 인사말·맺음말을 뺄지. 2주차 전후 비교에서
            False로 한 번, True로 한 번 빌드해 점수를 비교한다.
    """
    chunks = []
    skipped = 0
    for doc in docs:
        try:
            pieces = chunker.chunk(doc)
        except ValueError as error:
            skipped += 1
            _log(f"[건너뜀] {error}")
            continue
        for piece in pieces:
            # 사람에게 보여줄 원문은 그대로 두고, 검색용 토큰만 정규화한 텍스트로 만든다.
            indexed = normalize.normalize(piece["text"]) if use_normalize else piece["text"]
            chunks.append({
                "text": piece["text"],
                "meta": piece["meta"],
                "tokens": searcher.tokenize(f"{piece['meta'].get('제목', '')}\n{indexed}"),
            })

    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "normalized": use_normalize,
        "doc_count": len(docs) - skipped,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="BM25 인덱스를 만든다.")
    parser.add_argument("--seed-dir", default=SEED_DIR)
    parser.add_argument("--data-dir", default=DATA_DIR)
    parser.add_argument("--out", default=OUT_PATH)
    parser.add_argument("--no-normalize", action="store_true",
                        help="인사말·맺음말 정규화를 끄고 빌드한다 (전후 비교용)")
    args = parser.parse_args()

    docs = load_seed(args.seed_dir) + load_notices(args.data_dir) + load_events(args.data_dir)
    index = build(docs, use_normalize=not args.no_normalize)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    _log(f"문서 {index['doc_count']}건 → 청크 {index['chunk_count']}개 → {args.out}")


if __name__ == "__main__":
    main()
