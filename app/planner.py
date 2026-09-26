"""이번 주 계획 — 남은 과제·강의영상을 날짜에 배치한다 (2026-09-23).

사용자 요청: "이날 이거 때문에 제출일 지키기 힘들 것 같으니 이날 해봐라" 를 보여주는 것.
decisions.md 의 est_hours 절 — *마지막 가용창부터 거꾸로 소요시간만큼 쌓아서 착수 시점을
구한다* — 를 그대로 옮긴 것이다. 모델을 안 부른다. 날짜 계산뿐이다.

저장하지 않는다. 대시보드를 열 때마다, MCP week_plan 을 부를 때마다 새로 짠다.
완료를 찍거나 캘린더가 바뀌면 다음 계산에 바로 반영된다. 사람이 고친 것만 DB 에 있다:
  plan_pin  과제를 특정 날에 박기      ("물리는 목요일에 할게")
  plan_day  그 날 가용시간을 직접 정하기 ("금요일엔 못 해" → 0)

하루 가용시간 = min(MAX_DAY, (DAY_START~DAY_END 중 빈 시간) × RATIO)
  빈 시간에서 빼는 것: 수업(data/timetable.json) + 구글 캘린더 일정, 앞뒤 PAD 씩.
  종일 일정은 하루를 통째로 막는다. 단 제목이 `[` 로 시작하면(예: "[프기실]Quiz1")
  시험·표시용이라 보고 막지 않는다.

배치 = 마감 늦은 것부터, 각자 (마감일 − BUFFER_DAYS) 에서 거꾸로 채운다 (역순 EDF).
  그 날이 모자라면 하루씩 앞당긴다 → 앞당겨진 과제엔 이유가 붙는다.
  하루 전까지 안 들어가면 마감 당일, 그래도 안 되면 `short` 로 올린다. 숨기지 않는다.

목표일(item.target_at, 2026-09-25) — 사용자가 스스로 정한 날. 진짜 마감(due_at)과 다르게 다룬다:
  BUFFER 없이 그 날까지 끝내도록 배치하고, 자리가 없으면 뒤로 밀린다(마감이 아니니까).
  지나도 "마감 지남" 이 아니라 "목표일 지남" 이다. 둘 다 있으면 이른 쪽을 목표로 삼는다.
"""

import json
import math
from datetime import date, datetime, timedelta

from . import config, tools

KST = tools.KST
TIMETABLE = config.DATA_DIR / "timetable.json"

DAY_START, DAY_END = 9.0, 24.0   # 공부할 수 있는 창 (시)
PAD = 0.5                        # 수업·일정 앞뒤 이동·준비 시간
RATIO = 0.5                      # 빈 시간 중 실제로 과제에 쓰는 비율
MAX_DAY = 5.0                    # 하루 상한
STEP = 0.5                       # 배치 단위 (시간)
BUFFER_DAYS = 1                  # 마감 하루 전까지 끝내는 게 목표
HORIZON = 14                     # 이만큼 앞 마감까지 배치한다
SHOW = 7                         # 보여주는 날 수
DEFAULT_EST = 1.0                # 추정이 없을 때
DOW = "월화수목금토일"


def _up(h: float) -> float:
    return math.ceil(h / STEP - 1e-9) * STEP


def _down(h: float) -> float:
    return math.floor(h / STEP + 1e-9) * STEP


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(KST)


def _hm(h: float) -> str:
    return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


def _hours(t: str) -> float:
    hh, mm = t.split(":")
    return int(hh) + int(mm) / 60


def load_timetable() -> dict:
    try:
        return json.loads(TIMETABLE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"classes": [], "holidays": []}


