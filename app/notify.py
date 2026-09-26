"""알림 판단 (2026-09-23). systemd 타이머가 10분마다 `python -m app.notify` 로 부른다.

사용자 요청: "마감 전에도 알려줘야 하고, 어떤 일정이 있어서 못 하는 날은 그 전날에도 보내야 해.
적어도 오늘까지는 끝내야 해요! 이런 알림."

보내는 것 세 가지. 전부 push_log 의 key 로 한 번만 보낸다.
  1. 아침 (MORNING 시) — 오늘 할 것. **오늘 안 하면 마감을 못 지키는 몫**이 있으면 그걸 앞에 세운다.
     내일이 일정으로 막혀 있으면 그 이유를 같이 적는다 ("내일 투썸 알바 17:30–24:00").
  2. 저녁 (EVENING 시) — 그 "오늘 최소 몫" 중 아직 완료를 안 찍은 게 있으면 한 번 더.
  3. 마감 전 — DUE_BEFORE 시간 전에 한 번씩. 같은 회차에 걸린 건 한 알림으로 묶는다.
     강의영상(LearningX 출석)은 DUE_BEFORE_LEC — 한 번 더 일찍. 놓치면 출석이 날아간다.
  4. 강의영상 열림 (2026-09-25) — 인정기간이 시작되면 한 번. "언제까지 봐야 출석인지" 를 같이.

내가 챗으로 추가한 블록의 목표일(item.target_at)은 마감이 아니다 — 여기 어디에도 안 낀다.

"오늘 최소 몫" = planner.compute(skip_today=True) 가 새로 `short` 에 올리는 양.
오늘을 통째로 비웠을 때 마감 안에 안 들어가게 되는 시간이다. 내일·모레가 알바나 종일
일정으로 막혀 있으면 여기서 자연히 커진다 — 막힌 날을 따로 세지 않아도 된다.

진도는 모른다(완료만 안다). 반쯤 했어도 저녁 알림은 온다 — 완료를 찍으면 안 온다.
QUIET 시간대에는 아무것도 안 보낸다. 창 안에 있는 건 조용한 시간이 끝나면 나간다.
"""

import json
import sys
import time
from datetime import datetime, timedelta

from . import config, db, planner, push, tools

KST = tools.KST
MORNING = 9                       # 아침 알림 (시). 이 시각부터 MORNING_UNTIL 전까지 한 번
MORNING_UNTIL = 12                # 서버가 오전 내내 꺼져 있었으면 그날 아침 알림은 건너뛴다
EVENING = 21
EVENING_UNTIL = 24
DUE_BEFORE = (24, 3)              # 마감 몇 시간 전에 알릴지
DUE_BEFORE_LEC = (48, 24, 3)      # 강의영상 출석 인정 끝 몇 시간 전에 알릴지
LEC_OPEN_FRESH = 48               # 열린 지 이 시간 안이면 "열림" 알림 (배포 첫날 옛것을 쏟지 않게)
QUIET = (1, 8)                    # 01:00–08:00 에는 안 보낸다
BLOCKED_CAP = 1.0                 # 가용시간이 이보다 적은 날 = "못 하는 날"
LEC_CACHE = config.DATA_DIR / "notify_lectures.json"
LEC_CACHE_SEC = 3600              # LearningX 는 한 시간에 한 번만 (토큰 발급이 매번 요청 1번이다)
KEEP_LOG_DAYS = 45


