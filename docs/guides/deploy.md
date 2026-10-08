# AI 서버 배포

백엔드(stream-server)와 같은 방식으로 올린다. **GHCR 이미지 + Coolify**다.
로컬에서 도커로 띄우는 방법은 `docker.md`를 본다.

```
main 머지 → GitHub Actions가 이미지 빌드 → GHCR에 올림 → Coolify에 재배포 요청 → 컨테이너 교체
```

- **GHCR**: GitHub이 제공하는 도커 이미지 저장소. `ghcr.io/billilge/stream-ai:dev`로 올라간다
- **Coolify**: 우리 서버에 설치해 쓰는 배포 도구. 백엔드도 여기서 돈다
- 워크플로: `.github/workflows/deploy-dev.yml`

## 처음 한 번 해야 하는 것

Coolify 관리자 권한이 필요하다. 백엔드 담당(서준)과 같이 한다.

### 1. Coolify에 리소스 만들기

| 항목 | 값 | 이유 |
| --- | --- | --- |
| 종류 | Docker Image | 이미지는 GitHub Actions가 만든다. Coolify가 빌드하지 않는다 |
| 이미지 | `ghcr.io/billilge/stream-ai:dev` | |
| 서버·네트워크 | **백엔드와 같은 서버, 같은 도커 네트워크** | 백엔드가 내부 주소로 부르게 한다 |
| 도메인 | **비워 둔다** | 도메인을 붙이면 인터넷에 열린다 |
| 포트 매핑 | **비워 둔다** | 같은 이유. 백엔드는 내부 네트워크로 8000에 붙는다 |
| 헬스체크 | `GET /health` → 200 | 인증 없이 열려 있는 유일한 주소다 |

AI 서버는 인터넷에 직접 노출하지 않는다(AGENTS.md 아키텍처). 열려 있으면 누구나 우리 Gemini 비용으로
질문할 수 있다. 헤더 검증(`INTERNAL_TOKEN`)이 두 번째 방어선이고, 노출하지 않는 것이 첫 번째다.

### 2. 환경변수

Coolify 리소스의 Environment Variables에 넣는다. 저장소에는 올리지 않는다.

| 이름 | 값 |
| --- | --- |
| `ENV` | `prod` (데모 페이지가 닫힌다) |
| `INTERNAL_TOKEN` | 새로 만든 값. 백엔드의 `STREAM_AI_INTERNAL_TOKEN`과 **같은 값** |
| `GEMINI_MODEL` | `.env.example`과 같은 모델 |
| `GOOGLE_GENAI_USE_VERTEXAI` | `True` |
| `GOOGLE_CLOUD_PROJECT` | `my-project-kmualpha` |
| `GOOGLE_CLOUD_LOCATION` | `global` |
| `GOOGLE_APPLICATION_CREDENTIALS` | 3번에서 B를 고른 경우만. `/run/secrets/gcp-sa.json` |

토큰 만들기: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

`ENV=prod`인데 `INTERNAL_TOKEN`이 비어 있으면 서버가 뜨지 않는다. 일부러 그렇게 만들었다.

### 3. Vertex 인증

로컬은 맥북의 gcloud 로그인 파일(ADC)을 쓰지만 서버에는 그 파일이 없다. 서버가 어디 있느냐에 따라 갈린다.

| Coolify 서버 위치 | 방법 |
| --- | --- |
| **A. 같은 GCP 프로젝트의 VM** | VM에 붙은 서비스 계정을 SDK가 자동으로 쓴다. 파일이 필요 없다. 그 계정에 `Vertex AI 사용자` 역할만 준다 |
| **B. 그 밖의 서버** | AI 서버 전용 서비스 계정을 만들고(`Vertex AI 사용자` 역할만), 키 JSON을 Coolify의 파일 마운트(Storages)로 `/run/secrets/gcp-sa.json`에 넣는다 |

B에서 키 파일을 **이미지나 저장소에 넣지 않는다.** 그 파일이 곧 비밀번호다(AGENTS.md 금지 사항).
역할을 `Vertex AI 사용자` 하나로 좁히는 이유는, 파일이 새도 Gemini 호출 말고는 아무것도 못 하게 하기 위해서다.

### 4. GitHub Secrets

stream-ai 저장소 Settings → Secrets and variables → Actions에 넣는다. 백엔드 저장소에 같은 이름으로 있다.

| 이름 | 값 |
| --- | --- |
| `COOLIFY_URL` | 백엔드와 같다 |
| `COOLIFY_TOKEN` | 백엔드와 같다 |
| `COOLIFY_DEV_UUID` | 1번에서 만든 리소스의 UUID |

GHCR 이미지가 비공개라면 Coolify 서버가 `ghcr.io`에 로그인돼 있어야 받아 갈 수 있다.
백엔드 이미지를 이미 받고 있으므로 같은 조직(billilge)이면 그대로 될 가능성이 높다. 안 되면 여기부터 본다.

### 5. 백엔드 설정

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
| 컨테이너가 바로 죽음, 로그에 `GEMINI_MODEL이 없습니다` | 2번 환경변수 누락 |
| 질문하면 `error` 이벤트, 로그에 403 또는 `DefaultCredentialsError` | 3번 인증 문제. B라면 파일 경로와 `GOOGLE_APPLICATION_CREDENTIALS`가 맞는지 본다 |
| 백엔드 로그에 `status=401` | 양쪽 토큰이 다르다 |
| 백엔드 로그에 연결 실패 | 같은 네트워크가 아니거나 `STREAM_AI_BASE_URL`의 컨테이너 이름이 틀렸다 |
| Actions의 Deploy 단계 실패 | 4번 Secrets 누락 또는 UUID가 틀렸다 |

## 되돌리기

이미지는 커밋마다 `dev-<짧은 커밋 해시>` 태그로도 남는다. Coolify 이미지 태그를 그 값으로 바꾸고 재배포한다.