def _busy(day: date, tt: dict, events: list[dict]) -> list[dict]:
    """그 날 잡혀 있는 것. kind: class | event | allday | mark(표시만, 안 막음)."""
    out = []
    ds = day.isoformat()
    if (tt.get("from", "") <= ds <= tt.get("to", "9999")) and ds not in tt.get("holidays", []):
        for c in tt.get("classes", []):
            if c["dow"] == day.weekday():
                out.append({"s": _hours(c["start"]), "e": _hours(c["end"]),
                            "label": c["name"], "kind": "class"})
    d0 = datetime(day.year, day.month, day.day, tzinfo=KST)
    d1 = d0 + timedelta(days=1)
    for ev in events:
        s = _dt(ev["start_at"])
        e = _dt(ev["end_at"]) if ev.get("end_at") else s + timedelta(hours=1)
        if e <= d0 or s >= d1:
            continue
        span = (e - s).total_seconds() / 3600
        allday = s.hour == 0 and s.minute == 0 and span >= 24 and span % 24 == 0
        if allday:
            kind = "mark" if ev["title"].startswith("[") else "allday"
            out.append({"s": DAY_START, "e": DAY_END, "label": ev["title"], "kind": kind})
            continue
        sh = max(0.0, (s - d0).total_seconds() / 3600)
        eh = min(24.0, (e - d0).total_seconds() / 3600)
        out.append({"s": sh, "e": eh, "label": ev["title"], "kind": "event"})
    out.sort(key=lambda b: (b["s"], b["e"]))
    return out


def _free(busy: list[dict], start: float) -> float:
    """[start, DAY_END] 에서 막힌 구간(앞뒤 PAD)을 뺀 시간."""
    iv = sorted((max(start, b["s"] - PAD), min(DAY_END, b["e"] + PAD))
                for b in busy if b["kind"] != "mark")
    free, cur = 0.0, max(DAY_START, start)
    for s, e in iv:
        if e <= cur:
            continue
        if s > cur:
            free += s - cur
        cur = max(cur, e)
    return free + max(0.0, DAY_END - cur)


def _cause(day: dict) -> str | None:
    """이 날이 왜 좁은가 — 가장 긴 일정 하나를 이름으로 댄다."""
    big = [b for b in day["busy"] if b["kind"] in ("event", "allday")]
    if big:
        b = max(big, key=lambda b: b["_e"] - b["_s"])
        if b["kind"] == "allday":
            return f"{day['dow']} {b['label']}(종일)"
        return f"{day['dow']} {b['label']} {b['s']}–{b['e']}"
    if day["cap_note"]:
        return f"{day['dow']} {day['cap_note']}"
    if sum(1 for b in day["busy"] if b["kind"] == "class") >= 2:
        return f"{day['dow']} 수업"
    return None


