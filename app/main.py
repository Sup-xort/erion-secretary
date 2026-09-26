"""erion 볼트 — 원격 MCP 서버 조립점.

step 1: /mcp + OAuth + 읽기 3종(deadlines, blocks, docs_search/doc_read).
2026-09-18: 대시보드 블록 도구 — board · canvas_find/read · item_save/done · est_adjust/history.
  이게 첫 쓰기 도구다. claude.ai 가 쓰기 도구를 실제로 부르는지가 여기서 처음 검증된다.
  쓰기는 블록(item)·표시 이름·추정값에 한정된다. Canvas 원본 데이터는 못 바꾼다.
purge/raw/삭제는 여기 도구 레지스트리에 아예 등록하지 않는다 — 의도된 것.
  블록을 없애는 방법은 item_done 뿐이다.
2026-09-20: 완료 표시가 Canvas 과제·강의영상까지 넓어졌다 (tools.mark_done · done_mark 테이블).
  대시보드에도 완료 버튼이 생겼다 — 원본이 뭐라 하든 사람이 찍은 게 이긴다.
"""

import asyncio
import hmac
import html

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from . import canvas, config, db, gcal_oauth, learningx, pages, planner, tools, web
from .oauth_provider import SCOPE, ErionOAuthProvider

db.init()

provider = ErionOAuthProvider()

server = MCPServer(
    "erion-vault",
    version="0.1.0",
    instructions="erion 개인 금고. 마감·블록·문서를 좁은 질의로 읽는다. "
                 "응답은 행 상한이 있다. 목록 전체를 요구하지 마라.\n"
                 "대시보드(/erion/web)의 블록은 과제·할 일·영상이다. 할 일 목록은 board 로 본다. "
                 "강의영상의 출석 인정기간·시청 여부는 lectures 로 본다 — Canvas 에는 그 정보가 없다. "
                 "사용자가 '이 과목 이 영상 봐야 해' 같은 말을 하면 canvas_find → canvas_read 로 찾고 "
                 "정리해서 item_save 로 블록을 만든다. 만들기 전에 est_history 로 사용자의 보정 기록을 "
                 "보고 est_hours 를 정하라. 사용자가 소요시간을 고쳐 말하면 est_adjust 로 남겨라 — "
                 "그게 다음 추정의 기억이 된다. 표시 이름은 label 이다(원제목 title 과 다르다).\n"
                 "이번 주 계획(어느 날 뭘 할지)은 week_plan 으로 본다 — 서버가 수업·캘린더·마감·소요시간으로 "
                 "짠 것이다. 사용자가 배치를 바꾸고 싶어하면('물리 숙제는 목요일에 할게', '금요일엔 못 해') "
                 "plan_pin(과제를 그 날에) / plan_day(그 날 가용시간)로 고치고 week_plan 으로 결과를 보여줘라. "
                 "계획 자체를 네가 새로 짜서 말로만 답하지 마라 — 대시보드와 어긋난다.",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=config.ISSUER,
        resource_server_url=f"{config.ISSUER}{config.MCP_PATH}",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=None,   # 단일 사용자 — 스코프 협상 실패 방지. 토큰 자체로 검증한다
    ),
)


# ── 읽기 도구 3종 (SPEC §3). 커넥션은 요청마다 열고 닫는다 ──────────────────
@server.tool(description="다가오는 마감. within_days 안, 기본 미제출만. 최대 20행.")
def deadlines(within_days: int = 14, include_submitted: bool = False) -> list[dict]:
    conn = db.vault()
    try:
        return tools.deadlines(conn, within_days, include_submitted)
    finally:
        conn.close()


@server.tool(description="시각 블록(약속·강의). start~end 구간, ISO8601. 최대 50행.")
def blocks(start: str, end: str) -> list[dict]:
    conn = db.vault()
    try:
        return tools.blocks(conn, start, end)
    finally:
        conn.close()


@server.tool(description="문서 제목 검색. kind=material|output|note. 발췌 포함. 최대 10행.")
def docs_search(q: str, kind: str | None = None, limit: int = 10) -> list[dict]:
    conn = db.vault()
    try:
        return tools.docs_search(conn, q, kind, limit)
    finally:
        conn.close()


@server.tool(description="문서 본문 읽기. 40KB 상한, 초과 시 truncated=true.")
def doc_read(id: str) -> dict:
    conn = db.vault()
    try:
        return tools.doc_read(conn, id)
    finally:
        conn.close()


