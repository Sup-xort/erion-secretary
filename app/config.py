"""erion 볼트 설정 — 환경변수 하나로 모은다. 근거 없는 기본값은 두지 않는다."""

import os
from pathlib import Path
from urllib.parse import urlsplit

ISSUER = os.environ.get("ERION_ISSUER", "https://sqhsxp.duckdns.org").rstrip("/")

# Host/Origin 허용 목록 — ISSUER 에서 뽑는다. 로그인 폼 action 과 같은 근거로,
# "우리가 어디에 사는가"의 출처를 하나로 둔다.
#
# 왜 필요한가: MCP SDK 는 streamable_http_app(host=...) 이 로컬 주소면 DNS 리바인딩
# 방어를 켜고 allowed_hosts 를 localhost 3개로 고정한다. 기본값이 "127.0.0.1" 이라
# 아무것도 안 넘기면 그 상태가 되고, nginx 가 넘긴 실제 도메인은 421 로 튕긴다.
# 2026-08-22 에 OAuth 를 완주하고도 POST /mcp 가 죽은 원인이 이것이다 (memory/deploy.md).
ISSUER_HOST = urlsplit(ISSUER).netloc          # sqhsxp.duckdns.org
ISSUER_ORIGIN = f"{urlsplit(ISSUER).scheme}://{ISSUER_HOST}"

# Origin 은 없으면 통과다(서버 대 서버 호출). 브라우저에서 오는 것만 검사한다.
# 목록에 없는 Origin 은 403 이고, journal 에 "Invalid Origin header: …" 로 찍힌다 —
# 커넥터가 못 붙는데 401/421 도 아니면 여기부터 봐라.
ALLOWED_ORIGINS = [ISSUER_ORIGIN, "https://claude.ai", "https://claude.com"]
DATA_DIR = Path(os.environ.get("ERION_DATA", "/home/ubuntu/projects/erion/data"))

HOST = os.environ.get("ERION_HOST", "127.0.0.1")
PORT = int(os.environ.get("ERION_PORT", "8788"))

# /authorize 로그인 게이트. 단일 사용자.
AS_PASSWORD = os.environ.get("ERION_AS_PASSWORD", "")
# 구글 캘린더 OAuth 클라이언트 (웹 애플리케이션). 콜백은 <ISSUER>/gcal/callback.
# 비어 있으면 /erion/gcal/start 가 503 으로 안내만 한다.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# /ingest 정적 키. step 3.
INGEST_KEY = os.environ.get("ERION_INGEST_KEY", "")

# 경로
VAULT_DB = DATA_DIR / "erion.db"      # 구조화 레코드 (SPEC §2 의 4테이블)
AUTH_DB = DATA_DIR / "auth.db"        # OAuth 상태 (인프라 — 볼트 데이터 아님, purge 대상 아님)
DOCS_DIR = DATA_DIR / "docs"          # 문서 본문 (material / output / note)

MCP_PATH = "/mcp"

# KST 고정 오프셋. zoneinfo 안 씀 (decide/canvas 와 동일 근거).
KST_OFFSET_HOURS = 9
