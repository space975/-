# 우주수니 AI 매니저 (Render 무료 우선 구성)

FastAPI 기반 개인용 웹앱입니다. OpenAI Responses API 대화, Naver Search, Naver DataLab, Naver Calendar OAuth/일정 등록을 제공합니다.

## 무료 배포 구성

- Render Free 웹 서비스: 요청이 없으면 잠들 수 있으며 첫 접속이 느릴 수 있습니다.
- Supabase Free PostgreSQL: 네이버 OAuth 상태와 토큰을 저장합니다.
- Render의 `DATABASE_URL` 환경변수에는 Supabase의 Session pooler 연결 문자열을 넣습니다.
- Supabase Free 프로젝트는 DB 활동이 적으면 7일 후 일시 중지될 수 있습니다. 재개하면 데이터와 설정은 복구됩니다.
- OpenAI API는 ChatGPT 구독과 별도 과금입니다. 네이버 API는 앱 키의 승인 범위와 제공 한도에 따릅니다.

## 1. Supabase 무료 데이터베이스 만들기

1. https://supabase.com 에서 가입하고 새 프로젝트를 만듭니다. 유료 옵션은 선택하지 않습니다.
2. 프로젝트가 준비되면 **Connect**를 누릅니다.
3. **Session pooler** 연결 방식을 선택하고 연결 문자열을 복사합니다.
4. 연결 문자열의 비밀번호 자리에는 프로젝트 생성 시 설정한 DB 비밀번호가 들어가 있어야 합니다. 비밀번호에 특수문자가 있으면 URL 인코딩이 필요할 수 있습니다.
5. 이 연결 문자열은 외부에 공유하지 말고 Render의 환경변수에만 입력합니다.

앱이 최초 실행될 때 필요한 `oauth_states`, `naver_tokens` 테이블을 자동으로 생성합니다.

## 2. Render 무료 배포

1. Render에서 **New > Blueprint**를 선택합니다.
2. GitHub 저장소 `space975/-`를 연결하고 `render.yaml`을 사용합니다.
3. 환경변수 입력 화면에서 아래 값을 입력합니다.
   - `OPENAI_API_KEY`: OpenAI Platform API 키
   - `APP_PASSWORD`: 웹앱 로그인 비밀번호
   - `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: 네이버 개발자센터 앱 키
   - `DATABASE_URL`: Supabase Session pooler 연결 문자열
   - `BASE_URL`: 배포 후 Render가 발급한 HTTPS 주소
   - `NAVER_REDIRECT_URI`: `https://서비스주소/oauth/naver/callback`
4. `SESSION_SECRET`와 `GPT_ACTION_API_KEY`는 Blueprint가 자동 생성합니다.
5. 배포가 끝나면 `BASE_URL`과 `NAVER_REDIRECT_URI`를 실제 Render 주소에 맞게 확인하고 필요하면 수정 후 재배포합니다.

## 3. 네이버 로그인 설정

네이버 개발자센터 앱의 네이버 로그인 Callback URL에 아래 주소를 등록합니다.

`https://서비스주소/oauth/naver/callback`

앱에서 로그인한 뒤 **네이버 캘린더 연결하기**를 눌러 권한 동의를 진행합니다.

## 환경변수

- `OPENAI_API_KEY`: OpenAI Platform에서 발급한 API 키
- `OPENAI_MODEL`: 기본 `gpt-4.1-mini`
- `APP_PASSWORD`: 웹앱 로그인 비밀번호
- `SESSION_SECRET`: 쿠키 서명용 난수 (Render가 자동 생성)
- `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: 네이버 API 앱 키
- `BASE_URL`, `NAVER_REDIRECT_URI`: Render 공개 HTTPS 주소
- `DATABASE_URL`: Supabase PostgreSQL Session pooler 연결 문자열
- `GPT_ACTION_API_KEY`: GPT Actions 인증 키 (Render가 자동 생성)

## 로컬 실행

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

로컬 테스트에서는 `.env`에 `COOKIE_SECURE=false`를 설정할 수 있습니다. `DATABASE_URL`이 비어 있으면 로컬 SQLite를 사용합니다.

## 보안/범위

- API 키와 DB 연결 문자열은 브라우저에 전달하지 않고 서버 환경변수에서만 사용합니다.
- GitHub 저장소는 비공개(Private)로 설정하는 것을 권장합니다.
- 앱은 단일 비밀번호 로그인 방식의 개인용 MVP입니다.
- 네이버 검색/DataLab API 사용 승인 여부와 캘린더 권한은 실제 배포 후 확인해야 합니다.
- OpenAI API 사용료는 ChatGPT 구독과 별도입니다.
