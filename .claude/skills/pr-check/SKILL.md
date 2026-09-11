---
name: pr-check
description: 현재 브랜치에 올라온 PR의 코드리뷰 코멘트를 중요도 순으로 번호 매겨 간략한 수정 방안과 함께 나열하는 스킬. /pr-check로 호출한다.
---

# PR 리뷰 코멘트 정리 워크플로우

현재 브랜치의 PR에 달린 코드리뷰 코멘트를 수집해 **중요도 순으로 번호를 매기고 간략한 수정 방안**을 정리한다.

> **모든 조회는 `gh api`(REST)로만 한다.** `gh pr view --json`은 내부적으로 GitHub GraphQL API를 호출하는데, 팀 저장소에서 GraphQL 오류가 난 적이 있어 사용하지 않는다.
> **`gh api`에는 `-r`(raw) 플래그가 없다.** `--jq` 결과가 문자열이면 이미 raw로 출력되므로 `-r`을 붙이지 않는다(붙이면 `unknown shorthand flag: 'r'`).

## Step 0 — 저장소·브랜치 정보 확보

이후 모든 REST 호출에 쓸 `{owner}/{repo}`와 현재 브랜치명을 확보한다.

```bash
gh repo view --json nameWithOwner --jq .nameWithOwner   # → {owner}/{repo}
git rev-parse --abbrev-ref HEAD                          # → {branch}
```

## Step 1 — 현재 브랜치의 PR 확인

현재 브랜치를 head로 하는 열린 PR을 REST로 조회한다.
**`head={owner}:{branch}` 필터는 쓰지 않는다** — 브랜치명에 `#`(이슈번호) 같은 특수문자가 있으면 URL이 잘려 빈 결과가 나온다. 이 저장소의 브랜치 규칙(`{type}/#{이슈번호}-{작업내용}`)이 정확히 여기 해당한다. 대신 열린 PR을 모두 받아 jq로 `.head.ref`를 직접 매칭한다.

```bash
gh api "repos/{owner}/{repo}/pulls?state=open&per_page=100" \
  --jq '.[] | select(.head.ref=="{branch}") | {number, title, html_url}'
```

- 결과가 비어 있으면(PR 없음) 그 사실만 알리고 종료한다.
- PR이 있으면 `number`를 확보해 다음 단계로 진행한다.

## Step 2 — 리뷰 코멘트 수집

세 종류의 코멘트를 모두 REST로 모은다. 한 종류라도 빠뜨리지 않는다.
**본문(`body`)을 통째로 뽑지 않는다** — CodeRabbit 등 봇 리뷰는 한 코멘트가 100KB를 넘기도 한다(`<details>`, HTML 주석, "Prompt for AI Agents", `consolidated_sites` 블록). 아래처럼 `<details>` 이전만 취하고 굵은 제목(`**...**`) + 심각도 줄만 축약해서 뽑는다.
**봇 본문은 빈 줄·HTML 주석(`<!-- -->`)으로 시작하는 경우가 많으므로 `split("\n")[0]`처럼 첫 줄을 그대로 쓰지 않는다** — 빈 줄·주석 줄을 건너뛴 첫 줄을 잡고, 심각도는 줄 위치와 무관하게 키워드로 따로 찾는다.