def compute(conn, live: list[dict], now: datetime | None = None, skip_today: bool = False) -> dict:
    """live = 아직 안 끝난 블록(tools.board + 강의영상). 끝난 건 넘기지 마라.

    skip_today=True — "오늘 아무것도 안 하면?" 반사실 계산. 오늘 가용시간 0, 오늘 박은 것 무시.
    그때 새로 `short` 에 오르는 양이 곧 **오늘 최소한 해야 하는 양**이다 (notify.must_today).
    """
    now = (now or datetime.now(KST)).astimezone(KST)
    today = now.date()
    last = today + timedelta(days=HORIZON)
    tt = load_timetable()
    events = tools.blocks(conn, today.isoformat(), (last + timedelta(days=1)).isoformat(),
                          limit=400)
    pins = [dict(r) for r in conn.execute(
        "SELECT * FROM plan_pin WHERE day >= ? ORDER BY day", [today.isoformat()])]
    caps = {r["day"]: dict(r) for r in conn.execute(
        "SELECT * FROM plan_day WHERE day >= ?", [today.isoformat()])}

    # ---- 날마다 가용시간
    days: list[dict] = []
    idx: dict[date, dict] = {}
    for i in range(HORIZON + 1):
        d = today + timedelta(days=i)
        busy = _busy(d, tt, events)
        start = DAY_START
        if i == 0:
            start = max(DAY_START, _up(now.hour + now.minute / 60))
        base = _down(min(MAX_DAY, _free(busy, start) * RATIO))
        ov = caps.get(d.isoformat())
        cap = float(ov["hours"]) if ov else base
        if skip_today and i == 0:
            cap = 0.0
        day = {"date": d.isoformat(), "dow": DOW[d.weekday()], "today": i == 0,
               "cap": cap, "cap_base": base, "cap_note": ov["note"] if ov else None,
               "cap_set": bool(ov), "left": cap,
               "busy": [dict(b, s=_hm(b["s"]), e=_hm(b["e"]), _s=b["s"], _e=b["e"])
                        for b in busy],
               "tasks": []}
        days.append(day)
        idx[d] = day

    # ---- 배치할 것
    tasks, undated = [], []
    def by_day(x: datetime) -> date:      # 아침 일찍 끝나는 시각이면 그 전날까지 해야 한다
        return x.date() if x.hour + x.minute / 60 > DAY_START else x.date() - timedelta(days=1)

    for b in live:
        due = _dt(b["due_at"]) if b.get("due_at") else None
        tgt = _dt(b["target_at"]) if b.get("target_at") else None
        if not due and not tgt:
            undated.append({"id": b["id"], "label": b["label"], "course": b.get("course"),
                            "kind": b["kind"]})
            continue
        key = min(x for x in (due, tgt) if x)
        if key.date() > last:
            continue
        if b["kind"] == "lecture" and due < now:
            continue                 # 인정기간이 끝난 영상은 지금 봐도 출석이 안 된다
        if b.get("est_hours"):
            est, guess = _up(float(b["est_hours"])), False
        else:
            est, guess = DEFAULT_EST, True
        goals = []
        if due:
            goals.append(by_day(due) - timedelta(days=BUFFER_DAYS))
        if tgt:
            goals.append(by_day(tgt))
        goal = min(goals)
        # 넘겨도 되는 끝 — 진짜 마감이 있으면 그 날, 목표일뿐이면 계획 창 끝까지 밀린다.
        latest = min(last, by_day(due)) if due else last
        earliest = today
        if b.get("open_at"):
            earliest = max(today, _dt(b["open_at"]).date())
        tasks.append({"b": b, "due": key, "est": est, "guess": guess, "left": est,
                      "goal": goal, "soft": not due, "real": due, "tgt": tgt,
                      "latest": max(today, latest), "earliest": earliest,
                      "overdue": bool(due) and due < now,
                      "goal_passed": bool(tgt) and by_day(tgt) < today, "parts": []})
    by_id = {t["b"]["id"]: t for t in tasks}

    def put(t, day, h, reason=None, pinned=False, note=None):
        day["left"] -= h
        t["left"] -= h
        t["parts"].append(day["date"])
        b = t["b"]
        day["tasks"].append({
            "id": b["id"], "label": b["label"], "course": b.get("course"), "kind": b["kind"],
            "hours": h, "due_at": b.get("due_at"), "target_at": b.get("target_at"),
            "est_guess": t["guess"],
            "reason": reason, "pinned": pinned, "note": note})

    # 1) 사람이 박은 것 먼저. 가용시간을 넘겨도 따른다 (넘치면 화면에 과부하로 보인다).
    for p in pins:
        if skip_today and p["day"] == today.isoformat():
            continue
        t = by_id.get(p["target"])
        day = idx.get(date.fromisoformat(p["day"]))
        if not t or not day or t["left"] <= 0:
            continue
        h = min(t["left"], _up(p["hours"])) if p["hours"] else t["left"]
        late = (" · 마감 뒤" if t["real"] and day["date"] > t["real"].date().isoformat()
                else " · 목표 뒤" if t["tgt"] and day["date"] > t["tgt"].date().isoformat() else "")
        put(t, day, h, "직접 정함" + (f" — {p['note']}" if p["note"] else "") + late, True, p["note"])

    # 2) 마감 지난 것 — 오늘로.
    short = []
    for t in sorted(tasks, key=lambda t: t["due"]):
        if t["overdue"] and t["left"] > 0 and not skip_today:
            put(t, days[0], t["left"], "마감 지남 — 오늘 처리")

    # 3) 나머지 — 마감 늦은 것부터, (마감 − BUFFER) 또는 목표일에서 거꾸로.
    for t in sorted(tasks, key=lambda t: t["due"], reverse=True):
        if t["left"] <= 0:
            continue
        target = max(t["earliest"], t["goal"])
        d = target
        while t["left"] > 0 and d >= t["earliest"]:
            day = idx[d]
            h = min(t["left"], _down(day["left"]))
            if h > 0:
                reason = None
                if d < target:
                    # 마감에 가까운 날부터 두 개만 — 이유가 길면 안 읽힌다
                    why = []
                    for k in range(min(2, (target - d).days)):
                        dd = idx[target - timedelta(days=k)]
                        c = _cause(dd)
                        why.append(c if c else f"{dd['dow']} 다른 과제로 참")
                    reason = " · ".join(why) + " → 앞당김"
                elif t["goal_passed"]:
                    reason = "목표일 지남 — 오늘로"
                put(t, day, h, reason)
            d -= timedelta(days=1)
        # 하루 전(목표일)까지 안 들어가면 그 뒤로 — 진짜 마감이면 마감 당일까지만
        d = target + timedelta(days=1)
        while t["left"] > 0 and d <= t["latest"]:
            day = idx[d]
            h = min(t["left"], _down(day["left"]))
            if h > 0:
                last_day = d == t["latest"] and not t["soft"]
                put(t, day, h, "하루 전까지 자리가 없어서 마감 당일" if last_day
                    else "목표일까지 자리가 없어서 뒤로")
            d += timedelta(days=1)
        if t["left"] > 0:
            short.append({"id": t["b"]["id"], "label": t["b"]["label"],
                          "course": t["b"].get("course"), "due_at": t["b"].get("due_at"),
                          "target_at": t["b"].get("target_at"),
                          "missing": t["left"], "est": t["est"]})

    # 여러 날에 나뉜 것은 "1/2" 표시
    for day in days:
        for x in day["tasks"]:
            ps = by_id[x["id"]]["parts"]
            if len(ps) > 1:
                x["part"] = f"{sorted(ps).index(day['date']) + 1}/{len(ps)}"
        day["tasks"].sort(key=lambda x: x["due_at"] or x["target_at"])
        day["used"] = sum(x["hours"] for x in day["tasks"])
        day["left"] = max(0.0, day["left"])
        for b in day["busy"]:
            b.pop("_s"), b.pop("_e")

    return {"now": now.isoformat(timespec="seconds"), "days": days, "short": short,
            "undated": undated[:20],
            "rule": {"window": f"{_hm(DAY_START)}–{_hm(DAY_END)}", "ratio": RATIO,
                     "max_day": MAX_DAY, "buffer_days": BUFFER_DAYS}}


