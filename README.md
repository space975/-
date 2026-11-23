# 우주수니 AI 매니저 (Render)

FastAPI 기반 개인용 웹앱입니다. OpenAI Responses API 대화, Naver Search, Naver DataLab, Naver Calendar OAuth/일정 등록을 제공합니다.

## Render 배포
1. 이 폴더 전체를 새 GitHub 저장소에 업로드합니다.
2. Render에서 New > Blueprint로 저장소를 연결하고 `render.yaml`을 선택합니다.
3. 환경변수 입력 화면에서 `OPENAI_API_KEY`, `APP_PASSWORD`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `BASE_URL`, `NAVER_REDIRECT_URI`를 입력합니다.
4. 유료 Starter 인스턴스 + 1GB 영구 디스크로 배포합니다. 무료 인스턴스는 sleep 및 영구 디스크 제약이 있어 OAuth 토큰 저장용 운영 환경으로 권장하지 않습니다.
5. Render가 서비스 URL을 발급하면 `BASE_URL=https://서비스주소`와 `NAVER_REDIRECT_URI=https://서비스주소/oauth/naver/callback`을 맞춥니다. 환경변수를 바꾼 뒤 재배포합니다.
6. 네이버 개발자센터 앱의 네이버 로그인 Callback URL에 같은 callback URL을 등록하고 캘린더 API 권한/서비스 환경을 확인합니다.
7. 앱 주소에 접속해 `APP_PASSWORD`로 로그인한 뒤 네이버 캘린더 연결을 누르고 동의합니다.

## 환경변수
- `OPENAI_API_KEY`: OpenAI Platform에서 발급한 API 키 (ChatGPT 구독과 별도 과금)
- `OPENAI_MODEL`: 기본 `gpt-4.1-mini`
- `APP_PASSWORD`: 웹앱 로그인 비밀번호
- `SESSION_SECRET`: 쿠키 서명용 장기 난수
- `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: 네이버 API 앱 키
- `BASE_URL`, `NAVER_REDIRECT_URI`: Render 공개 HTTPS 주소
- `TOKEN_DB_PATH`: 영구 디스크 내 SQLite 토큰 DB 경로

## 로컬 실행
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```
`.env`를 실제 값으로 채우고 로컬에서 `COOKIE_SECURE=false`로 설정하세요.

## 보안/범위
- API 키는 브라우저에 전달하지 않고 서버 환경변수에서만 사용합니다.
- 앱은 단일 비밀번호 로그인 방식의 개인용 MVP입니다. 공개 사용자 서비스로 확장하려면 계정별 인증/권한 및 사용자별 OAuth 토큰 저장소로 교체해야 합니다.
- 캘린더 일정 생성은 AI 도구가 호출할 수 있으나, GPT 지침상 사용자 명시 요청이 있을 때만 실행합니다.
- Naver Search/DataLab 호출은 기존 개발자센터 키 기반입니다. 사용자의 앱에서 현재 키 호출이 승인되는지 배포 후 실제 확인해야 합니다. NAVER API HUB 이관 완료 여부에 따라 별도 API 어댑터가 필요할 수 있습니다.
- OpenAI API 사용료는 ChatGPT 구독과 별도입니다.
