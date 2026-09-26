"""단일 사용자 OAuth 2.1 인가 서버 provider (SPEC §1).

크립토를 직접 짜지 않는다 — MCP SDK 가 PKCE·토큰 엔드포인트·디스커버리를 처리한다.
여기서는 저장(SQLite)과 로그인 게이트만 구현한다.

흐름:
  claude.ai -> /authorize -> provider.authorize() 가 /login?ticket= 로 넘긴다
  /login (비밀번호 1개) 통과 -> 인가코드 발급 -> redirect_uri 로 되돌림
  claude.ai -> /token (PKCE) -> SDK 검증 -> exchange_authorization_code() 가 토큰 발급
  /mcp 요청마다 -> load_access_token() 이 Bearer 검증
"""

import json
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import config, db

SUBJECT = "erion-user"          # 단일 사용자
SCOPE = "erion"
ACCESS_TTL = 3600               # 1시간
REFRESH_TTL = 30 * 24 * 3600    # 30일
CODE_TTL = 300                  # 5분
PENDING_TTL = 600               # 로그인까지 10분


def _new_token() -> str:
    return secrets.token_urlsafe(32)


class ErionOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    # ── 클라이언트 (DCR 허용: claude.ai 가 self-register) ──────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = db.auth()
        try:
            row = conn.execute(
                "SELECT data FROM oauth_client WHERE client_id = ?", [client_id]
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return OAuthClientInformationFull.model_validate_json(row["data"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        conn = db.auth()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_client(client_id,data,created_at) "
                "VALUES(?,?,?)",
                [client_info.client_id, client_info.model_dump_json(),
                 str(int(time.time()))],
            )
            conn.commit()
        finally:
            conn.close()

    # ── 인가: 로그인 페이지로 넘긴다 ───────────────────────────────────────
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        ticket = _new_token()
        payload = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [SCOPE],
            "resource": params.resource,
        }
        conn = db.auth()
        try:
            conn.execute(
                "INSERT INTO oauth_pending(ticket,data,expires_at) VALUES(?,?,?)",
                [ticket, json.dumps(payload), time.time() + PENDING_TTL],
            )
            conn.commit()
        finally:
            conn.close()
        return f"{config.ISSUER}/login?ticket={ticket}"

    # ── 로그인 통과 후 /login 라우트가 부른다 (SDK 인터페이스 아님) ─────────
    def issue_code_from_ticket(self, ticket: str) -> str | None:
        """유효 티켓이면 인가코드를 만들어 저장하고, redirect_uri(code,state)를 돌려준다."""
        conn = db.auth()
        try:
            row = conn.execute(
                "SELECT data,expires_at FROM oauth_pending WHERE ticket = ?", [ticket]
            ).fetchone()
            if not row or row["expires_at"] < time.time():
                return None
            p = json.loads(row["data"])
            code = _new_token()
            ac = AuthorizationCode(
                code=code, scopes=p["scopes"], expires_at=time.time() + CODE_TTL,
                client_id=p["client_id"], code_challenge=p["code_challenge"],
                redirect_uri=p["redirect_uri"],
                redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                resource=p.get("resource"), subject=SUBJECT,
            )
            conn.execute("INSERT INTO oauth_code(code,data,expires_at) VALUES(?,?,?)",
                         [code, ac.model_dump_json(), ac.expires_at])
            conn.execute("DELETE FROM oauth_pending WHERE ticket = ?", [ticket])
            conn.commit()
            return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])
        finally:
            conn.close()

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                     authorization_code: str) -> AuthorizationCode | None:
        conn = db.auth()
        try:
            row = conn.execute(
                "SELECT data,expires_at FROM oauth_code WHERE code = ?",
                [authorization_code],
            ).fetchone()
        finally:
            conn.close()
        if not row or row["expires_at"] < time.time():
            return None
        return AuthorizationCode.model_validate_json(row["data"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                        authorization_code: AuthorizationCode) -> OAuthToken:
        # PKCE 검증은 SDK 토큰 핸들러가 이미 했다. 코드는 1회용 — 지운다.
        conn = db.auth()
        try:
            conn.execute("DELETE FROM oauth_code WHERE code = ?",
                         [authorization_code.code])
            conn.commit()
        finally:
            conn.close()
        return self._issue_tokens(authorization_code.client_id,
                                  authorization_code.scopes)

    # ── 리프레시 ───────────────────────────────────────────────────────────
    async def load_refresh_token(self, client: OAuthClientInformationFull,
                               refresh_token: str) -> RefreshToken | None:
        conn = db.auth()
        try:
            row = conn.execute(
                "SELECT data,expires_at FROM oauth_token WHERE token = ? AND kind='refresh'",
                [refresh_token],
            ).fetchone()
        finally:
            conn.close()
        if not row or (row["expires_at"] and row["expires_at"] < time.time()):
            return None
        return RefreshToken.model_validate_json(row["data"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                   refresh_token: RefreshToken,
                                   scopes: list[str]) -> OAuthToken:
        conn = db.auth()
        try:
            conn.execute("DELETE FROM oauth_token WHERE token = ?",
                         [refresh_token.token])  # 회전
            conn.commit()
        finally:
            conn.close()
        return self._issue_tokens(refresh_token.client_id,
                                  scopes or refresh_token.scopes)

    # ── 액세스 토큰 검증 (/mcp 요청마다) ──────────────────────────────────
    async def load_access_token(self, token: str) -> AccessToken | None:
        conn = db.auth()
        try:
            row = conn.execute(
                "SELECT data,expires_at FROM oauth_token WHERE token = ? AND kind='access'",
                [token],
            ).fetchone()
        finally:
            conn.close()
        if not row or (row["expires_at"] and row["expires_at"] < time.time()):
            return None
        return AccessToken.model_validate_json(row["data"])

    async def revoke_token(self, token) -> None:
        tok = token.token if hasattr(token, "token") else token
        conn = db.auth()
        try:
            conn.execute("DELETE FROM oauth_token WHERE token = ?", [tok])
            conn.commit()
        finally:
            conn.close()

    async def exchange_identity_assertion(self, *args, **kwargs):
        raise NotImplementedError("identity assertion 미지원")

    # ── 내부: 액세스+리프레시 발급 후 저장 ────────────────────────────────
    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access = _new_token()
        refresh = _new_token()
        now = time.time()
        at = AccessToken(token=access, client_id=client_id, scopes=scopes,
                         expires_at=int(now + ACCESS_TTL), subject=SUBJECT)
        rt = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                          expires_at=int(now + REFRESH_TTL), subject=SUBJECT)
        conn = db.auth()
        try:
            conn.execute("INSERT INTO oauth_token(token,kind,data,expires_at) VALUES(?,?,?,?)",
                         [access, "access", at.model_dump_json(), at.expires_at])
            conn.execute("INSERT INTO oauth_token(token,kind,data,expires_at) VALUES(?,?,?,?)",
                         [refresh, "refresh", rt.model_dump_json(), rt.expires_at])
            conn.commit()
        finally:
            conn.close()
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)