# ── 대시보드 블록 도구 (2026-09-18) ──────────────────────────────────────────
@server.tool(description="대시보드 블록 목록 — Canvas 과제·할 일·영상, 마감순. within_days 안. "
                         "각 블록의 id 로 item_save/item_done/est_adjust 를 부른다. 최대 40행.")
def board(within_days: int = 14, include_done: bool = False) -> list[dict]:
    conn = db.vault()
    try:
        return tools.board_brief(conn, within_days, include_done)
    finally:
        conn.close()


@server.tool(description="강의영상의 **출석 인정기간**과 시청 여부. Canvas 에는 이 정보가 없다 — "
                         "LearningX 에서 읽는다. within_days 안에 인정기간이 끝나는 것만, 마감순. "
                         "기본은 아직 안 본 것만(unwatched_only). course 는 과목명 일부. "
                         "minutes 는 영상 길이라 est_hours 를 여기서 정하면 된다. 최대 30행.")
async def lectures(within_days: int = 14, course: str | None = None,
                   unwatched_only: bool = True) -> list[dict]:
    try:
        return await asyncio.to_thread(learningx.brief, within_days, course, unwatched_only)
    except (learningx.LearningXError, canvas.CanvasError) as e:
        return [{"error": str(e)}]


@server.tool(description="사용자의 Canvas 에서 모듈 항목(강의영상·페이지·과제·파일)을 제목으로 찾는다. "
                         "query 는 공백으로 나뉜 낱말 전부가 '모듈명+항목명'에 들어가야 맞는다 "
                         "(예: '3주차', 'Lecture 3'). course 는 과목명 일부(영문 과목은 영문으로). "
                         "kind: attendance=출결 LTI(대개 강의영상), zoom=Zoom 녹화. 최대 30행.")
async def canvas_find(query: str = "", course: str | None = None) -> list[dict]:
    try:
        return await asyncio.to_thread(canvas.find, query, course)
    except canvas.CanvasError as e:
        return [{"error": str(e)}]


@server.tool(description="canvas_find 가 준 ref 의 내용 — 페이지·과제 본문, 같은 모듈의 다른 항목 제목. "
                         "영상(attendance/zoom)은 본문을 못 읽는다: 제목·모듈·주변 항목으로 정리하라.")
async def canvas_read(ref: str) -> dict:
    try:
        return await asyncio.to_thread(canvas.read, ref)
    except canvas.CanvasError as e:
        return {"error": str(e)}


@server.tool(description="블록 만들기/고치기. id 없이 부르면 새 블록(kind=task|video, label 필수). "
                         "id 를 주면 넘긴 필드만 바뀐다. Canvas 과제(id=canvas:*)는 label·prep_note 만. "
                         "날짜는 두 칸이다 — due_at 은 밖에서 정해진 진짜 마감(교수·Canvas·출석 인정기간)만, "
                         "사용자가 '10/4 까지 볼게' 처럼 스스로 정한 날은 target_at. 헷갈리면 사용자에게 묻거나 target_at. "
                         "형식은 '2026-09-20' 또는 '2026-09-20T18:00'(KST), 'none' 을 주면 그 칸을 비운다. "
                         "description 에 정리한 내용, url 에 Canvas 링크를 넣어라 — 대시보드에서 누르면 보인다.")
def item_save(id: str | None = None, kind: str | None = None, label: str | None = None,
              course: str | None = None, due_at: str | None = None, est_hours: float | None = None,
              prep_note: str | None = None, description: str | None = None,
              url: str | None = None, target_at: str | None = None) -> dict:
    conn = db.vault()
    try:
        return tools.item_save(conn, id=id, kind=kind, label=label, course=course, due_at=due_at,
                               est_hours=est_hours, prep_note=prep_note,
                               description=description, url=url, target_at=target_at)
    except ValueError as e:
        return {"ok": False, "error": f"형식 오류: {e}"}
    finally:
        conn.close()


@server.tool(description="블록을 완료(done=true)/되살리기(false). 대시보드에서 사라진다. "
                         "할 일·영상은 물론 Canvas 과제(canvas:*)와 강의영상(lx:*)도 받는다 — "
                         "사용자가 '이거 다 했어' 라고 하면 Canvas 제출 여부와 무관하게 찍어라. "
                         "Canvas 에 실제로 낸 것은 폴러가 알아서 내리니 부를 필요 없다.")
