#!/usr/bin/env python3
"""구글 캘린더 → 볼트 `block` 테이블 폴러.

`app/gcal_oauth.py` 가 받아둔 refresh token 으로 access token 을 그때그때 받아 쓴다.
access token 은 디스크에 안 남긴다 (1시간짜리라 남길 이유가 없다).

**기본은 primary 캘린더 하나다.** 구글 계정에는 `대한민국의 휴일` 과 Canvas ICS 임포트
피드도 붙어 있는데, 휴일은 블록이 아니고 Canvas 쪽은 `deadline` 과 중복이다.
늘리려면 ERION_GCAL_CALENDARS 에 쉼표로 넣어라.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.environ.get("ERION_DATA", "/home/ubuntu/projects/erion/data"))
TOKEN_FILE = DATA_DIR / "gcal_token.json"
VAULT_DB = DATA_DIR / "erion.db"
CALENDARS = [c.strip() for c in
             os.environ.get("ERION_GCAL_CALENDARS", "primary").split(",") if c.strip()]

PAST_DAYS = 1
FUTURE_DAYS = 30

# 캘린더에 **시작 표시만** 찍혀 있는 일정 — 실제 끝나는 시각을 여기서 덮어쓴다.
# 투썸 알바가 캘린더엔 17:30~18:00(30분)으로 들어오는데 실제 근무는 17:30~24:00 이다
# (2026-09-16 사용자 확인). 안 고치면 가용창 계산이 저녁을 통째로 비어 있다고 본다.
# 제목에 열쇳말이 들어 있으면 그날 그 시각까지로 늘린다. "24:00" 은 다음날 00:00.
SHIFT_UNTIL = {"투썸": "24:00"}


def _access_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit(f"토큰이 없다: {TOKEN_FILE}\n  → https://sqhsxp.duckdns.org/erion/gcal/start 에서 한 번 연결해라")
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    sec = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (cid and sec):
        sys.exit("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 이 환경에 없다 (.env)")
    rt = json.loads(TOKEN_FILE.read_text())["refresh_token"]
    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": sec,
        "refresh_token": rt, "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"토큰 갱신 실패 {e.code} — 권한이 해제됐으면 /erion/gcal/start 부터 다시")


def _events(cal: str, tok: str) -> list[dict]:
    now = datetime.now(KST)
    q = {
        "timeMin": (now - timedelta(days=PAST_DAYS)).isoformat(timespec="seconds"),
        "timeMax": (now + timedelta(days=FUTURE_DAYS)).isoformat(timespec="seconds"),
        "singleEvents": "true",        # 반복 일정을 실제 발생 건으로 펼친다
        "orderBy": "startTime",
        "maxResults": "250",
        "showDeleted": "true",         # 취소된 건 status=cancelled 로 받아서 반영한다
    }
    url = (f"https://www.googleapis.com/calendar/v3/calendars/"
           f"{urllib.parse.quote(cal, safe='')}/events?{urllib.parse.urlencode(q)}")
    out: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = json.loads(r.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"캘린더 {cal} 조회 실패 {e.code}")
        out += page.get("items", [])
        nxt = page.get("nextPageToken")
        url = (f"https://www.googleapis.com/calendar/v3/calendars/"
               f"{urllib.parse.quote(cal, safe='')}/events?"
               f"{urllib.parse.urlencode({**q, 'pageToken': nxt})}") if nxt else ""
    return out


def _when(node: dict) -> tuple[str | None, bool]:
    """(ISO8601 +09:00, 종일인가). 종일은 date 만 와서 시각이 없다."""
    if not node:
        return None, False
    if node.get("dateTime"):
        return datetime.fromisoformat(node["dateTime"]).astimezone(KST).isoformat(
            timespec="seconds"), False
    if node.get("date"):
        d = datetime.fromisoformat(node["date"]).replace(tzinfo=KST)
        return d.isoformat(timespec="seconds"), True
    return None, False


def _shift_end(title: str, start: str, end: str | None) -> tuple[str | None, bool]:
    """(끝시각, 덮어썼나). SHIFT_UNTIL 열쇳말이 제목에 있으면 실제 근무 끝으로 늘린다."""
    for key, until in SHIFT_UNTIL.items():
        if key in title:
            h, m = (int(x) for x in until.split(":"))
            base = datetime.fromisoformat(start).replace(hour=0, minute=0, second=0)
            return (base + timedelta(hours=h, minutes=m)).isoformat(timespec="seconds"), True
    return end, False


def collect(tok: str) -> list[dict]:
    rows = []
    for cal in CALENDARS:
        for e in _events(cal, tok):
            start, allday = _when(e.get("start"))
            end, _ = _when(e.get("end"))
            if not start:
                continue
            title = (e.get("summary") or "(제목 없음)").strip()
            end, fixed = _shift_end(title, start, end)
            rows.append({
                "id": f"gcal:{e['id']}",
                "title": title,
                "start_at": start,
                "end_at": end,
                "location": (e.get("location") or "").strip() or None,
                "source": "gcal",
                "confidence": None,          # gcal 은 추출이 아니라 사실이다 (schema.sql)
                "status": "cancelled" if e.get("status") == "cancelled" else "confirmed",
                "_allday": allday,
                "_fixed": fixed,
            })
    return rows


def write(rows: list[dict]) -> tuple[int, int]:
    ts = datetime.now(KST).isoformat(timespec="seconds")
    conn = sqlite3.connect(VAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        before = {r["id"] for r in conn.execute("SELECT id FROM block WHERE id LIKE 'gcal:%'")}
        ins = upd = 0
        for r in rows:
            (upd, ins) = (upd + 1, ins) if r["id"] in before else (upd, ins + 1)
            conn.execute(
                "INSERT INTO block (id,title,start_at,end_at,location,source,confidence,status,updated_at) "
                "VALUES (:id,:title,:start_at,:end_at,:location,:source,:confidence,:status,:ts) "
                "ON CONFLICT(id) DO UPDATE SET title=:title,start_at=:start_at,end_at=:end_at,"
                "location=:location,source=:source,confidence=:confidence,status=:status,updated_at=:ts",
                {k: v for k, v in r.items() if not k.startswith("_")} | {"ts": ts})
        conn.commit()
        return ins, upd
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="구글 캘린더 → 볼트 block 폴러")
    ap.add_argument("--dry", action="store_true", help="DB 에 안 쓰고 보여주기만")
    args = ap.parse_args()

    rows = collect(_access_token())
    rows.sort(key=lambda r: r["start_at"])
    allday = sum(1 for r in rows if r["_allday"])
    print(f"[gcal] {', '.join(CALENDARS)} → {len(rows)}건 (종일 {allday}건)")
    for r in rows[:40]:
        mark = ("x" if r["status"] == "cancelled"
                else "D" if r["_allday"] else "*" if r["_fixed"] else " ")
        loc = f"  @{r['location'][:20]}" if r["location"] else ""
        print(f"  {mark} {r['start_at'][:16]}  {r['title'][:40]}{loc}")
    if len(rows) > 40:
        print(f"  … 외 {len(rows)-40}건")

    if args.dry:
        print("[gcal] --dry 라 DB 에 안 썼다")
        return 0
    ins, upd = write(rows)
    print(f"[gcal] 신규 {ins} · 갱신 {upd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
