# AI 서버 배포

**Coolify가 이 저장소의 `Dockerfile`로 직접 빌드한다.** GitHub Actions나 이미지 저장소(GHCR)를 거치지 않는다.
로컬에서 도커로 띄우는 방법은 `docker.md`를 본다.

```
main 머지 → Coolify가 변경을 감지(Auto Deploy) → main을 받아 Dockerfile로 빌드 → 컨테이너 교체
```

- **Coolify**: 우리 서버에 설치해 쓰는 배포 도구. 백엔드도 여기서 돈다
- 배포 설정은 저장소에 파일로 두지 않는다. Coolify 화면이 전부다

## 처음 한 번 해야 하는 것

Coolify 관리자 권한이 필요하다. Coolify 담당(수민)과 같이 한다. 2026-11 기준 아래대로 설정되어 있다.

### 1. Coolify에 리소스 만들기

| 항목 | 값 | 이유 |
| --- | --- | --- |
| 종류 | 저장소 + Dockerfile | Coolify가 저장소를 받아 직접 빌드한다 |
| 브랜치 | `main` | |
| Auto Deploy | 켬 | main에 머지되면 자동으로 다시 빌드한다 |
| 서버·네트워크 | **백엔드와 같은 서버, 같은 도커 네트워크** | 백엔드가 내부 주소로 부르게 한다 |
| 도메인 | **비워 둔다** | 도메인을 붙이면 인터넷에 열린다 |
| 포트 매핑 | **비워 둔다** | 같은 이유. 백엔드는 내부 네트워크로 8000에 붙는다 |
| 헬스체크 | `GET /health` → 200 | 인증 없이 열려 있는 유일한 주소다 |

AI 서버는 인터넷에 직접 노출하지 않는다(AGENTS.md 아키텍처). 열려 있으면 누구나 우리 LLM 비용으로
질문할 수 있다. 헤더 검증(`INTERNAL_TOKEN`)이 두 번째 방어선이고, 노출하지 않는 것이 첫 번째다.

### 2. 환경변수

Coolify 리소스의 Environment Variables에 넣는다. 저장소에는 올리지 않는다.

| 이름 | 값 |
| --- | --- |
| `ENV` | `prod` (데모 페이지가 닫힌다) |
| `INTERNAL_TOKEN` | 새로 만든 값. 백엔드의 `STREAM_AI_INTERNAL_TOKEN`과 **같은 값** |
| `LLM_MODEL` | `gpt-oss-120b` (모델 선정 근거: `eval/model-comparison.md`) |
| `KMU_AI_API_KEY` | 학교 AI 게이트웨이 키. **실험용과 다른 운영 전용 키**를 쓴다 |

토큰 만들기: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

`ENV=prod`인데 `INTERNAL_TOKEN`이 비어 있으면 서버가 뜨지 않는다. `LLM_MODEL`이나 `KMU_AI_API_KEY`가 없어도 뜨지 않는다.
일부러 그렇게 만들었다. 서버가 떠 있는데 질문마다 실패하는 것보다 원인을 찾기 쉽다.

**main에 머지하면 바로 배포된다.** 새 환경변수가 필요한 변경은 환경변수를 먼저 넣고 머지한다.
순서가 바뀌면 새 코드가 값을 못 찾아 컨테이너가 뜨지 않고, 그동안 챗봇이 멈춘다.

운영 키를 실험용과 나누는 이유: 실험에서 크레딧을 다 쓰거나 키를 다시 만들어도 실제 챗봇이 멈추지 않게 하려는 것이다.

예전 Gemini 설정(`GEMINI_MODEL`, `GOOGLE_*`, 서비스 계정 파일)은 서버가 더 이상 읽지 않는다(#65).
#65를 되돌려 Gemini로 돌아갈 때 필요하므로, 교체가 자리 잡을 때까지는 지우지 않고 둔다.

### 3. 게이트웨이 접속 확인

AI 서버는 학교 AI 게이트웨이(`ai.cs.kookmin.ac.kr`)를 불러 답을 만든다. Coolify 서버에서 그 주소로 나갈 수 있어야 한다.
Coolify 서버 터미널에서 확인한다.

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://ai.cs.kookmin.ac.kr/v1/models
```

`401`이 나오면 연결은 된다(키를 안 실어서 거절된 것). 시간 초과나 연결 실패가 나오면 학교 쪽 방화벽 등으로 막혀 있는 것이다.

### 4. 백엔드 설정

백엔드 리소스의 환경변수를 바꾼다.

| 이름 | 값 |
| --- | --- |
| `STREAM_AI_BASE_URL` | `http://<Coolify가 정한 AI 서버 컨테이너 이름>:8000` |
| `STREAM_AI_INTERNAL_TOKEN` | 2번의 `INTERNAL_TOKEN`과 같은 값 |

## 배포 후 확인

1. Coolify 로그에 `Uvicorn running on http://0.0.0.0:8000`이 보인다
2. 헬스체크가 초록색이다
3. **밖에서 안 열린다.** 서버 공인 IP로 `curl http://<IP>:8000/health`가 연결되지 않아야 한다
4. 앱에서 챗봇에 질문해서 답이 **조각으로** 온다. 한 번에 오면 중간 프록시가 모아두고 있는 것이다
5. 토큰을 일부러 틀리게 하면 백엔드 로그에 `AI 서버가 오류를 돌려줬습니다. status=401`이 찍힌다

## 막히는 곳

| 증상 | 원인 |
| --- | --- |
| 컨테이너가 바로 죽음, 로그에 `INTERNAL_TOKEN이 없습니다` | 2번 환경변수 누락 |
| 컨테이너가 바로 죽음, 로그에 `LLM_MODEL 또는 KMU_AI_API_KEY가 없습니다` | 2번 환경변수 누락 |
| 질문하면 `error` 이벤트, 로그에 `401 Unauthorized` (`ai.cs.kookmin.ac.kr`) | 게이트웨이 키가 틀렸거나 막혔다. 값 끝에 다른 글자가 붙지 않았는지, 게이트웨이 관리 화면에서 키 상태·남은 크레딧을 본다 |
| 질문하면 `error` 이벤트, 로그에 `ConnectError` 또는 시간 초과 | 3번. Coolify 서버에서 게이트웨이로 나가지 못한다 |
| 백엔드 로그에 `status=401` | 양쪽 토큰이 다르다 |
| 백엔드 로그에 연결 실패 | 같은 네트워크가 아니거나 `STREAM_AI_BASE_URL`의 컨테이너 이름이 틀렸다 |
| Coolify 빌드 실패 | Coolify 빌드 로그를 본다. 로컬에서 `docker build .`가 되는지 먼저 확인한다 |

## 되돌리기

Coolify의 Deployments 화면에서 이전 배포를 골라 다시 띄운다.
코드로 되돌릴 때는 문제 커밋을 되돌리는(revert) PR을 main에 머지하면 자동으로 다시 빌드된다.