def item_done(id: str, done: bool = True) -> dict:
    conn = db.vault()
    try:
        return tools.item_done(conn, id, done)
    finally:
        conn.close()


@server.tool(description="소요시간 보정. 사용자가 '이건 3시간은 걸려' → hours=3 (추정 수정). "
                         "'실제로 2시간 걸렸어' → actual=true (실측 기록, 추정값은 그대로). "
                         "reason 에 사용자가 말한 이유를 그대로 짧게. 기록은 다음 추정에 반영된다.")
def est_adjust(id: str, hours: float, reason: str | None = None, actual: bool = False) -> dict:
    conn = db.vault()
    try:
        return tools.est_adjust(conn, id, hours, reason, actual)
    finally:
        conn.close()


@server.tool(description="지금까지의 소요시간 보정 기록(최근순) — 이 사용자의 실제 속도. "
                         "새 블록의 est_hours 를 정하기 전에 본다. course 로 좁힐 수 있다. 최대 30행.")
def est_history(course: str | None = None, limit: int = 15) -> list[dict]:
    conn = db.vault()
    try:
        return tools.est_history(conn, course, limit)
    finally:
        conn.close()


# ── 이번 주 계획 (2026-09-23) ────────────────────────────────────────────────
# 계획은 저장하지 않는다 — 부를 때마다 planner.compute 가 새로 짠다. 대시보드 "이번 주" 탭과
# 같은 함수라 챗에서 본 것과 화면이 어긋나지 않는다. 사람이 고친 것만 plan_pin/plan_day 에 쌓인다.
async def _plan() -> dict:
    conn = db.vault()
    try:
        marks = tools.done_ids(conn)
    finally:
        conn.close()
    lec, _seen, _err = await asyncio.to_thread(web._lectures, marks)
    conn = db.vault()
    try:
        live = tools.board(conn, within_days=planner.HORIZON, past_days=14, limit=400)
        return planner.compute(conn, live + lec)
    finally:
        conn.close()


@server.tool(description="이번 주 계획 — 날마다 가용시간(cap, 수업·캘린더 일정을 뺀 것), 막힌 일정(busy), "
                         "그 날 할 과제와 시간(tasks). reason 은 왜 그 날로 당겨졌는지. short 는 마감 안에 "
                         "안 들어가는 것. days 는 1~14 (기본 7). 과제 id 로 plan_pin 을 부른다.")
async def week_plan(days: int = 7) -> dict:
    return planner.brief(await _plan(), days)


@server.tool(description="과제를 특정 날에 박는다 — 사용자가 '물리 숙제는 목요일에 할게' 라고 하면. "
                         "id 는 week_plan/board 의 id, day 는 'YYYY-MM-DD'. hours 를 주면 그 날 그만큼만 "
                         "(나머지는 자동 배치), 안 주면 남은 양 전부. hours=0 → 그 날 박은 것 풀기, "
                         "day='*' + hours=0 → 이 과제에 박은 것 전부 풀기. note 에 사용자가 말한 이유를 짧게.")
def plan_pin(id: str, day: str, hours: float | None = None, note: str | None = None) -> dict:
    conn = db.vault()
    try:
        return planner.pin(conn, id, day, hours, note)
    finally:
        conn.close()


@server.tool(description="그 날 과제에 쓸 수 있는 시간을 직접 정한다 — '금요일엔 본가 가서 못 해' → hours=0, "
                         "'토요일은 8시간 할 수 있어' → hours=8. hours 를 비우면(null) 자동 계산으로 되돌린다. "
                         "day 는 'YYYY-MM-DD'. note 에 이유를 짧게 — 대시보드에 그대로 보인다.")
def plan_day(day: str, hours: float | None = None, note: str | None = None) -> dict:
    conn = db.vault()
    try:
        return planner.set_day(conn, day, hours, note)
    finally:
        conn.close()