def brief(plan: dict, days: int = SHOW) -> dict:
    """MCP 용 — 날 수를 자르고 필드를 줄인다."""
    out = []
    for d in plan["days"][:max(1, min(days, HORIZON))]:
        out.append({
            "date": d["date"], "dow": d["dow"], "cap": d["cap"], "used": d["used"],
            "cap_set": d["cap_note"] if d["cap_set"] else None,
            "busy": [f"{b['s']}–{b['e']} {b['label']}" if b["kind"] not in ("allday", "mark")
                     else f"종일 {b['label']}" for b in d["busy"]],
            "tasks": [{k: x[k] for k in ("id", "label", "course", "hours", "due_at", "target_at",
                                         "reason")
                       if x.get(k) is not None} for x in d["tasks"]],
        })
    return {"days": out, "short": plan["short"], "rule": plan["rule"]}


# ---------------------------------------------------------------- 손질 (MCP 쓰기)
def pin(conn, id: str, day: str, hours: float | None = None, note: str | None = None) -> dict:
    """과제를 그 날에 박는다. hours=0 → 그 날 박은 것을 푼다. day='*' + hours=0 → 전부 푼다."""
    if day == "*":
        if hours != 0:
            return {"ok": False, "error": "day='*' 는 hours=0 (전부 풀기) 에만 쓴다"}
        n = conn.execute("DELETE FROM plan_pin WHERE target=?", [id]).rowcount
        conn.commit()
        return {"ok": True, "id": id, "unpinned": n}
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {"ok": False, "error": "day 는 YYYY-MM-DD"}
    if hours == 0:
        n = conn.execute("DELETE FROM plan_pin WHERE target=? AND day=?", [id, d.isoformat()]).rowcount
        conn.commit()
        return {"ok": True, "id": id, "day": d.isoformat(), "unpinned": n}
    if hours is not None and not (0 < hours <= 24):
        return {"ok": False, "error": "hours 는 0~24"}
    label = None
    if id.startswith(("canvas:", "item:")):
        tbl = "deadline" if id.startswith("canvas:") else "item"
        r = conn.execute(f"SELECT label{',title' if tbl == 'deadline' else ''} FROM {tbl} WHERE id=?",
                         [id]).fetchone()
        if not r:
            return {"ok": False, "error": f"그런 블록 없음: {id}"}
        label = r["label"] or (r["title"] if tbl == "deadline" else None)
    elif not id.startswith("lx:"):
        return {"ok": False, "error": f"알 수 없는 id: {id}"}
    conn.execute(
        "INSERT INTO plan_pin (target,day,hours,label,note,at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(target,day) DO UPDATE SET hours=excluded.hours, note=excluded.note, at=excluded.at",
        [id, d.isoformat(), hours, label, note, tools._now_iso()])
    conn.commit()
    return {"ok": True, "id": id, "day": d.isoformat(), "hours": hours}


