import os
import json
import secrets
import sqlite3
import urllib.parse
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

APP_TITLE = "Woojoo Naver Actions"
DB_PATH = os.getenv("TOKEN_DB_PATH", "naver_tokens.sqlite3")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
GPT_ACTION_API_KEY = os.getenv("GPT_ACTION_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
REDIRECT_URI = os.getenv("NAVER_REDIRECT_URI", "")

app = FastAPI(
    title=APP_TITLE,
    version="0.1.0",
    description="Woojoo Su-ni AI Manager: OpenAI chat plus Naver Search, DataLab, and Calendar."
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET or "unsafe-dev-secret-change-before-deploy", https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true", same_site="lax")

class DatabaseConnection:
    """Small compatibility wrapper for SQLite locally and PostgreSQL in hosted deployments."""
    def __init__(self, connection, postgres=False):
        self.connection = connection
        self.postgres = postgres

    def execute(self, sql, params=()):
        if self.postgres:
            if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO NAVER_TOKENS"):
                sql = """INSERT INTO naver_tokens
                    (id, access_token, refresh_token, expires_at, token_type)
                    VALUES (1, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                    access_token=EXCLUDED.access_token,
                    refresh_token=EXCLUDED.refresh_token,
                    expires_at=EXCLUDED.expires_at,
                    token_type=EXCLUDED.token_type"""
            else:
                sql = sql.replace("?", "%s")
        return self.connection.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()

def db():
    if DATABASE_URL:
        conn = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=10,
            sslmode="require",
        )
        return DatabaseConnection(conn, postgres=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DatabaseConnection(conn)

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS naver_tokens (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at TEXT,
            token_type TEXT DEFAULT 'Bearer'
        )""")

init_db()

def require_action_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if not GPT_ACTION_API_KEY:
        raise HTTPException(503, "Server setup incomplete: GPT_ACTION_API_KEY is not configured.")
    if not x_api_key or not secrets.compare_digest(x_api_key, GPT_ACTION_API_KEY):
        raise HTTPException(401, "Invalid action API key.")
    return True

def require_naver_config():
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise HTTPException(503, "NAVER_CLIENT_ID and NAVER_CLIENT_SECRET must be configured.")
    return True

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=100)
    category: str = Field(default="news", description="news, blog, webkr, cafearticle, kin")
    display: int = Field(default=10, ge=1, le=100)
    start: int = Field(default=1, ge=1, le=1000)
    sort: str = Field(default="sim", description="sim or date")

class KeywordGroup(BaseModel):
    groupName: str
    keywords: list[str] = Field(min_length=1, max_length=20)

class DataLabRequest(BaseModel):
    startDate: str
    endDate: str
    timeUnit: str = Field(default="date", description="date, week, or month")
    keywordGroups: list[KeywordGroup] = Field(min_length=1, max_length=5)
    device: Optional[str] = None
    gender: Optional[str] = None
    ages: Optional[list[str]] = None

class CalendarRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    start: str = Field(..., description="Local Korea time, ISO format e.g. 2026-11-24T14:00:00")
    end: str = Field(..., description="Local Korea time, ISO format e.g. 2026-11-24T15:00:00")
    description: str = ""
    location: str = ""

def escape_ical(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

def to_ical_datetime(value: str) -> str:
    # Treat naive input as Asia/Seoul local time. Naver's calendar example uses TZID=Asia/Seoul.
    dt = datetime.fromisoformat(value)
    return dt.strftime("%Y%m%dT%H%M%S")

def get_saved_token():
    with db() as conn:
        row = conn.execute("SELECT * FROM naver_tokens WHERE id=1").fetchone()
    if not row:
        raise HTTPException(409, "Naver Calendar is not connected. Call /oauth/naver/start and complete Naver login first.")
    expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    if expires_at and expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60):
        if not row["refresh_token"]:
            raise HTTPException(409, "Naver access token expired. Reconnect Naver Calendar.")
        refreshed = refresh_naver_token(row["refresh_token"])
        return refreshed
    return dict(row)

def refresh_naver_token(refresh_token: str):
    require_naver_config()
    params = {
        "grant_type": "refresh_token",
        "client_id": NAVER_CLIENT_ID,
        "client_secret": NAVER_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=20) as client:
        response = client.get("https://nid.naver.com/oauth2.0/token", params=params)
    if response.status_code != 200:
        raise HTTPException(502, "Naver token refresh failed.")
    data = response.json()
    if data.get("error"):
        raise HTTPException(502, f"Naver token refresh error: {data.get('error_description', data['error'])}")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    with db() as conn:
        conn.execute("""INSERT OR REPLACE INTO naver_tokens
            (id, access_token, refresh_token, expires_at, token_type)
            VALUES (1, ?, ?, ?, ?)""",
            (data["access_token"], data.get("refresh_token", refresh_token),
             expires_at.isoformat(), data.get("token_type", "Bearer")))
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": expires_at.isoformat(), "token_type": data.get("token_type", "Bearer")}

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_HTML)

@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not APP_PASSWORD:
        raise HTTPException(503, "APP_PASSWORD is not configured on the server.")
    if not secrets.compare_digest(password, APP_PASSWORD):
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

def require_web_login(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(401, "로그인이 필요합니다.")
    return True

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/connect/naver")
def connect_naver_from_dashboard(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    require_naver_config()
    if not REDIRECT_URI:
        raise HTTPException(503, "NAVER_REDIRECT_URI is not configured.")
    state = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("INSERT INTO oauth_states(state, created_at) VALUES (?, ?)",
                     (state, datetime.now(timezone.utc).isoformat()))
    params = {"response_type": "code", "client_id": NAVER_CLIENT_ID,
              "redirect_uri": REDIRECT_URI, "state": state}
    url = "https://nid.naver.com/oauth2.0/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)

OPENAI_TOOLS = [
    {"type": "function", "name": "search_naver", "description": "Search Naver news, blogs, web pages, cafes, or Knowledge iN.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "category": {"type": "string", "enum": ["news", "blog", "webkr", "cafearticle", "kin"]},
         "display": {"type": "integer", "minimum": 1, "maximum": 20}, "sort": {"type": "string", "enum": ["sim", "date"]}},
         "required": ["query", "category"], "additionalProperties": False}},
    {"type": "function", "name": "naver_search_trend", "description": "Get Naver DataLab search keyword trend data. Use dates in YYYY-MM-DD.",
     "parameters": {"type": "object", "properties": {
         "startDate": {"type": "string"}, "endDate": {"type": "string"}, "timeUnit": {"type": "string", "enum": ["date", "week", "month"]},
         "keywordGroups": {"type": "array", "items": {"type": "object", "properties": {"groupName": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}}, "required": ["groupName", "keywords"], "additionalProperties": False}},
         "device": {"type": "string", "enum": ["pc", "mo"]}, "gender": {"type": "string", "enum": ["m", "f"]}},
         "required": ["startDate", "endDate", "keywordGroups"], "additionalProperties": False}},
    {"type": "function", "name": "naver_calendar_status", "description": "Check whether the Naver Calendar OAuth account is connected.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "create_naver_calendar_event", "description": "Create an event in the connected Naver Calendar. Only call when the user explicitly asks to create the event and title, date, start and end times are known. Use Asia/Seoul local time.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}, "location": {"type": "string"}},
         "required": ["title", "start", "end"], "additionalProperties": False}}
]

async def run_openai_tool(name: str, args: dict):
    if name == "search_naver":
        category = args.get("category", "news")
        if category not in {"news", "blog", "webkr", "cafearticle", "kin"}:
            raise ValueError("지원하지 않는 네이버 검색 종류입니다.")
        require_naver_config()
        headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
        params = {"query": args["query"], "display": min(max(int(args.get("display", 10)), 1), 20),
                  "start": 1, "sort": args.get("sort", "sim")}
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"https://openapi.naver.com/v1/search/{category}.json", params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        return {"total": data.get("total", 0), "items": data.get("items", [])}
    if name == "naver_search_trend":
        require_naver_config()
        payload = {k: v for k, v in args.items() if v is not None}
        headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://openapi.naver.com/v1/datalab/search", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()
    if name == "naver_calendar_status":
        with db() as conn:
            row = conn.execute("SELECT expires_at FROM naver_tokens WHERE id=1").fetchone()
        return {"connected": bool(row), "expires_at": row["expires_at"] if row else None}
    if name == "create_naver_calendar_event":
        body = CalendarRequest(**args)
        token = get_saved_token()
        start, end = to_ical_datetime(body.start), to_ical_datetime(body.end)
        if datetime.fromisoformat(body.end) <= datetime.fromisoformat(body.start):
            raise ValueError("종료 시간은 시작 시간보다 늦어야 합니다.")
        uid = secrets.token_hex(16) + "@woojoo-naver-actions"
        now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ical = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Woojoo Naver GPT//KO", "CALSCALE:GREGORIAN",
            "BEGIN:VTIMEZONE", "TZID:Asia/Seoul", "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE",
            "BEGIN:VEVENT", "SEQUENCE:0", "CLASS:PUBLIC", "TRANSP:OPAQUE", f"UID:{uid}", f"DTSTAMP:{now_utc}",
            f"DTSTART;TZID=Asia/Seoul:{start}", f"DTEND;TZID=Asia/Seoul:{end}", f"SUMMARY:{escape_ical(body.title)}",
            f"DESCRIPTION:{escape_ical(body.description)}", f"LOCATION:{escape_ical(body.location)}", "END:VEVENT", "END:VCALENDAR", ""])
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post("https://openapi.naver.com/calendar/createSchedule.json",
                data={"calendarId": "defaultCalendarId", "scheduleIcalString": ical},
                headers={"Authorization": f"Bearer {token['access_token']}"})
        r.raise_for_status()
        result = r.json()
        if result.get("result") != "success":
            raise RuntimeError(f"네이버 캘린더가 일정을 등록하지 못했습니다: {result}")
        return {"success": True, "result": result}
    raise ValueError("알 수 없는 도구 요청입니다.")

@app.post("/api/chat")
async def chat_api(request: Request, _login=Depends(require_web_login)):
    if not OPENAI_API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY가 Render 환경변수에 설정되지 않았습니다.")
    try:
        body = await request.json()
        messages = body.get("messages", [])[-20:]
        if not messages or messages[-1].get("role") != "user":
            raise HTTPException(400, "사용자 메시지가 필요합니다.")
        input_items = [{"role": m.get("role"), "content": str(m.get("content", ""))[:12000]}
                       for m in messages if m.get("role") in {"user", "assistant"}]
        instructions = """너는 우주수니 AI 매니저다. 한국어로 친근하고 둥글둥글한 말투를 사용한다.
네이버 검색이나 검색어 추세를 물으면 반드시 제공된 도구를 호출하고, 결과에 없는 사실이나 날짜를 만들지 않는다.
블로그 글은 사용자의 우주수니 스타일에 맞춰 자연스럽고 충분한 분량으로 작성하며 본문에 이모지를 넣지 않는다.
캘린더 일정 생성은 사용자가 명확히 요청한 경우에만 실행한다. 제목/날짜/시작/종료가 불명확하면 질문한다.
검색 결과는 공식 발표와 게시물의 주장을 구분하고, 검색 결과가 제공하지 않는 내용을 사실처럼 단정하지 않는다."""
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": OPENAI_MODEL, "instructions": instructions, "input": input_items, "tools": OPENAI_TOOLS, "tool_choice": "auto"})
            if response.status_code >= 400:
                raise HTTPException(502, "OpenAI API 호출에 실패했습니다. API 키, 결제 상태, 모델 설정을 확인하세요.")
            data = response.json()
            for _ in range(4):
                calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
                if not calls:
                    break
                outputs = []
                for call in calls:
                    try:
                        args = json.loads(call.get("arguments") or "{}")
                        result = await run_openai_tool(call["name"], args)
                        output = json.dumps(result, ensure_ascii=False)
                    except Exception as exc:
                        output = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
                response = await client.post("https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={"model": OPENAI_MODEL, "instructions": instructions,
                          "previous_response_id": data.get("id"), "input": outputs, "tools": OPENAI_TOOLS, "tool_choice": "auto"})
                if response.status_code >= 400:
                    raise HTTPException(502, "OpenAI 도구 실행 후 응답을 가져오지 못했습니다.")
                data = response.json()
        answer = "\n".join(item.get("text", "") for item in data.get("output", [])
                           if item.get("type") == "message" for item in item.get("content", []) if item.get("type") == "output_text")
        return {"answer": answer or "응답을 생성하지 못했습니다. 다시 시도해주세요."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"요청 처리 중 오류가 발생했습니다: {str(exc)[:300]}")

LOGIN_HTML = r'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>우주수니 AI 매니저 로그인</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f2ff;font-family:system-ui,sans-serif;color:#242038}.panel{width:min(92vw,390px);padding:32px;border-radius:24px;background:white;box-shadow:0 15px 50px #38206b18}h1{margin:0 0 8px;font-size:24px}.muted{color:#77718a;font-size:14px}input,button{width:100%;padding:14px;border-radius:12px;font-size:15px}input{border:1px solid #ddd7ee;margin:22px 0 12px}button{border:0;background:#6c4bd3;color:white;font-weight:700;cursor:pointer}</style><div class="panel"><h1>우주수니 AI 매니저</h1><p class="muted">나만 사용하는 AI 작업 공간입니다.</p><form method="post" action="/login"><input type="password" name="password" placeholder="접속 비밀번호" required autofocus><button>로그인</button></form></div></html>'''

DASHBOARD_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>우주수니 AI 매니저</title><style>
:root{--purple:#7052d9;--ink:#28243a;--muted:#817c91;--line:#ebe8f2;--bg:#f8f7fb}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:Inter,system-ui,-apple-system,sans-serif;color:var(--ink)}.app{max-width:1100px;margin:auto;min-height:100vh;display:grid;grid-template-columns:260px 1fr;background:white;box-shadow:0 0 50px #2c1a5510}.side{padding:28px 20px;background:#fbfaff;border-right:1px solid var(--line)}.brand{font-size:20px;font-weight:800;letter-spacing:-.5px}.sub{font-size:12px;color:var(--muted);margin-top:5px}.section{margin-top:34px;font-size:11px;font-weight:700;color:#9a94aa;letter-spacing:1px}.quick{display:block;width:100%;text-align:left;padding:12px;margin-top:8px;border:1px solid var(--line);border-radius:12px;background:white;color:#514b64;cursor:pointer}.quick:hover{border-color:#b9a9f2;background:#f7f4ff}.connect{display:block;text-decoration:none;text-align:center;background:#eee9ff;color:#5940b4;padding:12px;border-radius:12px;margin-top:12px;font-size:13px;font-weight:700}.main{display:flex;flex-direction:column;min-height:100vh}.top{padding:24px 30px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.top strong{font-size:16px}.status{font-size:12px;color:#25845a;background:#e9f8ef;padding:7px 10px;border-radius:20px}.messages{padding:30px;flex:1;overflow:auto;max-height:calc(100vh - 190px)}.welcome{max-width:650px;margin:7vh auto;text-align:center}.orb{width:62px;height:62px;margin:0 auto 20px;border-radius:22px;background:linear-gradient(135deg,#8c6be9,#c3a9ff);display:grid;place-items:center;color:white;font-size:28px}.welcome h1{font-size:28px;letter-spacing:-1px;margin:0 0 10px}.welcome p{color:var(--muted);line-height:1.7}.chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:24px}.chips button{border:1px solid var(--line);background:white;padding:10px 13px;border-radius:20px;color:#655e78;cursor:pointer}.msg{max-width:820px;margin:0 auto 20px;display:flex;gap:12px}.bubble{padding:15px 18px;border-radius:16px;line-height:1.7;white-space:pre-wrap;max-width:90%;font-size:14px}.user{justify-content:flex-end}.user .bubble{background:#eee9ff}.assistant .bubble{background:#f7f6fa;border:1px solid var(--line)}.composer-wrap{padding:18px 30px 24px;border-top:1px solid var(--line)}.composer{max-width:850px;margin:auto;display:flex;gap:10px;padding:9px;border:1px solid #ddd7ec;border-radius:17px;box-shadow:0 5px 20px #35205c0b}.composer textarea{flex:1;border:0;outline:0;resize:none;font:inherit;padding:9px;min-height:42px;max-height:150px}.send{border:0;background:var(--purple);color:white;border-radius:12px;padding:0 20px;font-weight:700;cursor:pointer}.hint{text-align:center;font-size:11px;color:#aaa4b5;margin-top:10px}.logout{background:none;border:0;color:#8b8499;cursor:pointer;font-size:12px}@media(max-width:760px){.app{display:block}.side{display:none}.top{padding:18px}.messages{padding:18px;max-height:calc(100vh - 165px)}.composer-wrap{padding:12px}.welcome{margin:12vh auto}.welcome h1{font-size:24px}}
</style></head><body><div class="app"><aside class="side"><div class="brand">우주수니 AI 매니저</div><div class="sub">NAVER × OPENAI WORKSPACE</div><div class="section">빠른 시작</div><button class="quick" onclick="ask('오늘 주목할 만한 네이버 뉴스 검색해줘')">오늘의 뉴스 찾기</button><button class="quick" onclick="ask('최근 검색어 트렌드를 비교할 수 있게 키워드 분석 방법을 안내해줘')">검색 트렌드 분석</button><button class="quick" onclick="ask('운빨존많겜 관련 네이버 블로그 검색 결과를 정리해줘')">블로그 글감 찾기</button><button class="quick" onclick="ask('우주수니 블로그 스타일로 글을 작성해줘')">블로그 글 작성</button><div class="section">네이버 연결</div><a class="connect" href="/connect/naver">네이버 캘린더 연결하기 ↗</a><div class="section">안내</div><p class="sub" style="line-height:1.7">검색과 트렌드는 네이버 API를 사용합니다. 일정 등록은 네이버 로그인을 먼저 연결한 뒤 요청하세요.</p></aside><main class="main"><header class="top"><strong>AI 대화</strong><div><span class="status">● 개인 작업 공간</span> <form style="display:inline" method="post" action="/logout"><button class="logout">로그아웃</button></form></div></header><section id="messages" class="messages"><div class="welcome" id="welcome"><div class="orb">✦</div><h1>무엇을 도와드릴까요?</h1><p>네이버 검색부터 검색어 트렌드 분석, 우주수니 블로그 글쓰기까지<br>한 곳에서 편하게 시작해보세요.</p><div class="chips"><button onclick="ask('네이버에서 최신 뉴스 검색해줘')">최신 뉴스 검색</button><button onclick="ask('검색어 트렌드를 분석하고 싶어')">트렌드 분석</button><button onclick="ask('블로그 글감 추천해줘')">블로그 글감 추천</button></div></div></section><div class="composer-wrap"><form class="composer" id="form"><textarea id="prompt" placeholder="메시지를 입력하세요..." rows="1"></textarea><button class="send" id="send">보내기</button></form><div class="hint">AI 답변은 검토 후 사용하세요. 캘린더 일정은 명시적으로 요청한 경우에만 등록됩니다.</div></div></main></div><script>
const messages=[];const box=document.getElementById('messages'),input=document.getElementById('prompt'),form=document.getElementById('form'),send=document.getElementById('send');function add(role,text){document.getElementById('welcome')?.remove();const row=document.createElement('div');row.className='msg '+role;const b=document.createElement('div');b.className='bubble';b.textContent=text;row.appendChild(b);box.appendChild(row);box.scrollTop=box.scrollHeight;return b}function ask(t){input.value=t;form.requestSubmit()}form.addEventListener('submit',async e=>{e.preventDefault();const text=input.value.trim();if(!text)return;input.value='';messages.push({role:'user',content:text});add('user',text);send.disabled=true;send.textContent='생성 중';const pending=add('assistant','답변을 준비하고 있어요...');try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'요청에 실패했습니다.');pending.textContent=data.answer;messages.push({role:'assistant',content:data.answer});}catch(err){pending.textContent='오류: '+err.message;}finally{send.disabled=false;send.textContent='보내기';box.scrollTop=box.scrollHeight;}});input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();form.requestSubmit()}});
</script></body></html>'''
@app.get("/health")
def health():
    return {"status": "ok", "service": APP_TITLE, "version": "0.1.0"}

@app.get("/actions/naver/search")
async def naver_search(
    query: str = Query(..., min_length=1, max_length=100),
    category: str = Query("news"),
    display: int = Query(10, ge=1, le=100),
    start: int = Query(1, ge=1, le=1000),
    sort: str = Query("sim"),
    _auth=Depends(require_action_key),
    _config=Depends(require_naver_config),
):
    allowed = {"news", "blog", "webkr", "cafearticle", "kin"}
    if category not in allowed:
        raise HTTPException(400, f"category must be one of: {', '.join(sorted(allowed))}")
    if sort not in {"sim", "date"}:
        raise HTTPException(400, "sort must be sim or date")
    params = {"query": query, "display": display, "start": start, "sort": sort}
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    url = f"https://openapi.naver.com/v1/search/{category}.json"
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(url, params=params, headers=headers)
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Naver Search API request failed.")
    return response.json()

@app.post("/actions/naver/datalab/search")
async def datalab_search(body: DataLabRequest, _auth=Depends(require_action_key), _config=Depends(require_naver_config)):
    try:
        start = datetime.strptime(body.startDate, "%Y-%m-%d")
        end = datetime.strptime(body.endDate, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "startDate/endDate must use YYYY-MM-DD.")
    if end < start:
        raise HTTPException(400, "endDate must be on or after startDate.")
    payload = body.model_dump(exclude_none=True)
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://openapi.naver.com/v1/datalab/search", json=payload, headers=headers)
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Naver DataLab Search API request failed.")
    return response.json()

@app.get("/actions/naver/calendar/status")
def calendar_status(_auth=Depends(require_action_key)):
    with db() as conn:
        row = conn.execute("SELECT expires_at FROM naver_tokens WHERE id=1").fetchone()
    return {"connected": bool(row), "expires_at": row["expires_at"] if row else None}

@app.get("/oauth/naver/start")
def naver_oauth_start(_auth=Depends(require_action_key), _config=Depends(require_naver_config)):
    if not REDIRECT_URI:
        raise HTTPException(503, "NAVER_REDIRECT_URI is not configured.")
    state = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("INSERT INTO oauth_states(state, created_at) VALUES (?, ?)",
                     (state, datetime.now(timezone.utc).isoformat()))
    params = {
        "response_type": "code",
        "client_id": NAVER_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return {"authorization_url": "https://nid.naver.com/oauth2.0/authorize?" + urllib.parse.urlencode(params),
            "instructions": "Open authorization_url in a browser, sign in to Naver, and approve the requested permissions."}

@app.get("/oauth/naver/callback", response_class=HTMLResponse)
def naver_oauth_callback(code: str, state: str):
    with db() as conn:
        row = conn.execute("SELECT state FROM oauth_states WHERE state=?", (state,)).fetchone()
        if not row:
            raise HTTPException(400, "Invalid or expired OAuth state.")
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    require_naver_config()
    params = {
        "grant_type": "authorization_code",
        "client_id": NAVER_CLIENT_ID,
        "client_secret": NAVER_CLIENT_SECRET,
        "code": code,
        "state": state,
    }
    with httpx.Client(timeout=25) as client:
        response = client.get("https://nid.naver.com/oauth2.0/token", params=params)
    if response.status_code != 200:
        raise HTTPException(502, "Naver authorization token exchange failed.")
    data = response.json()
    if data.get("error") or not data.get("access_token"):
        raise HTTPException(502, f"Naver authorization failed: {data.get('error_description', data.get('error', 'unknown error'))}")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    with db() as conn:
        conn.execute("""INSERT OR REPLACE INTO naver_tokens
            (id, access_token, refresh_token, expires_at, token_type)
            VALUES (1, ?, ?, ?, ?)""",
            (data["access_token"], data.get("refresh_token"), expires_at.isoformat(),
             data.get("token_type", "Bearer")))
    return HTMLResponse("<h2>Naver Calendar connected</h2><p>You can return to ChatGPT and ask it to check the connection.</p>")

@app.post("/actions/naver/calendar/create")
async def create_calendar_event(body: CalendarRequest, _auth=Depends(require_action_key)):
    token = get_saved_token()
    try:
        start = to_ical_datetime(body.start)
        end = to_ical_datetime(body.end)
    except ValueError:
        raise HTTPException(400, "start/end must be ISO date-time values.")
    if datetime.fromisoformat(body.end) <= datetime.fromisoformat(body.start):
        raise HTTPException(400, "end must be later than start.")
    uid = secrets.token_hex(16) + "@woojoo-naver-actions"
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ical = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Woojoo Naver GPT//KO",
        "CALSCALE:GREGORIAN", "BEGIN:VTIMEZONE", "TZID:Asia/Seoul",
        "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0900",
        "TZOFFSETTO:+0900", "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE",
        "BEGIN:VEVENT", "SEQUENCE:0", "CLASS:PUBLIC", "TRANSP:OPAQUE",
        f"UID:{uid}", f"DTSTAMP:{now_utc}",
        f"DTSTART;TZID=Asia/Seoul:{start}", f"DTEND;TZID=Asia/Seoul:{end}",
        f"SUMMARY:{escape_ical(body.title)}",
        f"DESCRIPTION:{escape_ical(body.description)}",
        f"LOCATION:{escape_ical(body.location)}",
        "END:VEVENT", "END:VCALENDAR", ""
    ])
    data = {"calendarId": "defaultCalendarId", "scheduleIcalString": ical}
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post("https://openapi.naver.com/calendar/createSchedule.json",
                                     data=data, headers=headers)
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Naver Calendar event creation failed. Check Naver Calendar permission and token.")
    result = response.json()
    if result.get("result") != "success":
        raise HTTPException(502, f"Naver Calendar returned an unsuccessful result: {result}")
    return {"success": True, "message": "Naver Calendar event created.", "naver_result": result}
