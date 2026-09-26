#!/usr/bin/env python3
"""Canvas → 볼트 `deadline` 테이블 폴러.

싼 층이다. 모델이 안 들어간다. 규칙과 날짜계산만 한다 (decisions.md 아키텍처).

동작:
  활성 과목 목록 → 과목마다 assignments(+submission) → due_at 이 창 안이면 upsert.
  창 안인데 이번 회차에 안 보인 canvas:* 행은 지운다 (Canvas 에서 삭제된 과제).

쓰는 것은 `deadline` 뿐이다. 다른 테이블은 건드리지 않는다.
"""

import argparse
import html
import json
import re
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
BASE = os.environ.get("ERION_CANVAS_BASE", "https://canvas.skku.edu") + "/api/v1"
TOKEN_FILE = Path(os.environ.get(
    "ERION_CANVAS_TOKEN_FILE", str(Path.home() / ".erion/canvas_token")))
VAULT_DB = Path(os.environ.get(
    "ERION_DATA", "/home/ubuntu/projects/erion/data")) / "erion.db"

# 법정의무교육류 — 마감이 있어도 판단 대상이 아니다. id 로 빼는 이유는 제목이 해마다 바뀌어서다.
EXCLUDE_COURSES = {
    79157,   # [학생] 2026 학생을 위한 폭력예방교육(법정의무교육)
    79160,   # 2026 Online Education for Human Rights ...
    79162,   # 2026 学生校园暴力预防教育
}

# 창: 지난 것도 조금 남겨둔다(미제출 확인용), 미래는 넉넉히.
PAST_DAYS = 7
FUTURE_DAYS = 90


def _token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit(f"토큰 파일이 없다: {TOKEN_FILE}")
    tok = TOKEN_FILE.read_text().strip()
    if not tok:
        sys.exit(f"토큰 파일이 비어 있다: {TOKEN_FILE}")
    return tok


def _get(path: str, tok: str) -> list:
    """페이지네이션을 따라가며 전부 모은다. Canvas 의 while(1); 를 벗긴다."""
    url = BASE + path
    out: list = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
                link = r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Canvas {e.code} at {path}") from e
        if body.startswith("while(1);"):
            body = body[len("while(1);"):]
        page = json.loads(body)
        if not isinstance(page, list):
            return page
        out += page
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return out


def _to_kst(iso_z: str | None) -> str | None:
    """Canvas 는 UTC(Z)로 준다. 볼트는 +09:00 로 저장한다 (schema.sql)."""
    if not iso_z:
        return None
    dt = datetime.fromisoformat(iso_z.replace("Z", "+00:00"))
    return dt.astimezone(KST).isoformat(timespec="seconds")


def _submitted(sub: dict | None) -> bool:
    if not sub:
        return False
    if sub.get("submitted_at"):
        return True
    return sub.get("workflow_state") in ("submitted", "graded", "pending_review")


# 설명은 추정 배치(estimate.py)의 재료다. 모델에 넣을 거라 길이를 자른다 — 첨부 목록이
# 수십 줄씩 붙은 과제도 있는데, 소요시간 감을 잡는 데는 앞부분이면 충분하다.
DESC_MAX = 4000


def _plain(h: str | None) -> str | None:
    """Canvas description(HTML) → 텍스트. 블록 태그는 줄바꿈으로, 나머지는 벗긴다."""
    if not h:
        return None
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", h)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:DESC_MAX] or None


def _courses(tok: str) -> list[dict]:
    """**즐겨찾기가 이번 학기다.** `enrollment_state=active` 는 지난 학기·계절특강까지
    18개를 돌려준다 — 그걸 쓰면 끝난 과목의 마감이 섞인다. 즐겨찾기는 사용자가 직접
    학기마다 정리하는 목록이라 이쪽이 정확하다 (2026-09-11 사용자 확인).

    학기가 바뀌었는데 즐겨찾기를 안 고쳤다면 이 폴러도 옛 과목을 본다. 그건 사용자가
    Canvas 에서 별을 옮기면 그대로 따라온다 — 여기 하드코딩할 값이 아니다.
    """
    fav = _get("/users/self/favorites/courses?per_page=100", tok)
    if fav:
        return fav
    print("[canvas] 즐겨찾기가 비어 있다 — active 전체로 넘어간다", file=sys.stderr)
    return _get("/courses?enrollment_state=active&per_page=100", tok)


