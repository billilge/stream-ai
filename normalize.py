"""인사말·맺음말 정규화.

BM25 색인용 텍스트에서 모든 공지에 공통으로 들어가는 인사말·맺음말·장식 줄을 뺀다.
실측(2026-09-20)으로 카톡 공지 360자 중 116자(32%)가 이 부분이었다. 모든 문서에
똑같이 들어가는 말은 변별력이 없으면서 문서 길이만 늘려 BM25 점수를 왜곡한다.

**색인용 텍스트에만 쓴다.** 답변 생성에 넘기는 원문은 그대로 둔다. 인사말이 빠진
본문을 그대로 보여주면 사람이 읽는 답변이 어색해진다.

설계 원칙은 "애매하면 남긴다"다. 인사말 한 줄을 못 지우는 손해보다 신청 기간 한 줄을
지우는 손해가 훨씬 크다. 그래서 숫자나 핵심 키워드가 들어간 줄은 인사말 패턴에
걸려도 남긴다.
"""

import re

# 이 낱말이 들어간 줄은 인사말 패턴에 걸려도 지우지 않는다. 사실이 실려 있을 수 있다.
_KEEP_WORDS = (
    "신청", "접수", "마감", "기간", "일시", "장소", "대상", "정원", "선착순",
    "취소", "반납", "수령", "준비물", "비용", "회비", "문의", "링크",
)
_HAS_DIGIT = re.compile(r"\d")

# 줄 전체가 인사·맺음말인 경우. 한 줄을 통째로 지운다.
# 누가 쓴 글인지 밝히는 첫 줄. 실제 공지는 "안녕하십니까, 제10대 소프트웨어융합대학
# 학생회 \'에코\'입니다."로 시작하는데, 모든 공지에 똑같이 붙어 변별력이 전혀 없다.
# 기수에 숫자가 들어 있어 "숫자가 있으면 남긴다"는 보수적인 규칙에 걸리므로, 이 패턴만은
# 숫자·낱말 검사를 건너뛰고 지운다. 사실이 실릴 자리가 아니기 때문이다.
_STRONG_GREETING = re.compile(
    r"^\s*(?:"
    r"안녕하[세십][요니]까?[^.\n]*|"
    r"[^.\n]*제\s*\d+\s*대\s*[^.\n]*(?:학생회|비상대책위원회)[^.\n]*입니다[^.\n]*|"
    r"[^.\n]*학생회\s*[\'\u2018\u201c\"][^\'\u2019\u201d\"\n]{1,20}[\'\u2019\u201d\"]\s*입니다[^.\n]*"
    r")\s*[.!~]*\s*$"
)

# "안내드립니다", "공지드립니다"처럼 본문을 여는 말은 지우지 않는다. 그 줄에 "간식행사"
# 같은 주제어가 같이 들어 있어서, 지우면 BM25가 찾아야 할 낱말까지 사라진다.
_GREETING_LINE = re.compile(
    r"^\s*(?:"
    r"안녕하세요[^.\n]*|"
    r"[^.\n]{0,20}학생회(?:입니다|장입니다)[^.\n]*|"
    r"반갑습니다[^.\n]*|"
    r"감사합니다[^.\n]*|"
    r"고맙습니다[^.\n]*|"
    r"많은\s*(?:참여|관심|이용|신청)[^.\n]*|"
    r"좋은\s*(?:하루|한\s*주|학기)[^.\n]*"
    r")\s*[.!~]*\s*$"
)

# 문장 단위로 섞여 있는 경우. 줄 안에서 그 문장만 지운다.
_GREETING_SENTENCE = re.compile(
    r"(?:^|(?<=[.!?]\s))\s*(?:"
    r"안녕하[세십][요니]까?[^.!?\n]*|"
    r"반갑습니다|"
    r"감사합니다|"
    r"고맙습니다|"
    r"많은\s*(?:참여|관심|이용|신청)[^.!?\n]*"
    r")[.!?~]*\s*"
)

# 이모지·구분선만 있는 장식 줄.
_DECOR_LINE = re.compile(r"^\s*(?:[-=~*_·ㆍ■□●○▶▷☆★]{2,}|[^\w\s]{1,4})\s*$")


def _keep(line: str) -> bool:
    """사실이 실려 있을 만한 줄인지. 그렇다면 인사말 패턴에 걸려도 남긴다."""
    return bool(_HAS_DIGIT.search(line)) or any(w in line for w in _KEEP_WORDS)


def _strip_sentences(line: str) -> str:
    """줄 안에 섞인 인사말 문장만 지운다.

    판단은 줄이 아니라 문장 단위로 한다. "수령 시 학생증을 보여주세요. 많은 참여
    바랍니다."처럼 사실과 맺음말이 한 줄에 붙어 있을 때, 줄 단위로 보면 "수령"
    때문에 맺음말까지 남는다.
    """
    return _GREETING_SENTENCE.sub(lambda m: "" if not _keep(m.group(0)) else m.group(0), line)


def normalize(text: str) -> str:
    """인사말·맺음말·장식 줄을 뺀 색인용 텍스트를 돌려준다."""
    kept = []
    for line in text.split("\n"):
        if _DECOR_LINE.match(line):
            continue
        if _STRONG_GREETING.match(line):
            continue
        if _GREETING_LINE.match(line) and not _keep(line):
            continue
        line = _strip_sentences(line)
        kept.append(line.rstrip())

    # 줄을 지우면서 생긴 빈 줄 연속을 하나로 줄인다.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def stats(text: str) -> dict:
    """정규화 전후 길이를 비교한다. 2주차 BM25 전후 점수 비교의 근거로 쓴다."""
    after = normalize(text)
    before_len, after_len = len(text), len(after)
    removed = before_len - after_len
    return {
        "before": before_len,
        "after": after_len,
        "removed": removed,
        "removed_ratio": round(removed / before_len, 3) if before_len else 0.0,
    }
