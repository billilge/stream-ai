"""금지 문구 점검 보고서.

test_data/test_cases.json의 금지 문구를 모아 마크다운 표로 뽑는다. 같이 보려고 만든다.

보는 것은 두 가지다.

1. 문구가 삭제된 행이나 지난 학기 글에 실제로 들어 있는지.
   없으면 챗봇이 그 말을 할 일도 없어서 그 금지 항목은 아무것도 지키지 못한다.
2. 그 문구가 살아 있는 문서에도 들어 있는지.
   들어 있으면 맞는 답을 하면서도 그 말을 쓸 수 있어, 멀쩡한 답이 위반으로 잡힌다.

사용:
    python tools/banned_report.py > docs/eval/banned-words.md
"""

import json
import re
import sys

CASES_PATH = "test_data/test_cases.json"
NOTICES_PATH = "test_data/notices.json"
EVENTS_PATH = "test_data/events.json"


def _load_sources() -> tuple[dict[str, str], dict[str, str]]:
    """문서ID → 본문. (살아 있는 문서, 걸러져야 하는 문서)로 나눠 돌려준다.

    걸러져야 하는 문서는 두 종류다. isDeleted가 참인 소프트 삭제 행과, 삭제는 아니지만
    지난 학기 글이라 이번 학기 답에 쓰면 안 되는 글이다. 뒤쪽은 날짜로 걸러야 한다.
    """
    live: dict[str, str] = {}
    filtered: dict[str, str] = {}

    notices = json.load(open(NOTICES_PATH, encoding="utf-8"))
    for n in notices.get("notices", []):
        target = filtered if n.get("isDeleted") else live
        target[f"notice-{n['noticeId']}"] = f"{n['title']} {n['content']}"
    for a in notices.get("archives", []):
        # 아카이빙은 소프트 삭제가 없다. 지난 학기 글은 날짜로 걸러야 해서 여기 둔다.
        bucket = filtered if a.get("startDate", "") < "2026-07-01" else live
        bucket[f"archive-{a['archiveId']}"] = f"{a['title']} {a['content']}"
    for f in notices.get("openFeedbacks", []):
        live[f"feedback-{f['feedbackId']}"] = f"{f['question']} {f.get('answer') or ''}"

    events = json.load(open(EVENTS_PATH, encoding="utf-8"))
    for e in events.get("events", []):
        text = f"{e['title']} {e.get('description', '')} {e.get('applyEndAt', '')} {e.get('eventStartAt', '')}"
        (filtered if e.get("isDeleted") else live)[f"event-{e['eventId']}"] = text
    return live, filtered


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _with_dates(text: str) -> str:
    """ISO 날짜를 사람이 쓰는 표기로도 덧붙인다.

    목업은 날짜를 2026-10-08로 저장하지만 챗봇은 "10월 8일"이나 "10/8"로 답한다.
    원문만 보면 날짜 금지 문구가 데이터에 없는 것처럼 보여 점검이 헛돈다.
    """
    extra = []
    for year, month, day in _ISO.findall(text):
        m, d = int(month), int(day)
        extra += [f"{m}/{d}", f"{m}월 {d}일"]
    return text + " " + " ".join(extra)


def _where(phrase: str, docs: dict[str, str]) -> list[str]:
    return sorted(doc_id for doc_id, text in docs.items() if phrase in _with_dates(text))


def main() -> None:
    cases = json.load(open(CASES_PATH, encoding="utf-8"))["cases"]
    live, filtered = _load_sources()

    out = [
        "# 금지 문구 점검",
        "",
        "`test_data/test_cases.json`에서 뽑았다. `python tools/banned_report.py`로 다시 만든다.",
        "",
        "- **걸러질 문서**: 그 문구가 삭제된 행이나 지난 학기 글에 들어 있다. 챗봇이 필터를 "
        "빠뜨리면 이 말을 하게 되므로 금지 문구로 쓸 수 있다.",
        "- **살아 있는 문서**: 그 문구가 지금도 유효한 문서에 들어 있다. 맞는 답에도 섞일 수 "
        "있어 멀쩡한 답이 위반으로 잡힐 수 있다. 비어 있는 쪽이 좋다.",
        "",
        "| 문항 | 질문 | 금지 문구 | 사유 | 걸러질 문서 | 살아 있는 문서 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    manual = []
    risky = []
    useless = []

    for case in cases:
        if "must_not" not in case:
            continue
        question = case["질문"] if "질문" in case else case.get("q", "")
        reason = case.get("must_not_reason", "")
        if case.get("must_not_check") == "판정":
            manual.append(f"- #{case['id']} {question} — {reason}")
            continue

        for phrase in case["must_not"]:
            hit_filtered = _where(phrase, filtered)
            hit_live = _where(phrase, live)
            if not hit_filtered:
                useless.append(f"- #{case['id']} `{phrase}`")
            if hit_live:
                risky.append(f"- #{case['id']} `{phrase}` → {', '.join(hit_live)}")
            out.append(
                f"| #{case['id']} | {question} | `{phrase}` | {reason} | "
                f"{', '.join(hit_filtered) or '—'} | {', '.join(hit_live) or '—'} |"
            )

    out += ["", "## 사람이 봐야 하는 것", ""]
    out += manual or ["- 없음"]

    out += ["", "## 살아 있는 문서에도 있는 문구", "",
            "맞는 답에도 섞일 수 있어 오탐이 날 수 있다. 문구를 더 좁히거나 채점에서 문맥을 함께 봐야 한다.", ""]
    out += risky or ["- 없음"]

    out += ["", "## 걸러질 문서에서 못 찾은 문구", "",
            "데이터에 없는 말이라 챗봇이 할 일도 없다. 문구가 틀렸거나 데이터가 바뀐 것이다.", ""]
    out += useless or ["- 없음"]

    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())