def collect(tok: str) -> list[dict]:
    now = datetime.now(KST)
    lo, hi = now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)
    rows = []
    courses = _courses(tok)
    for c in courses:
        cid = c.get("id")
        if cid in EXCLUDE_COURSES:
            continue
        name = (c.get("name") or "").strip()
        # 과목명이 "논리회로_ICE2001_42(조건희)" 꼴이라 앞부분만 쓴다. 한 줄 출력에 통째로
        # 넣으면 읽을 수가 없다 (decisions.md: 출력은 언제나 한 줄).
        short = name.split("_")[0].strip() or name
        for a in _get(f"/courses/{cid}/assignments?per_page=100&include[]=submission", tok):
            if not a.get("published"):
                continue
            due = _to_kst(a.get("due_at"))
            if not due:
                continue
            when = datetime.fromisoformat(due)
            if not (lo <= when <= hi):
                continue
            rows.append({
                "id": f"canvas:{cid}:{a['id']}",
                "title": (a.get("name") or "").strip(),
                "course": short,
                "due_at": due,
                "due_approx": 0,
                "points": a.get("points_possible"),
                "submitted": 1 if _submitted(a.get("submission")) else 0,
                "description": _plain(a.get("description")),
                "sub_types": ",".join(a.get("submission_types") or []) or None,
                "url": a.get("html_url"),
            })
    return rows


def write(rows: list[dict], prune: bool) -> tuple[int, int, int]:
    now_iso = datetime.now(KST).isoformat(timespec="seconds")
    conn = sqlite3.connect(VAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        before = {r["id"]: dict(r) for r in
                  conn.execute("SELECT * FROM deadline WHERE id LIKE 'canvas:%'")}
        ins = upd = 0
        for r in rows:
            old = before.get(r["id"])
            if old is None:
                ins += 1
            elif any(old[k] != r[k] for k in
                     ("title", "course", "due_at", "points", "submitted",
                      "description", "sub_types", "url")):
                upd += 1
            conn.execute(
                "INSERT INTO deadline (id,title,course,due_at,due_approx,points,submitted,"
                "description,sub_types,url,updated_at) "
                "VALUES (:id,:title,:course,:due_at,:due_approx,:points,:submitted,"
                ":description,:sub_types,:url,:ts) "
                "ON CONFLICT(id) DO UPDATE SET title=:title,course=:course,due_at=:due_at,"
                "due_approx=:due_approx,points=:points,submitted=:submitted,"
                "description=:description,sub_types=:sub_types,url=:url,updated_at=:ts",
                # est_* · label 은 여기서 안 건드린다 — 추정 배치(estimate.py)와 챗의 몫이다.
                {**r, "ts": now_iso})
        # Canvas 에서 사라진 과제 정리. 창 밖의 옛 행은 건드리지 않는다.
        gone = 0
        if prune:
            seen = {r["id"] for r in rows}
            now = datetime.now(KST)
            lo = (now - timedelta(days=PAST_DAYS)).isoformat(timespec="seconds")
            hi = (now + timedelta(days=FUTURE_DAYS)).isoformat(timespec="seconds")
            for r in conn.execute(
                    "SELECT id FROM deadline WHERE id LIKE 'canvas:%' "
                    "AND due_at >= ? AND due_at <= ?", [lo, hi]):
                if r["id"] not in seen:
                    conn.execute("DELETE FROM deadline WHERE id=?", [r["id"]])
                    gone += 1
        conn.commit()
        return ins, upd, gone
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Canvas → 볼트 deadline 폴러")
    ap.add_argument("--dry", action="store_true", help="DB 에 안 쓰고 무엇이 바뀔지만 보여준다")
    ap.add_argument("--no-prune", action="store_true", help="사라진 과제를 지우지 않는다")
    args = ap.parse_args()

    rows = collect(_token())
    rows.sort(key=lambda r: r["due_at"])
    pend = [r for r in rows if not r["submitted"]]
    print(f"[canvas] 수집 {len(rows)}건 (미제출 {len(pend)}건)")
    for r in rows[:40]:
        mark = " " if r["submitted"] else "*"
        pts = "" if r["points"] is None else f" {r['points']:g}점"
        print(f"  {mark} {r['due_at'][:16]}  {r['course'][:18]:18} {r['title'][:40]}{pts}")
    if len(rows) > 40:
        print(f"  … 외 {len(rows)-40}건")

    if args.dry:
        print("[canvas] --dry 라 DB 에 안 썼다")
        return 0
    ins, upd, gone = write(rows, prune=not args.no_prune)
    print(f"[canvas] 신규 {ins} · 갱신 {upd} · 삭제 {gone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