# ── 로그인 게이트 (단일 사용자 비밀번호 1개) ────────────────────────────────
_LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>erion 로그인</title>
<style>body{{background:#0d1117;color:#e6edf3;font:15px/1.6 ui-monospace,monospace;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
form{{width:min(320px,88vw)}}input{{width:100%;box-sizing:border-box;padding:10px;
margin:6px 0;background:#161b22;border:1px solid #30363d;border-radius:8px;color:inherit}}
button{{width:100%;padding:10px;margin-top:8px;background:#238636;border:0;border-radius:8px;
color:#fff;font:inherit;cursor:pointer}}.err{{color:#f85149;font-size:13px}}
h1{{font-size:14px;color:#7d8590;font-weight:600}}</style>
<form method=post action="{action}">
<h1>erion 볼트 연결</h1>
<input type=hidden name=ticket value="{ticket}">
<input type=password name=password placeholder="비밀번호" autofocus autocomplete=current-password>
{err}
<button type=submit>연결 승인</button>
</form>"""


def _login_page(ticket: str, err: str = "", status: int = 200) -> HTMLResponse:
    # 폼 action 은 ISSUER 에서 뽑는다 — erion 이 /erion 서브패스에 살아서 "/login" 으로
    # 박으면 루트로 POST 가 날아가 404 다. provider.authorize() 도 같은 근거로 ISSUER 기반.
    return HTMLResponse(
        _LOGIN_HTML.format(ticket=html.escape(ticket), err=err,
                           action=f"{config.ISSUER}/login"),
        status_code=status,
    )


async def login(request: Request):
    if request.method == "GET":
        return _login_page(request.query_params.get("ticket", ""))
    form = await request.form()
    ticket = str(form.get("ticket", ""))
    password = str(form.get("password", ""))
    ok = bool(config.AS_PASSWORD) and hmac.compare_digest(password, config.AS_PASSWORD)
    if not ok:
        return _login_page(ticket, '<div class=err>비밀번호가 틀렸다</div>', 401)
    redirect = provider.issue_code_from_ticket(ticket)
    if not redirect:
        return _login_page("", '<div class=err>티켓 만료. 다시 시도해라</div>', 400)
    return RedirectResponse(redirect, status_code=302)


# ── ASGI 앱 ────────────────────────────────────────────────────────────────
# Host/Origin 허용 목록을 명시적으로 넘긴다. 안 넘기면 SDK 가 host 기본값("127.0.0.1")을
# 보고 localhost 전용 서버로 판단해 도메인 요청을 421 로 튕긴다 — 근거는 config.py.
app = server.streamable_http_app(
    streamable_http_path=config.MCP_PATH,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # nginx 는 `proxy_set_header Host $host` 로 실제 도메인을 넘긴다.
        # 뒤 둘은 서버에서 8788 로 직접 찔러볼 때를 위한 것이다.
        allowed_hosts=[config.ISSUER_HOST, "127.0.0.1:*", "localhost:*"],
        allowed_origins=config.ALLOWED_ORIGINS,
    ),
)
app.routes.insert(0, Route("/login", login, methods=["GET", "POST"]))
# 구글 캘린더 토큰 획득 면. 1회성이고 erion 비밀번호로 잠겨 있다 (gcal_oauth.py).
app.routes.insert(0, Route("/gcal/start", gcal_oauth.start, methods=["GET", "POST"]))
app.routes.insert(0, Route("/gcal/callback", gcal_oauth.callback, methods=["GET"]))

# 공개 정적 면 3종 (pages.py). 구글 동의 화면이 홈페이지·처리방침·약관 URL 을 요구한다 —
# 이게 없으면 게시 상태를 프로덕션으로 못 올리고, 테스트에 머물면 토큰이 7일마다 죽는다.
# "/" 는 web.ROUTES 가 잡는다 — 로그인 시 대시보드, 비로그인 시 pages.home (web.root 참조)
app.routes.insert(0, Route("/privacy", pages.privacy, methods=["GET"]))
app.routes.insert(0, Route("/terms", pages.terms, methods=["GET"]))
# 끝 슬래시 변형도 직접 받는다. 안 받으면 Starlette 가 307 로 `/privacy` 에 보내는데,
# nginx 가 벗긴 `/erion` 을 모르니 루트 `/privacy` 로 가서 404 가 난다 (2026-09-16,
# 구글 검토가 "처리방침 내용 부족" 으로 반려한 원인).
app.routes.insert(0, Route("/privacy/", pages.privacy, methods=["GET"]))
app.routes.insert(0, Route("/terms/", pages.terms, methods=["GET"]))

# 대시보드 (web.py). 쿠키 로그인. 끝 슬래시 변형도 위와 같은 이유로 직접 받는다.
for _path, _fn, _methods in web.ROUTES:
    app.routes.insert(0, Route(_path, _fn, methods=_methods))


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