```bash
# (1) 코드 라인에 달린 인라인 리뷰 코멘트 (핵심)
gh api "repos/{owner}/{repo}/pulls/{번호}/comments" --paginate \
  --jq '.[] | "\(.path):\(.line // "outdated") @\(.user.login)\n  " +
        (.body | split("<details>")[0]
         | ((capture("\\*\\*(?<h>[^*\\n]+)\\*\\*").h
             // ([split("\n")[] | select(test("\\S") and (test("^\\s*<!--")|not))][0] // "")[0:200])
            + ((capture("(?<s>[^\\n]*(Critical|Major|Minor|Trivial)[^\\n]*)").s | " | " + .) // "")))'

# (2) 리뷰 요약 코멘트 (Approve/Request changes 시 본문)
gh api "repos/{owner}/{repo}/pulls/{번호}/reviews" --paginate \
  --jq '.[] | select(.body != "") | "@\(.user.login) [\(.state)] " +
        ([.body | split("<details>")[0] | split("\n")[] | select(test("\\S") and (test("^\\s*<!--")|not))][0] // "")'

# (3) PR 전체에 달린 일반 코멘트 (issue 코멘트)
gh api "repos/{owner}/{repo}/issues/{번호}/comments" --paginate \
  --jq '.[] | "@\(.user.login): " +
        ([.body | split("<details>")[0] | split("\n")[] | select(test("\\S") and (test("^\\s*<!--")|not))][0] // "")'
```

- 코멘트가 하나도 없으면 그 사실을 알리고 종료한다.
- **outdated 감지**: 인라인 코멘트의 `line`(과 `position`)이 `null`이면 코드가 밀려난 outdated 코멘트다. `[outdated]`로 표기한다.
- **resolved 여부는 REST로 알 수 없다**(스레드 `isResolved`는 GraphQL 전용). resolved 필터링은 하지 않으며, outdated만 위 방식으로 구분한다.
- 봇 리뷰가 **한 코멘트로 여러 파일을 묶어**(`consolidated_sites`) 지적하기도 한다. 같은 뿌리의 지적은 번호 하나로 묶고 관련 파일을 함께 적는다.

## Step 3 — 중요도 판정 및 정렬

각 코멘트를 아래 기준으로 분류하고 높은 순으로 정렬한다.

**리뷰어가 심각도를 이미 명시했으면(특히 CodeRabbit의 `🟠 Major / 🟡 Minor / 🔵 Trivial`) 그것을 1차 기준으로 매핑하고**, 아래 rubric은 보정용으로만 쓴다. `Major → High, Minor → Medium, Trivial → Low`를 출발점으로 하되, 보안·데이터 정합성이면 한 단계 올린다.

| 중요도 | 판단 기준 |
|--------|-----------|
| 🔴 High | 버그·장애·보안·데이터 정합성, 컨벤션 위반으로 인한 구조적 문제, 반드시 고쳐야 머지 가능한 것 |
| 🟡 Medium | 로직 개선, 예외 처리 누락, 네이밍·모듈 경계 위반 등 고치는 게 바람직한 것 |
| 🟢 Low | 사소한 스타일, 오타, 취향/제안 수준 |

- 리뷰어가 "must/반드시/blocking" 등을 명시했거나 `Request changes` 리뷰면 최소 Medium 이상으로 본다.
- 이 저장소 고유의 High 후보: 데이터 분류 위반(실시간·개인별 데이터를 인덱싱), 학사 정보를 단정, 프롬프트를 코드에 하드코딩, API 키 노출.
- 애매하면 코멘트가 가리키는 코드를 직접 열어 맥락을 확인한 뒤 판정한다.

## Step 4 — 결과 출력

번호를 매겨 아래 형식으로 나열한다. 번호는 중요도 높은 순서다.

```
## PR #{번호} 리뷰 코멘트 ({총 N개})

1. 🔴 [파일:라인] 코멘트 요약
   - 리뷰어: @{user}
   - 수정 방안: {한두 문장의 구체적 방안}

2. 🟡 [파일:라인] ...
   - 리뷰어: @{user}
   - 수정 방안: ...
```

- **수정 방안은 간략하게** — 한두 문장으로 무엇을 어떻게 바꿀지 제시한다. 실제 코드 수정은 이 스킬의 범위가 아니다. (사용자가 요청하면 그때 진행)
- 위치 표기: 인라인은 `[파일:라인]`, `line`이 null이면 `[파일:outdated]`, 요약/일반 코멘트는 `[전체]`로 표기한다.