def _lectures(marks: set[str]) -> list[dict]:
    """강의영상 블록. 한 시간 캐시 — 10분마다 LearningX 를 두드리지 않는다."""
    try:
        c = json.loads(LEC_CACHE.read_text(encoding="utf-8"))
        if time.time() - c["at"] < LEC_CACHE_SEC:
            return [b for b in c["lec"] if b["id"] not in marks]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    from . import web                   # starlette 까지 끌고 오므로 필요할 때만
    lec, _seen, err = web._lectures(set())
    if err:
        # 출석 알림은 조용히 빠지면 안 된다 — 오래된 캐시라도 있으면 그걸로 간다.
        print(f"lectures: {err} — 캐시로 대신함", flush=True)
        try:
            c = json.loads(LEC_CACHE.read_text(encoding="utf-8"))
            return [b for b in c["lec"] if b["id"] not in marks]
        except (FileNotFoundError, ValueError, KeyError):
            return []
    LEC_CACHE.write_text(json.dumps({"at": time.time(), "lec": lec}, ensure_ascii=False),
                         encoding="utf-8")
    return [b for b in lec if b["id"] not in marks]


def _live(conn) -> list[dict]:
    marks = tools.done_ids(conn)
    board = tools.board(conn, within_days=planner.HORIZON, past_days=14, limit=400)
    return board + _lectures(marks)


