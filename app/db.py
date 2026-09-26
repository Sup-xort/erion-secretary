"""SQLite 접근. 볼트 DB(구조화 레코드)와 auth DB(OAuth 상태)를 분리한다.

분리 이유: purge 는 볼트 데이터를 지우는 것이지 OAuth 세션까지 날리는 게 아니다.
auth 테이블을 erion.db 에 섞으면 purge 설계가 지저분해진다.
"""

import sqlite3
from pathlib import Path

from . import config

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# OAuth 상태 테이블 — 인프라. SPEC §2 의 볼트 4테이블과 별개다.
_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_client (
  client_id     TEXT PRIMARY KEY,
  data          TEXT NOT NULL,       -- OAuthClientInformationFull JSON
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_code (
  code          TEXT PRIMARY KEY,
  data          TEXT NOT NULL,       -- AuthorizationCode JSON
  expires_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_token (
  token         TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,       -- access | refresh
  data          TEXT NOT NULL,       -- AccessToken/RefreshToken JSON
  expires_at    REAL                 -- NULL = 무기한
);
-- authorize -> 로그인 페이지로 넘길 때 파라미터를 잠깐 보관하는 티켓
CREATE TABLE IF NOT EXISTS oauth_pending (
  ticket        TEXT PRIMARY KEY,
  data          TEXT NOT NULL,       -- {client_id, params...} JSON
  expires_at    REAL NOT NULL
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def vault() -> sqlite3.Connection:
    conn = _connect(config.VAULT_DB)
    conn.executescript(_SCHEMA)
    return conn


def auth() -> sqlite3.Connection:
    conn = _connect(config.AUTH_DB)
    conn.executescript(_AUTH_SCHEMA)
    return conn


# 이미 있는 테이블에 나중에 붙인 칼럼. CREATE IF NOT EXISTS 는 칼럼을 안 늘리므로
# 여기서 빠진 것만 ALTER 한다. 칼럼을 schema.sql 에 추가했으면 여기에도 적어라.
_ADDED_COLUMNS = {
    "deadline": [
        ("description", "TEXT"), ("sub_types", "TEXT"), ("url", "TEXT"), ("label", "TEXT"),
        ("est_hours", "REAL"), ("prep_note", "TEXT"), ("est_model", "TEXT"),
        ("est_basis", "TEXT"), ("est_at", "TEXT"), ("actual_hours", "REAL"),
    ],
    "item": [("target_at", "TEXT")],
    "push_log": [("url", "TEXT")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()


def init() -> None:
    """디렉터리 + 스키마 + docs 트리 생성. 서비스 시작 시 한 번."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("material", "output", "note"):
        (config.DOCS_DIR / sub).mkdir(parents=True, exist_ok=True)
    conn = vault()
    _migrate(conn)
    conn.close()
    auth().close()
