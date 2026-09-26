"""구글 캘린더 OAuth — refresh token 한 번 받아서 파일에 넣는 것이 전부.

서버엔 브라우저가 없고 사용자 브라우저는 다른 기기에 있다. 그래서 루프백 리디렉션을
못 쓴다. 대신 **우리가 이미 가진 도메인으로 콜백을 받는다** — `…/erion/gcal/callback`.
구글 콘솔에 "웹 애플리케이션" 클라이언트로 이 URI 를 등록해두면 된다.

이 면은 erion 비밀번호로 잠근다. 안 잠그면 지나가던 사람이 흐름을 시작해서
**자기 구글 계정 토큰**을 여기 심을 수 있다.

여기서 하는 일은 토큰 획득뿐이다. 실제 조회는 `poller/gcal_poll.py` 가 한다.
"""

import json
import hmac
import os
import secrets
import time
import urllib.parse
import urllib.request

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

TOKEN_FILE = config.DATA_DIR / "gcal_token.json"
REDIRECT_URI = f"{config.ISSUER}/gcal/callback"

_pending: dict[str, float] = {}      # state -> 만료시각. 프로세스 메모리면 충분하다(1회성)
_STATE_TTL = 600


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>erion · 구글 캘린더</title>"
        "<style>body{background:#0d1117;color:#e6edf3;font:15px/1.7 ui-monospace,monospace;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        "div{width:min(460px,88vw)}input{width:100%;box-sizing:border-box;padding:10px;margin:6px 0;"
        "background:#161b22;border:1px solid #30363d;border-radius:8px;color:inherit}"
        "button{width:100%;padding:10px;margin-top:8px;background:#238636;border:0;"
        "border-radius:8px;color:#fff;font:inherit;cursor:pointer}"
        "code{color:#7d8590}.err{color:#f85149}.ok{color:#3fb950}</style>"
        f"<div>{body}</div>", status_code=status)


def _configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


async def start(request: Request):
    if not _configured():
        return _page(
            "<h1 class=err>구글 클라이언트가 아직 없다</h1>"
            "<p><code>.env</code> 에 <code>GOOGLE_CLIENT_ID</code> 와 "
            "<code>GOOGLE_CLIENT_SECRET</code> 을 넣고 "
            "<code>systemctl restart erion-vault</code> 한 뒤 다시 와라.</p>"
            f"<p>구글 콘솔에 등록할 리디렉션 URI:<br><code>{REDIRECT_URI}</code></p>", 503)

    if request.method == "GET":
        return _page(
            "<h1>구글 캘린더 연결</h1>"
            "<form method=post>"
            "<input type=password name=password placeholder='erion 비밀번호' autofocus>"
            "<button type=submit>구글로 이동</button></form>")

    form = await request.form()
    if not (config.AS_PASSWORD and hmac.compare_digest(
            str(form.get("password", "")), config.AS_PASSWORD)):
        return _page("<h1 class=err>비밀번호가 틀렸다</h1>"
                     "<form method=post><input type=password name=password autofocus>"
                     "<button type=submit>다시</button></form>", 401)

    now = time.time()
    for k, exp in list(_pending.items()):
        if exp < now:
            _pending.pop(k, None)
    state = secrets.token_urlsafe(24)
    _pending[state] = now + _STATE_TTL

    q = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",     # refresh token 을 받기 위한 조건
        "prompt": "consent",          # 두 번째부터도 refresh token 을 다시 주게 한다
        "include_granted_scopes": "true",
        "state": state,
    })
    return RedirectResponse(f"{AUTH_URL}?{q}", status_code=302)


async def callback(request: Request):
    err = request.query_params.get("error")
    if err:
        return _page(f"<h1 class=err>구글이 거절했다</h1><p><code>{err}</code></p>", 400)

    state = request.query_params.get("state", "")
    if not state or _pending.pop(state, 0) < time.time():
        return _page("<h1 class=err>state 가 만료됐거나 맞지 않는다</h1>"
                     "<p>처음부터 다시 해라.</p>", 400)

    code = request.query_params.get("code", "")
    if not code:
        return _page("<h1 class=err>code 가 없다</h1>", 400)

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return _page("<h1 class=err>토큰 교환 실패</h1>"
                     f"<p><code>{e.code}</code></p>", 502)

    if "refresh_token" not in tok:
        return _page("<h1 class=err>refresh token 이 안 왔다</h1>"
                     "<p>구글 계정의 기존 권한을 해제하고 다시 해라 "
                     "(<code>myaccount.google.com/permissions</code>).</p>", 400)

    TOKEN_FILE.write_text(json.dumps({
        "refresh_token": tok["refresh_token"],
        "scope": tok.get("scope"),
        "obtained_at": int(time.time()),
    }, ensure_ascii=False))
    os.chmod(TOKEN_FILE, 0o600)
    return _page("<h1 class=ok>연결됐다</h1>"
                 "<p>refresh token 을 서버에 저장했다. 이 창은 닫아도 된다.</p>"
                 "<p><code>poller/gcal_poll.py --dry</code> 로 확인해라.</p>")
