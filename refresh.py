"""검색 인덱스를 서버 안에서 주기적으로 다시 만든다.

왜 필요한가: 인덱스는 이미지를 빌드할 때 한 번 만들어진다(`Dockerfile`의 `build_index.py`).
서버는 그걸 메모리에 들고 있어서, 학생회가 공지를 올려도 재배포하기 전에는 챗봇이 모른다.
그래서 서버가 켜져 있는 동안 10분마다 백엔드에서 다시 받아 인덱스를 바꿔 끼운다.
**이미지를 다시 만드는 게 아니다.** 컨테이너 안의 `index/` 폴더에 파일만 새로 쓴다.

한 번 갱신하는 순서 (AGENTS.md "인덱스 갱신"):
    1. 동기화    sync.py가 백엔드 내부 API에서 공지·행사·아카이빙·열린피드백을 받는다
    2. 빌드      시드 문서·FAQ와 합쳐 새 폴더(index/build_YYYYMMDD_HHMMSS/)에 인덱스를 만든다
    3. 검사      받은 문서가 0건이거나 직전보다 절반 아래로 줄면 바꾸지 않는다
    4. 교체      검색이 새 인덱스를 쓰게 하고, index/current 링크를 새 폴더로 바꾼다
    5. 정리      이전 빌드는 몇 개만 남기고 지운다

어느 단계에서든 실패하면 기존 인덱스를 그대로 쓴다. 백엔드가 잠깐 죽어도 챗봇은
마지막으로 성공한 인덱스로 계속 답한다.

컨테이너 안 파일이라 재배포·재시작하면 사라진다. 그래서 서버가 켜지자마자 한 번 바로 돌고,
그 전 몇 초 동안은 이미지에 든 인덱스(시드 문서)로 답한다.
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import build_index
import search
import sync

logger = logging.getLogger(__name__)

INDEX_ROOT = "index"
CURRENT_LINK = os.path.join(INDEX_ROOT, "current")
BUILD_PREFIX = "build_"

# 몇 초마다 다시 만들지. 공지가 수백 건이라 한 번에 몇 초면 끝난다.
INTERVAL_SECONDS = int(os.getenv("INDEX_REFRESH_SECONDS", "600"))

# 이전 빌드를 몇 개 남길지(AGENTS.md: 2~3개). 문제가 생겼을 때 직전 인덱스와 비교해 볼 수 있다.
KEEP_BUILDS = 3

# 받은 문서가 직전의 이 비율 아래로 줄면 이상하다고 보고 바꾸지 않는다.
# 백엔드 오류로 일부만 오는 경우를 막는다. 정말로 공지를 많이 지웠다면 경고 로그를 보고 사람이 판단한다.
MIN_RATIO = 0.5

# 직전에 받은 문서 수. 서버가 켜진 뒤 첫 갱신에는 비교할 값이 없어 0건만 아니면 받아들인다.
_last_synced_count: int | None = None


def refresh_once() -> bool:
    """한 번 갱신한다. 새 인덱스로 바꿨으면 True, 검사에 걸려 그대로 뒀으면 False.

    동기화·빌드·읽기에서 난 예외는 밖으로 던진다. 부르는 쪽(`run_forever`)이 로그만 남기고
    기존 인덱스를 그대로 둔다.
    """
    global _last_synced_count

    synced = sync.fetch_documents()
    if not synced:
        logger.warning("인덱스 갱신 건너뜀: 백엔드에서 받은 문서가 0건입니다")
        return False
    if _last_synced_count and len(synced) < _last_synced_count * MIN_RATIO:
        logger.warning("인덱스 갱신 건너뜀: 받은 문서가 %d건 → %d건으로 크게 줄었습니다",
                       _last_synced_count, len(synced))
        return False

    # 시드 문서·FAQ는 학생회가 올리는 글이 아니라 우리가 직접 쓴 고정 안내라 매번 같이 넣는다.
    # 예전 가짜 데이터(test_data의 공지·행사)는 넣지 않는다. 백엔드 실데이터가 그 자리를 대신한다.
    docs = build_index.load_seed() + build_index.load_faq() + synced
    index = build_index.build(docs)

    build_dir = os.path.join(INDEX_ROOT, BUILD_PREFIX + datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S"))
    os.makedirs(build_dir, exist_ok=True)
    path = os.path.join(build_dir, "bm25.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    # 검색이 새 인덱스를 쓰게 한다. 파일을 다 읽은 뒤에 바뀌므로 질문 처리 중에 반쯤 바뀌는 일은 없다.
    search.use_index(path)
    _point_current(build_dir)
    _remove_old_builds()

    _last_synced_count = len(synced)
    logger.info("인덱스 갱신: 문서 %d건 → 청크 %d개 (%s)", index["doc_count"], index["chunk_count"], build_dir)
    return True


def _point_current(build_dir: str) -> None:
    """index/current 링크를 새 빌드 폴더로 바꾼다.

    임시 이름으로 링크를 먼저 만들고 `os.replace`로 한 번에 덮어쓴다. 기존 링크를 지우고 새로 만들면
    그 사이 아주 잠깐 링크가 없는 순간이 생긴다. `os.replace`는 그런 순간이 없다(원자적 교체).
    """
    temp = CURRENT_LINK + ".tmp"
    if os.path.lexists(temp):
        os.remove(temp)
    # 링크 안에는 index/ 기준 상대 경로를 적는다. 폴더째 옮겨도 깨지지 않는다.
    os.symlink(os.path.basename(build_dir), temp)
    os.replace(temp, CURRENT_LINK)


def _remove_old_builds() -> None:
    """가장 최근 KEEP_BUILDS개만 남기고 이전 빌드 폴더를 지운다. 폴더 이름이 시각이라 이름순이 곧 시간순이다."""
    builds = sorted(
        name for name in os.listdir(INDEX_ROOT)
        if name.startswith(BUILD_PREFIX) and os.path.isdir(os.path.join(INDEX_ROOT, name))
    )
    for name in builds[:-KEEP_BUILDS]:
        shutil.rmtree(os.path.join(INDEX_ROOT, name), ignore_errors=True)


async def run_forever() -> None:
    """서버가 켜져 있는 동안 INTERVAL_SECONDS마다 갱신한다. `app.py`가 서버 시작 때 띄운다.

    갱신 자체는 파일을 쓰고 계산하는 일이라 별도 스레드(`asyncio.to_thread`)에서 돌린다.
    그래야 갱신하는 몇 초 동안에도 학생 질문이 막히지 않는다.
    """
    logger.info("인덱스 주기 갱신 시작: %d초마다", INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.to_thread(refresh_once)
        except Exception:
            # 상세 원인은 로그에만 남기고 다음 주기에 다시 시도한다. 기존 인덱스는 그대로다.
            logger.exception("인덱스 갱신 실패. 기존 인덱스를 그대로 씁니다")
        await asyncio.sleep(INTERVAL_SECONDS)