def move(conn, id: str, frm: str | None, to: str | None, hours: float | None = None,
         whole: bool = True) -> dict:
    """대시보드에서 끌어 옮기기 (2026-09-23). frm 날에 놓인 몫을 to 날로 박는다.

    whole=True  — 한 날에만 놓인 과제. 남은 양 전부를 to 에 박고, 박아 둔 다른 날은 푼다.
    whole=False — 여러 날에 나뉜 한 조각. frm 의 박음만 풀고 to 에 hours 만큼 더한다.
                  나머지 조각은 자동 배치가 그대로 다시 채운다.
    to=None     — 이 과제의 박음을 전부 풀어 자동 배치로 되돌린다.
    """
    if to is None:
        return pin(conn, id, "*", 0)
    today = datetime.now(KST).date()
    try:
        d = date.fromisoformat(to)
        f = date.fromisoformat(frm) if frm else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "날짜는 YYYY-MM-DD"}
    if not (today <= d <= today + timedelta(days=HORIZON)):
        return {"ok": False, "error": f"오늘부터 {HORIZON}일 안으로만 옮길 수 있어요"}
    if f == d:
        return {"ok": True, "id": id, "day": d.isoformat(), "same": True}
    if whole:
        conn.execute("DELETE FROM plan_pin WHERE target=? AND day>=?", [id, today.isoformat()])
        return pin(conn, id, d.isoformat(), None)
    if not hours or hours <= 0:
        return {"ok": False, "error": "나뉜 조각은 hours 가 있어야 한다"}
    old = conn.execute("SELECT hours FROM plan_pin WHERE target=? AND day=?",
                       [id, d.isoformat()]).fetchone()
    h = None if old and old["hours"] is None else (old["hours"] if old else 0) + hours
    if f:
        conn.execute("DELETE FROM plan_pin WHERE target=? AND day=?", [id, f.isoformat()])
    return pin(conn, id, d.isoformat(), min(h, 24) if h else None)


def set_day(conn, day: str, hours: float | None, note: str | None = None) -> dict:
    """그 날 가용시간을 직접 정한다. hours=None → 자동 계산으로 되돌린다."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {"ok": False, "error": "day 는 YYYY-MM-DD"}
    if hours is None:
        n = conn.execute("DELETE FROM plan_day WHERE day=?", [d.isoformat()]).rowcount
        conn.commit()
        return {"ok": True, "day": d.isoformat(), "reset": bool(n)}
    if not (0 <= hours <= 16):
        return {"ok": False, "error": "hours 는 0~16"}
    conn.execute(
        "INSERT INTO plan_day (day,hours,note,at) VALUES (?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET hours=excluded.hours, note=excluded.note, at=excluded.at",
        [d.isoformat(), hours, note, tools._now_iso()])
    conn.commit()
    return {"ok": True, "day": d.isoformat(), "hours": hours}
