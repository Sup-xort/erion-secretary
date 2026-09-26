"""연결 상태 한 줄 (2026-09-26) — 폴러가 끝날 때마다 `source_status` 에 마지막 시도·성공을 적는다.
대시보드 톱니의 "연결 상태" 가 이걸 읽는다. 토큰이 죽어도 옛 데이터만 조용히 보이던 문제 때문이다.

폴러는 app 을 import 하지 않는다(가볍게, 따로 죽게). 그래서 스키마 파일만 같이 읽어
새 테이블이 서비스 재기동 전에도 있게 한다.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
VAULT_DB = Path(os.environ.get(
    "ERION_DATA", "/home/ubuntu/projects/erion/data")) / "erion.db"
SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text(encoding="utf-8")


def mark(source: str, err: BaseException | None = None, note: str | None = None) -> None:
    """err 없으면 성공. 여기서 실패해도 폴러 본업을 망치지 않는다."""
    ts = datetime.now(KST).isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(VAULT_DB)
        try:
            conn.executescript(SCHEMA)
            if err is None:
                conn.execute("INSERT INTO source_status (source,last_try,last_ok,err,note) VALUES (?,?,?,NULL,?) "
                             "ON CONFLICT(source) DO UPDATE SET last_try=excluded.last_try,"
                             "last_ok=excluded.last_ok,err=NULL,note=excluded.note", [source, ts, ts, note])
            else:
                msg = (str(err) or type(err).__name__)[:200]
                conn.execute("INSERT INTO source_status (source,last_try,err) VALUES (?,?,?) "
                             "ON CONFLICT(source) DO UPDATE SET last_try=excluded.last_try,err=excluded.err",
                             [source, ts, msg])
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"[{source}] 연결 상태를 못 적음: {e}", file=sys.stderr)