def must_today(conn, live: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """오늘 안 하면 마감 안에 안 들어가는 몫 [{id,label,course,due_at,hours}] 마감순 + 실제 계획."""
    real = planner.compute(conn, live, now)
    cf = planner.compute(conn, live, now, skip_today=True)
    had = {s["id"]: s["missing"] for s in real["short"]}
    out = []
    for s in cf["short"]:
        h = s["missing"] - had.get(s["id"], 0.0)
        # 목표일(target_at)만 있는 건 스스로 정한 날이다 — "오늘 안 하면 마감 넘김" 으로 조르지 않는다
        if h > 0 and s["due_at"]:
            out.append({"id": s["id"], "label": s["label"], "course": s.get("course"),
                        "due_at": s["due_at"], "hours": h})
    # 이미 마감이 지난 것도 오늘 몫이다 (반사실 계산은 그걸 오늘에 안 넣으니 따로).
    for d in real["days"][:1]:
        for t in d["tasks"]:
            if t["due_at"] and planner._dt(t["due_at"]) < now and all(x["id"] != t["id"] for x in out):
                out.append({"id": t["id"], "label": t["label"], "course": t.get("course"),
                            "due_at": t["due_at"], "hours": t["hours"], "overdue": True})
    out.sort(key=lambda x: x["due_at"])
    return out, real


def blocked_ahead(plan: dict, days: int = 2) -> list[str]:
    """내일·모레 중 일정 때문에 거의 못 하는 날 — "내일 투썸 알바 17:30–24:00" 같은 한 줄씩."""
    out = []
    for i, d in enumerate(plan["days"][1:1 + days], 1):
        if d["cap"] >= BLOCKED_CAP:
            continue
        busy = [b for b in d["busy"] if b["kind"] in ("event", "allday")]
        if d["cap_set"]:
            why = d["cap_note"] or "직접 비운 날"
        elif busy:
            b = max(busy, key=lambda b: planner._hours(b["e"]) - planner._hours(b["s"]))
            why = f"{b['label']}(종일)" if b["kind"] == "allday" else f"{b['label']} {b['s']}–{b['e']}"
        elif any(b["kind"] == "class" for b in d["busy"]):
            why = "수업"
        else:
            continue
        out.append(f"{'내일' if i == 1 else '모레'}({d['dow']}) {why}")
    return out


def _hrs(h: float) -> str:
    return f"{round(h * 60)}분" if h < 1 else f"{h:g}시간"


def _due(iso: str, now: datetime) -> str:
    d = planner._dt(iso)
    n = (d.date() - now.date()).days
    t = "자정" if (d.hour, d.minute) == (23, 59) else f"{d.hour:02d}:{d.minute:02d}"
    day = "오늘" if n == 0 else "내일" if n == 1 else f"{d.month}/{d.day}({planner.DOW[d.weekday()]})"
    return f"{day} {t}"


def _url(open_id: str | None = None, tab: str | None = None) -> str:
    q = []
    if tab:
        q.append(f"tab={tab}")
    if open_id:
        from urllib.parse import quote
        q.append(f"open={quote(open_id, safe='')}")
    return f"{config.ISSUER}/" + ("?" + "&".join(q) if q else "")


# ---------------------------------------------------------------- 알림 한 벌씩
def morning(conn, live, now) -> tuple[str, str, str] | None:
    must, plan = must_today(conn, live, now)
    today = plan["days"][0]
    blk = blocked_ahead(plan)
    lines = []
    if blk:
        lines.append(" · ".join(blk) + " — 그날은 거의 못 해요.")
    if must:
        total = sum(x["hours"] for x in must)
        title = f"오늘 최소 여기까지 끝내야 해요 (≈ {_hrs(total)})"
        lines += [f"· {x['label']} {_hrs(x['hours'])} — 마감 {_due(x['due_at'], now)}"
                  + (" (지남)" if x.get("overdue") else "") for x in must[:5]]
        return title, "\n".join(lines), _url(tab="today")
    ts = today["tasks"]
    if not ts and not blk:
        return None
    if ts:
        title = f"오늘 할 것 ≈ {_hrs(sum(t['hours'] for t in ts))}"
        lines += [f"· {t['label']} {_hrs(t['hours'])}" for t in ts[:5]]
    else:
        title = "오늘은 배정된 게 없어요"
        lines.append("뒤에 있는 걸 당겨 해 두면 편해요.")
    return title, "\n".join(lines), _url(tab="week")


def evening(conn, live, now):
    must, _plan = must_today(conn, live, now)
    if not must:
        return None
    body = "\n".join(f"· {x['label']} {_hrs(x['hours'])} — 마감 {_due(x['due_at'], now)}" for x in must[:5])
    return ("아직 오늘 끝내야 할 게 남았어요",
            body + "\n다 했으면 대시보드에서 완료를 찍어 주세요.", _url(tab="today"))


def due_soon(live, now, seen=lambda k: False) -> list[tuple[str, str, str, str, list[str]]]:
    """(key 접두, title, body, url, 이 알림이 덮는 key 들). 회차(24h·3h)마다 한 알림.
    seen(key) 가 참인 것(이미 알린 것)은 뺀다 — 새로 걸린 것만 묶어 보낸다."""
    def tier(b, left):                  # 이 블록이 지금 걸리는 회차 (없으면 None)
        hs = DUE_BEFORE_LEC if b["kind"] == "lecture" else DUE_BEFORE
        for i, h in enumerate(hs):
            nxt = hs[i + 1] if i + 1 < len(hs) else 0
            if nxt < left <= h:
                return h
        return None

    out = []
    for h in sorted(set(DUE_BEFORE) | set(DUE_BEFORE_LEC), reverse=True):
        hit = []
        for b in live:
            if not b.get("due_at") or b.get("done"):
                continue
            left = (planner._dt(b["due_at"]) - now).total_seconds() / 3600
            if tier(b, left) == h and not seen(f"due{h}:{b['id']}:{b['due_at']}"):
                hit.append(b)
        if not hit:
            continue
        hit.sort(key=lambda b: b["due_at"])
        keys = [f"due{h}:{b['id']}:{b['due_at']}" for b in hit]
        lec = lambda b: " (출석 마감)" if b["kind"] == "lecture" else ""
        if len(hit) == 1:
            b = hit[0]
            title = (f"출석 마감 {h}시간 전 — {b['label']}" if b["kind"] == "lecture"
                     else f"마감 {h}시간 전 — {b['label']}")
            body = f"{b.get('course') or ''} · {_due(b['due_at'], now)}{lec(b)}".strip(" ·")
            url = _url(open_id=b["id"])
        else:
            title = f"마감 {h}시간 안 {len(hit)}개"
            body = "\n".join(f"· {b['label']} — {_due(b['due_at'], now)}{lec(b)}" for b in hit[:6])
            url = _url(tab="today")
        out.append((f"due{h}", title, body, url, keys))
    return out


def lec_opened(live, now, seen=lambda k: False) -> tuple[str, str, str, list[str]] | None:
    """인정기간이 막 열린 강의영상 → (title, body, url, keys). 여러 개면 한 알림."""
    hit = []
    for b in live:
        if b["kind"] != "lecture" or b.get("done") or not b.get("open_at") or not b.get("due_at"):
            continue
        age = (now - planner._dt(b["open_at"])).total_seconds() / 3600
        if 0 <= age < LEC_OPEN_FRESH and planner._dt(b["due_at"]) > now \
                and not seen(f"lecopen:{b['id']}:{b['due_at']}"):
            hit.append(b)
    if not hit:
        return None
    hit.sort(key=lambda b: b["due_at"])
    keys = [f"lecopen:{b['id']}:{b['due_at']}" for b in hit]
    mins = lambda b: f" {b['minutes']}분" if b.get("minutes") else ""
    if len(hit) == 1:
        b = hit[0]
        return (f"강의영상 열림 — {b['label']}",
                f"{b.get('course') or ''}{mins(b)} · 출석 마감 {_due(b['due_at'], now)}".strip(" ·"),
                _url(open_id=b["id"]), keys)
    return (f"강의영상 {len(hit)}개 열림",
            "\n".join(f"· {b['label']}{mins(b)} — {_due(b['due_at'], now)}까지" for b in hit[:6]),
            _url(tab="today"), keys)


# ---------------------------------------------------------------- 실행
def _sent(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM push_log WHERE key=?", [key]).fetchone() is not None


def _log(conn, key: str, title: str, body: str, n: int, url: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO push_log (key,at,title,body,sent,url) VALUES (?,?,?,?,?,?)",
                 [key, tools._now_iso(), title, body, n, url])


def run(now: datetime | None = None, dry: bool = False) -> list[dict]:
    now = (now or datetime.now(KST)).astimezone(KST)
    conn = db.vault()
    try:
        if not dry and not conn.execute("SELECT 1 FROM push_sub LIMIT 1").fetchone():
            return []                   # 받을 기기가 없으면 LearningX 도 안 부른다
        if QUIET[0] <= now.hour < QUIET[1]:
            return []
        live = _live(conn)
        todo = []                       # (keys, title, body, url, urgency)
        day = now.date().isoformat()
        if MORNING <= now.hour < MORNING_UNTIL and not _sent(conn, f"morning:{day}"):
            m = morning(conn, live, now)
            todo.append(([f"morning:{day}"], *(m or (None, None, None)), "normal"))
        if EVENING <= now.hour < EVENING_UNTIL and not _sent(conn, f"evening:{day}"):
            e = evening(conn, live, now)
            todo.append(([f"evening:{day}"], *(e or (None, None, None)), "high"))
        for _p, title, body, url, keys in due_soon(live, now, lambda k: _sent(conn, k)):
            todo.append((keys, title, body, url, "high"))
        lo = lec_opened(live, now, lambda k: _sent(conn, k))
        if lo:
            todo.append((lo[3], lo[0], lo[1], lo[2], "high"))
        out = []
        for keys, title, body, url, urg in todo:
            n = 0
            if title and not dry:
                n = push.send_all(conn, title, body, url, tag=keys[0].split(":")[0], urgency=urg)
            if not dry:
                for k in keys:          # 보낼 게 없었던 아침·저녁도 "확인함" 으로 적는다
                    _log(conn, k, title, body, n, url)
            out.append({"keys": keys, "title": title, "body": body, "url": url, "sent": n})
        if not dry:
            cut = (now - timedelta(days=KEEP_LOG_DAYS)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM push_log WHERE at < ?", [cut])
            conn.commit()
        return out
    finally:
        conn.close()


def main() -> None:
    dry = "--dry" in sys.argv
    at = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--at=")), None)
    now = datetime.fromisoformat(at).replace(tzinfo=KST) if at else None
    for r in run(now, dry):
        print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
