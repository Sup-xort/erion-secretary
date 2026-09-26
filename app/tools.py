"""MCP 읽기 도구 구현 (SPEC §3).

원칙: 모든 응답에 행 상한과 고정 필드가 있다. 통짜 덤프 없음.
여기 함수는 순수하다 — 커넥션을 받아 캡된 dict/list 만 돌려준다.
MCP 배선(데코레이터·인증)은 main.py 에 있다.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

KST = timezone(timedelta(hours=config.KST_OFFSET_HOURS))

# 상한 — SPEC §3 표
CAP_DEADLINES = 20
CAP_BLOCKS = 50
CAP_DOCS_SEARCH = 10
CAP_BOARD = 40
CAP_EST_HISTORY = 30
CAP_DONE = 30
ITEM_KINDS = ("task", "video")
DOC_READ_MAX_BYTES = 40 * 1024


def _now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def deadlines(conn: sqlite3.Connection, within_days: int = 14,
              include_submitted: bool = False) -> list[dict]:
    """다가오는 마감. within_days 안, 기본은 미제출만. 최대 20행."""
    now = datetime.now(KST)
    lo = now.isoformat(timespec="seconds")
    hi = (now + timedelta(days=within_days)).isoformat(timespec="seconds")
    sql = ("SELECT id,title,label,course,due_at,due_approx,points,est_hours,prep_note,url "
           "FROM deadline WHERE due_at IS NOT NULL AND due_at >= ? AND due_at <= ?")
    args: list = [lo, hi]
    if not include_submitted:
        # 손으로 찍은 완료도 미제출이 아니다 — Canvas 가 뭐라 하든 (done_mark)
        sql += " AND submitted = 0 AND id NOT IN (SELECT id FROM done_mark)"
    sql += " ORDER BY due_at ASC LIMIT ?"
    args.append(CAP_DEADLINES)
    rows = conn.execute(sql, args).fetchall()
    return [{
        "id": r["id"], "label": r["label"] or r["title"], "title": r["title"],
        "course": r["course"], "due_at": r["due_at"], "due_approx": bool(r["due_approx"]),
        "points": r["points"], "est_hours": r["est_hours"], "prep_note": r["prep_note"],
        "url": r["url"],
    } for r in rows]


def blocks(conn: sqlite3.Connection, frm: str, to: str, limit: int = CAP_BLOCKS) -> list[dict]:
    """[frm, to) 구간의 시각 블록. 최대 50행. 취소된 것은 뺀다.

    limit: MCP 는 50 그대로. 대시보드 달력·주간 계획(planner.py)은 넉넉히 받는다."""
    rows = conn.execute(
        "SELECT id,title,start_at,end_at,location,status FROM block "
        "WHERE start_at >= ? AND start_at < ? AND status != 'cancelled' "
        "ORDER BY start_at ASC LIMIT ?",
        [frm, to, limit],
    ).fetchall()
    return [{
        "id": r["id"], "title": r["title"], "start_at": r["start_at"],
        "end_at": r["end_at"], "location": r["location"], "status": r["status"],
    } for r in rows]


def _safe_doc_path(doc_id: str) -> Path:
    """doc.id 슬러그를 docs/ 아래 실제 경로로. 경로 이탈 차단."""
    p = (config.DOCS_DIR / doc_id).resolve()
    root = config.DOCS_DIR.resolve()
    if root not in p.parents and p != root:
        raise ValueError("경로 이탈")
    return p


def docs_search(conn: sqlite3.Connection, q: str, kind: str | None = None,
                limit: int = CAP_DOCS_SEARCH) -> list[dict]:
    """제목 부분일치 검색. 최대 10행. 발췌는 본문 앞 200자."""
    limit = min(limit, CAP_DOCS_SEARCH)
    sql = "SELECT id,title,kind,rel_deadline FROM doc WHERE title LIKE ?"
    args: list = [f"%{q}%"]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args).fetchall():
        excerpt = ""
        try:
            body = _safe_doc_path(r["id"]).read_text(encoding="utf-8")
            excerpt = body[:200]
        except (OSError, ValueError):
            excerpt = ""
        out.append({"id": r["id"], "title": r["title"], "kind": r["kind"],
                    "excerpt": excerpt})
    return out


def doc_read(conn: sqlite3.Connection, doc_id: str) -> dict:
    """문서 본문. 40KB 상한, 초과 시 자르고 truncated=true."""
    row = conn.execute("SELECT id,title,kind FROM doc WHERE id = ?", [doc_id]).fetchone()
    if not row:
        return {"id": doc_id, "found": False}
    try:
        raw = _safe_doc_path(doc_id).read_bytes()
    except (OSError, ValueError):
        return {"id": doc_id, "found": False}
    truncated = len(raw) > DOC_READ_MAX_BYTES
    body = raw[:DOC_READ_MAX_BYTES].decode("utf-8", errors="replace")
    return {"id": doc_id, "title": row["title"], "kind": row["kind"],
            "found": True, "truncated": truncated, "content": body}


# ── 대시보드 블록 (2026-09-18) ──────────────────────────────────────────────
# 블록 = Canvas 과제(deadline) + 할 일·영상(item). 대시보드(web.py)와 MCP 가 같은 함수를 쓴다.

def _norm_due(due: str | None) -> str | None:
    """"2026-09-20" · "2026-09-20T18:00" · "…+09:00" → ISO8601 +09:00. 날짜만이면 23:59."""
    if not due:
        return None
    d = due.strip()
    if len(d) == 10:
        d += "T23:59:00"
    dt = datetime.fromisoformat(d)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).isoformat(timespec="seconds")


def board(conn: sqlite3.Connection, within_days: int = 14, include_done: bool = False,
          past_days: int = 3, limit: int | None = None) -> list[dict]:
    """대시보드 블록 전부 — 과제·할 일·영상, 마감순. 마감 없는 할 일은 맨 뒤. 기본 40행.

    past_days: 마감이 지났어도 이만큼은 남긴다(미제출 확인용).
    limit: MCP 는 상한 40(CAP_BOARD)을 쓰지만 /erion/web 의 달력은 학기 전체가 필요하다."""
    now = datetime.now(KST)
    lo = (now - timedelta(days=past_days)).isoformat(timespec="seconds")
    hi = (now + timedelta(days=within_days)).isoformat(timespec="seconds")
    out: list[dict] = []
    marks = done_ids(conn)          # 손으로 찍은 완료. Canvas 의 submitted 와 별개다
    sql = ("SELECT * FROM deadline WHERE due_at IS NOT NULL AND due_at >= ? AND due_at <= ?"
           + ("" if include_done else " AND submitted = 0"))
    for r in conn.execute(sql, [lo, hi]):
        manual = r["id"] in marks
        out.append({
            "id": r["id"], "kind": "assignment", "label": r["label"] or r["title"],
            "title": r["title"], "course": r["course"], "due_at": r["due_at"], "target_at": None,
            "points": r["points"], "est_hours": r["est_hours"], "prep_note": r["prep_note"],
            "est_basis": r["est_basis"], "actual_hours": r["actual_hours"],
            "description": r["description"], "url": r["url"],
            "sub_types": r["sub_types"], "done": bool(r["submitted"]) or manual,
            "done_manual": manual,
        })
    # due_at = 진짜 마감, target_at = 스스로 정한 날. 어느 쪽이든 창 안에 들면 싣는다.
    sql = ("SELECT * FROM item WHERE ((due_at IS NULL AND target_at IS NULL)"
           " OR (due_at >= ? AND due_at <= ?) OR (target_at >= ? AND target_at <= ?))"
           + ("" if include_done else " AND status = 'todo'"))
    for r in conn.execute(sql, [lo, hi, lo, hi]):
        out.append({
            "id": r["id"], "kind": r["kind"], "label": r["label"], "title": r["label"],
            "course": r["course"], "due_at": r["due_at"], "target_at": r["target_at"],
            "points": None,
            "est_hours": r["est_hours"], "prep_note": r["prep_note"], "est_basis": None,
            "actual_hours": None, "description": r["description"], "url": r["url"],
            "sub_types": None, "done": r["status"] == "done",
            "done_manual": r["status"] == "done",
        })
    if not include_done:
        # SQL 이 걸러낸 건 Canvas 제출·item status 까지다. done_mark 는 여기서 뺀다.
        out = [b for b in out if not b["done"]]
    out.sort(key=lambda b: (not (b["due_at"] or b["target_at"]), b["due_at"] or b["target_at"] or ""))
    return out[:limit or CAP_BOARD]


def board_brief(conn: sqlite3.Connection, within_days: int = 14,
                include_done: bool = False) -> list[dict]:
    """MCP 용 — 설명 본문을 빼고 짧게. 본문은 대시보드와 canvas_read 가 맡는다."""
    keep = ("id", "kind", "label", "course", "due_at", "target_at", "points", "est_hours",
            "prep_note", "url", "done")
    return [{k: b[k] for k in keep} for b in board(conn, within_days, include_done)]


def item_save(conn: sqlite3.Connection, *, id: str | None = None, kind: str | None = None,
              label: str | None = None, course: str | None = None, due_at: str | None = None,
              est_hours: float | None = None, prep_note: str | None = None,
              description: str | None = None, url: str | None = None,
              target_at: str | None = None) -> dict:
    """블록 만들기/고치기. id 없으면 새 할 일·영상. id 가 canvas:* 면 과제의 label·prep_note 만 고친다
    (나머지는 Canvas 가 원본이라 폴러가 덮어쓴다). 넘긴 필드만 바뀐다.

    due_at = 밖에서 정해진 진짜 마감, target_at = 사용자가 스스로 정한 날. 둘 다 "none" 을
    넘기면 비운다 (잘못 넣은 날짜를 지우는 길)."""
    ts = _now_iso()
    if id and id.startswith("canvas:"):
        row = conn.execute("SELECT id FROM deadline WHERE id = ?", [id]).fetchone()
        if not row:
            return {"ok": False, "error": f"그런 과제 없음: {id}"}
        sets = {k: v for k, v in (("label", label), ("prep_note", prep_note)) if v is not None}
        if not sets:
            return {"ok": False, "error": "Canvas 과제는 label · prep_note 만 고칠 수 있다. "
                                          "추정시간은 est_adjust 로."}
        conn.execute(f"UPDATE deadline SET {', '.join(f'{k}=?' for k in sets)} WHERE id = ?",
                     [*sets.values(), id])
        conn.commit()
        return {"ok": True, "id": id, "updated": list(sets)}

    if kind is not None and kind not in ITEM_KINDS:
        return {"ok": False, "error": f"kind 는 {ITEM_KINDS} 중 하나"}
    clear = [k for k, v in (("due_at", due_at), ("target_at", target_at))
             if v is not None and v.strip().lower() in ("none", "null")]
    due = _norm_due(due_at) if due_at and "due_at" not in clear else None
    tgt = _norm_due(target_at) if target_at and "target_at" not in clear else None
    fields = {"kind": kind, "label": label, "course": course, "due_at": due, "target_at": tgt,
              "est_hours": est_hours, "prep_note": prep_note, "description": description, "url": url}
    if id:
        if not conn.execute("SELECT 1 FROM item WHERE id = ?", [id]).fetchone():
            return {"ok": False, "error": f"그런 블록 없음: {id}"}
        sets = {k: v for k, v in fields.items() if v is not None}
        sets.update({k: None for k in clear})
        if not sets:
            return {"ok": False, "error": "바꿀 필드가 없다"}
        conn.execute(f"UPDATE item SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?",
                     [*sets.values(), ts, id])
        conn.commit()
        return {"ok": True, "id": id, "updated": list(sets)}

    if not label or not kind:
        return {"ok": False, "error": "새 블록엔 kind 와 label 이 필요하다"}
    new_id = "item:" + os.urandom(4).hex()
    conn.execute(
        "INSERT INTO item (id,kind,label,course,due_at,target_at,est_hours,prep_note,description,url,"
        "status,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'todo','claude',?,?)",
        [new_id, kind, label, course, due, tgt, est_hours, prep_note, description, url, ts, ts])
    conn.commit()
    return {"ok": True, "id": new_id, "created": True}


def done_ids(conn: sqlite3.Connection) -> set[str]:
    """손으로 찍은 완료 표시의 id 집합 (done_mark). 과제·강의영상만 들어 있다."""
    return {r[0] for r in conn.execute("SELECT id FROM done_mark")}


def mark_done(conn: sqlite3.Connection, id: str, done: bool = True, *,
              label: str | None = None, course: str | None = None,
              due_at: str | None = None) -> dict:
    """완료 찍기/되돌리기. **Canvas·LearningX 가 뭐라 하든 사람 말이 이긴다.**

    id 앞머리로 갈라진다 — 원본을 누가 쥐고 있느냐가 다르기 때문이다:
      item:*    할 일·영상. 볼트가 원본이라 status 칸을 직접 바꾼다.
      canvas:*  Canvas 과제. submitted 는 30분마다 폴러가 덮어쓴다 → done_mark 에 적는다.
      lx:*      강의영상. DB 에 행 자체가 없다(실시간 조회) → done_mark 에 적는다.

    done_mark 에는 그때의 이름·과목·마감을 같이 남긴다. 원본이 목록에서 빠져도
    "완료함" 줄에 보여주고 되돌릴 수 있어야 해서다.
    """
    if id.startswith("item:"):
        cur = conn.execute("UPDATE item SET status=?, updated_at=? WHERE id=?",
                           ["done" if done else "todo", _now_iso(), id])
        conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "error": f"그런 블록 없음: {id}"}
        return {"ok": True, "id": id, "done": done, "status": "done" if done else "todo"}

    if id.startswith("canvas:"):
        kind = "assignment"
        r = conn.execute("SELECT label,title,course,due_at FROM deadline WHERE id=?",
                         [id]).fetchone()
        if r:
            label = label or r["label"] or r["title"]
            course = course or r["course"]
            due_at = due_at or r["due_at"]
        elif not done:
            pass                     # 되돌리기는 원본이 없어도 된다
        elif label is None:
            return {"ok": False, "error": f"그런 과제 없음: {id}"}
    elif id.startswith("lx:"):
        kind = "lecture"
    else:
        return {"ok": False, "error": f"알 수 없는 id: {id}"}

    if done:
        conn.execute(
            "INSERT INTO done_mark (id,kind,label,course,due_at,at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET label=COALESCE(excluded.label,label), "
            "course=COALESCE(excluded.course,course), due_at=COALESCE(excluded.due_at,due_at), "
            "at=excluded.at",
            [id, kind, label, course, due_at, _now_iso()])
    else:
        conn.execute("DELETE FROM done_mark WHERE id=?", [id])
    conn.commit()
    return {"ok": True, "id": id, "done": done}


def item_done(conn: sqlite3.Connection, id: str, done: bool = True) -> dict:
    """MCP 쪽 이름. 2026-09-20 부터 과제·강의영상도 받는다 (mark_done 참조)."""
    return mark_done(conn, id, done)


def _lx_open(lti: str) -> str | None:
    from . import learningx
    try:
        return learningx.open_url(lti)
    except Exception:                           # noqa: BLE001
        return None


def done_recent(conn: sqlite3.Connection, limit: int = CAP_DONE) -> list[dict]:
    """최근에 완료로 찍은 것. 대시보드가 "완료함" 줄에 깔고 되돌리기를 건다.

    두 곳에서 모은다 — done_mark(과제·강의영상)와 item(할 일·영상의 status).
    """
    out = [{"id": r["id"], "kind": r["kind"], "label": r["label"] or r["id"],
            "title": r["label"] or r["id"], "course": r["course"], "due_at": r["due_at"],
            "done_at": r["at"], "done": True, "done_manual": True,
            "points": None, "est_hours": None, "prep_note": None, "est_basis": None,
            "actual_hours": None, "description": None, "sub_types": None,
            # 강의영상 id 는 lx:<LTI 주소> 다. 그 주소는 바로 안 열려서 Canvas 항목 페이지로 바꿔 준다
            "url": _lx_open(r["id"][3:]) if r["kind"] == "lecture" else None}
           for r in conn.execute("SELECT * FROM done_mark ORDER BY at DESC LIMIT ?", [limit])]
    out += [{"id": r["id"], "kind": r["kind"], "label": r["label"], "title": r["label"],
             "course": r["course"], "due_at": r["due_at"], "done_at": r["updated_at"],
             "done": True, "done_manual": True, "points": None, "est_hours": r["est_hours"],
             "prep_note": r["prep_note"], "est_basis": None, "actual_hours": None,
             "description": r["description"], "url": r["url"], "sub_types": None}
            for r in conn.execute("SELECT * FROM item WHERE status='done' "
                                  "ORDER BY updated_at DESC LIMIT ?", [limit])]
    out.sort(key=lambda b: b["done_at"] or "", reverse=True)
    return out[:limit]


def est_adjust(conn: sqlite3.Connection, id: str, hours: float, reason: str | None = None,
               actual: bool = False) -> dict:
    """추정 보정. actual=False → 추정값을 고친다 / True → 실제 걸린 시간을 남긴다.
    어느 쪽이든 est_feedback 에 한 줄 쌓인다 — 다음 추정(estimate.py)이 이걸 참고한다."""
    if not (0 < hours <= 100):
        return {"ok": False, "error": "hours 는 0~100"}
    if id.startswith("canvas:"):
        r = conn.execute("SELECT label,title,course,sub_types,est_hours,actual_hours FROM deadline "
                         "WHERE id=?", [id]).fetchone()
        kind = "assignment"
    else:
        r = conn.execute("SELECT label,course,kind,est_hours FROM item WHERE id=?", [id]).fetchone()
        kind = r["kind"] if r else None
    if not r:
        return {"ok": False, "error": f"그런 블록 없음: {id}"}
    old = r["est_hours"]
    if kind == "assignment":
        if actual:
            conn.execute("UPDATE deadline SET actual_hours=? WHERE id=?", [hours, id])
        else:
            conn.execute("UPDATE deadline SET est_hours=?, est_model='user', est_basis='user', est_at=? "
                         "WHERE id=?", [hours, _now_iso(), id])
    elif not actual:
        conn.execute("UPDATE item SET est_hours=?, updated_at=? WHERE id=?", [hours, _now_iso(), id])
    conn.execute(
        "INSERT INTO est_feedback (target,label,course,kind,sub_types,old_hours,new_hours,actual,reason,at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [id, r["label"] or (r["title"] if kind == "assignment" else None), r["course"], kind,
         r["sub_types"] if kind == "assignment" else None, old, hours, int(actual), reason, _now_iso()])
    conn.commit()
    return {"ok": True, "id": id, "old_hours": old, "new_hours": hours, "actual": actual}


def est_history(conn: sqlite3.Connection, course: str | None = None,
                limit: int = CAP_EST_HISTORY) -> list[dict]:
    sql = "SELECT * FROM est_feedback"
    args: list = []
    if course:
        sql += " WHERE course LIKE ?"
        args.append(f"%{course}%")
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(min(limit, CAP_EST_HISTORY))
    return [{"target": r["target"], "label": r["label"], "course": r["course"], "kind": r["kind"],
             "old_hours": r["old_hours"], "new_hours": r["new_hours"], "actual": bool(r["actual"]),
             "reason": r["reason"], "at": r["at"]} for r in conn.execute(sql, args)]
