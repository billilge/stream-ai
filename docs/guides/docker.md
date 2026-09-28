# 도커로 AI 서버 실행하기

> 작성일: 2026-09-28
> 관련: `Dockerfile`, `.dockerignore`, 10주 로드맵 3주차

맥북이 아닌 곳에서도 AI 서버를 같게 실행하기 위한 것이다.
`Dockerfile`에 한 줄씩 이유를 주석으로 적어뒀으니 그쪽을 함께 본다.

## 용어

| 말 | 뜻 |
| --- | --- |
| 이미지 | 실행 환경을 묶어둔 설치 파일 같은 것. `docker build`로 만든다 |
| 컨테이너 | 이미지를 실제로 실행한 것. 하나의 이미지로 여러 개를 띄울 수 있다 |
| 층(layer) | `Dockerfile`의 명령 한 줄이 만드는 단위. 입력이 안 바뀌면 재사용된다 |
| 마운트 | 맥북의 파일·폴더를 컨테이너 안에서 보이게 연결하는 것. 복사가 아니다 |

## 빌드

```bash
docker build -t stream-ai .
```

- `-t stream-ai` — 이미지에 붙일 이름(tag)
- `.` — `Dockerfile`이 있는 폴더. 이 폴더 내용이 빌드에 쓰인다(`.dockerignore`가 뺀 것은 제외)

빌드 중 `python build_index.py`가 돌아 검색 인덱스를 이미지 안에 만든다.
`index/bm25.json`은 저장소에 커밋되지 않으므로 여기서 만들어야 한다.

## 실행

```bash
docker run -d --name stream-ai-test \
  -p 8001:8000 \
  --env-file .env \
  -e ENV=prod \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/home/appuser/.config/gcloud/application_default_credentials.json:ro" \
  stream-ai
```

| 옵션 | 뜻 |
| --- | --- |
| `-d` | 뒤에서 돌린다. 터미널을 붙잡지 않는다 |
| `--name` | 컨테이너에 이름을 준다. 없으면 도커가 임의로 지어서 나중에 찾기 번거롭다 |
| `-p 8001:8000` | 맥북의 8001을 컨테이너의 8000에 연결한다. **왼쪽이 맥북, 오른쪽이 컨테이너다** |
| `--env-file .env` | 설정값을 파일로 넘긴다. `.env`는 이미지에 들어 있지 않다 |
| `-e ENV=prod` | `.env`의 값을 덮어쓴다. `-e`가 `--env-file`보다 세다 |
| `-v A:B:ro` | 맥북의 파일 A를 컨테이너의 B 위치에서 보이게 한다. `ro`는 읽기 전용 |

로컬은 `8001`을 쓴다. 맥북에서 `uvicorn`을 직접 띄워 `8000`을 쓰고 있을 때 부딪히지 않게 하려는 것이다.

### 인증 파일을 왜 마운트하나

`app.py`의 `genai.Client()`는 인자가 없다. `google-genai` SDK가 환경변수를 보고 Vertex로 붙고,
로그인 정보는 `gcloud auth application-default login`이 맥북에 저장해둔 파일(ADC)에서 읽는다.

그 파일은 `~/.config/gcloud/application_default_credentials.json`에 있고 **컨테이너 안에는 없다.**
마운트하지 않으면 Vertex 403이 난다.

이미지에 복사해 넣으면 안 된다. 그 파일은 곧 비밀번호이고, 이미지를 가진 사람이 모두 쓸 수 있게 된다.
컨테이너 안 경로가 `/home/appuser/...`인 이유는 `Dockerfile`에서 `appuser`로 내려왔기 때문이다.
SDK는 그 계정의 홈 폴더에서 파일을 찾는다.

**이건 로컬 확인용이다.** 실제 배포에서는 이 방법을 쓰지 않는다. 4주차에 배포 환경의
서비스 계정으로 바꾼다. 그러면 파일 자체가 필요 없어진다.

## 확인

```bash
curl -s http://localhost:8001/health                 # {"status":"ok"}
curl -o /dev/null -w "%{http_code}\n" localhost:8001/  # 404 (ENV=prod면 데모 페이지가 닫힌다)
```

SSE가 조각으로 오는지 본다. `-N`이 중요하다 — curl이 응답을 모아두지 않게 하는 옵션이고,
없으면 스트리밍이 되고 있어도 한 번에 온 것처럼 보인다.

```bash
curl -N -X POST http://localhost:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","text":"빌릴게 어떻게 써요?"}]}'
```

2026-09-28 확인 결과: 첫 `delta` 1.38초, 전체 1.98초, `delta` 10개 · `bubble` 2개 · `done` 1개.

## 자주 볼 명령

```bash
docker logs -f stream-ai-test    # 로그 보기 (-f는 계속 따라가기)
docker ps                        # 돌고 있는 컨테이너
docker stop stream-ai-test       # 멈추기
docker rm -f stream-ai-test      # 지우기 (같은 이름으로 다시 띄우기 전에 필요)
docker exec -it stream-ai-test sh # 컨테이너 안에 들어가기
```

## 막히는 곳

| 증상 | 원인 |
| --- | --- |
| 브라우저가 연결 거부 | `--host 0.0.0.0`이 빠졌다. 기본값은 컨테이너 자기 자신만 받는다 |
| Vertex 403 | ADC 마운트가 빠졌거나 경로가 틀렸다. `docker logs`를 본다 |
| `docker logs`가 비어 있음 | `PYTHONUNBUFFERED=1`이 빠졌다. 파이썬이 로그를 모아두고 안 내보낸다 |
| `ZoneInfoNotFoundError` | slim 이미지에 시간대 표가 없다. `tzdata`를 설치해야 한다 |
| 이름이 이미 있다는 오류 | 멈춘 컨테이너가 남아 있다. `docker rm -f`로 지운다 |
| 코드를 고쳤는데 그대로 | 이미지를 다시 빌드해야 한다. 컨테이너는 만들 때의 이미지를 계속 쓴다 |
| `docker stop`이 10초 걸림 | `CMD`에 `exec`가 빠졌다. 종료 신호가 uvicorn까지 닿지 않는다 |

## 4주차에 바뀔 것

- 인증을 ADC 마운트에서 **배포 환경의 서비스 계정**으로 바꾼다
- `X-Internal-Token`, `X-User-Id` 검증을 붙인다
- 배포 후 SSE가 여전히 조각으로 오는지 다시 확인한다.
  중간에 끼는 프록시가 응답을 모아두면 스트리밍이 죽는다
