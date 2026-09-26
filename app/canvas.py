"""Canvas 실시간 조회 — 챗의 Claude 가 "이 과목 이 영상" 을 찾을 때 쓴다 (canvas_find / canvas_read).

폴러(poller/canvas_poll.py)는 과제만 30분마다 DB 에 담는다. 모듈·페이지·영상 링크는 양이 많고
자주 안 쓰여서 DB 에 안 담고, 물어볼 때만 Canvas 에 직접 간다. 과목별 모듈 목록은 5분 캐시.

**읽기만 한다.** ExternalTool(출결 LTI) 항목을 launch 하지 않는다 — launch 자체가 조회·출석
기록을 남길 수 있다 (memory/verified.md "출결 대상 판별"). 여기서 쓰는 건 모듈 메타데이터뿐이다.
Zoom 녹화 본문은 서버에서 못 연다(같은 곳). 영상은 제목·주차·링크까지만 정리된다.
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("ERION_CANVAS_BASE", "https://canvas.skku.edu")
TOKEN_FILE = Path(os.environ.get(
    "ERION_CANVAS_TOKEN_FILE", str(Path.home() / ".erion/canvas_token")))

# 폴러와 같은 제외 목록 — 법정의무교육류.
EXCLUDE_COURSES = {79157, 79160, 79162}

CACHE_SEC = 300
CAP_FIND = 30
READ_MAX = 12000

_cache: dict[str, tuple[float, object]] = {}


class CanvasError(RuntimeError):
    pass


def _token() -> str:
    try:
        tok = TOKEN_FILE.read_text().strip()
    except OSError as e:
        raise CanvasError(f"Canvas 토큰 파일을 못 읽음: {TOKEN_FILE}") from e
    if not tok:
        raise CanvasError("Canvas 토큰이 비어 있음")
    return tok


def _get(path: str, *, paged: bool = True):
    url = BASE + "/api/v1" + path
    out: list = []
    tok = _token()
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8")
                link = r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            raise CanvasError(f"Canvas {e.code} at {path}") from e
        if body.startswith("while(1);"):
            body = body[len("while(1);"):]
        page = json.loads(body)
        if not isinstance(page, list) or not paged:
            return page
        out += page
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return out


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def plain(h: str | None, limit: int = READ_MAX) -> str:
    """HTML → 텍스트. 폴러의 _plain 과 같은 규칙."""
    if not h:
        return ""
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", h)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()[:limit]


def short_name(name: str) -> str:
    """"논리회로_ICE2001_42(조건희)" → "논리회로". 폴러와 같은 규칙 — deadline.course 와 맞아야 한다."""
    return (name or "").split("_")[0].strip() or name


def courses() -> list[dict]:
    def load():
        fav = _get("/users/self/favorites/courses?per_page=100")
        return [{"id": c["id"], "name": c.get("name") or "", "short": short_name(c.get("name") or ""),
                 "code": c.get("course_code") or ""}
                for c in fav if c.get("id") not in EXCLUDE_COURSES]
    return _cached("courses", load)


def _match_course(q: str | None) -> list[dict]:
    cs = courses()
    if not q:
        return cs
    ql = q.lower().replace(" ", "")
    hit = [c for c in cs if ql in (c["name"] + c["code"]).lower().replace(" ", "")]
    return hit or cs  # 못 맞추면 전 과목에서 찾는다 — 빈손보다 낫다


def _modules(cid: int) -> list[dict]:
    return _cached(f"mod:{cid}", lambda: _get(f"/courses/{cid}/modules?include[]=items&per_page=100"))


def _kind(it: dict) -> str:
    t = it.get("type")
    ext = it.get("external_url") or ""
    if t == "ExternalTool" and "lecture_attendance" in ext:
        return "attendance"          # 출결 LTI — 영상일 수도 자료일 수도 있다(제목으로만 갈림)
    if t == "ExternalUrl" and "zoom.us" in ext:
        return "zoom"                # Zoom 녹화 — 본문 못 연다
    return (t or "").lower()          # page · assignment · file · discussion · quiz · subheader …


def find(query: str = "", course: str | None = None) -> list[dict]:
    """모듈 항목을 제목으로 찾는다. query 의 낱말이 전부 (모듈명+항목명)에 들어가야 맞는다."""
    words = [w.lower() for w in (query or "").split() if w]
    out = []
    for c in _match_course(course):
        for m in _modules(c["id"]):
            mname = m.get("name") or ""
            for it in m.get("items") or []:
                if it.get("type") == "SubHeader":
                    continue
                hay = f"{mname} {it.get('title') or ''}".lower()
                if words and not all(w in hay for w in words):
                    continue
                out.append({
                    "ref": f"{c['id']}:{it.get('id')}",
                    "course": c["short"],
                    "module": mname,
                    "title": it.get("title"),
                    "kind": _kind(it),
                    "url": it.get("html_url"),
                    "external_url": it.get("external_url"),
                })
                if len(out) >= CAP_FIND:
                    return out
    return out


def read(ref: str) -> dict:
    """find 가 준 ref("<course_id>:<module_item_id>") 의 내용. 같은 모듈의 다른 항목 제목도 준다."""
    try:
        cid_s, iid_s = ref.split(":", 1)
        cid, iid = int(cid_s), int(iid_s)
    except ValueError as e:
        raise CanvasError(f"ref 형식이 틀림: {ref!r} (canvas_find 결과의 ref 를 그대로 넘겨라)") from e
    course = next((c for c in courses() if c["id"] == cid), None)
    for m in _modules(cid):
        for it in m.get("items") or []:
            if it.get("id") != iid:
                continue
            kind = _kind(it)
            res = {
                "ref": ref, "course": course["short"] if course else str(cid),
                "module": m.get("name"), "title": it.get("title"), "kind": kind,
                "url": it.get("html_url"), "external_url": it.get("external_url"),
                "siblings": [s.get("title") for s in m.get("items") or []
                             if s.get("id") != iid and s.get("type") != "SubHeader"][:25],
                "content": "",
            }
            t = it.get("type")
            if t == "Page" and it.get("page_url"):
                res["content"] = plain(_get(f"/courses/{cid}/pages/{it['page_url']}", paged=False).get("body"))
            elif t == "Assignment" and it.get("content_id"):
                a = _get(f"/courses/{cid}/assignments/{it['content_id']}", paged=False)
                res["content"] = plain(a.get("description"))
                res["due_at"] = a.get("due_at")
            elif t == "Discussion" and it.get("content_id"):
                d = _get(f"/courses/{cid}/discussion_topics/{it['content_id']}", paged=False)
                res["content"] = plain(d.get("message"))
            elif t == "File" and it.get("content_id"):
                f = _get(f"/files/{it['content_id']}", paged=False)
                res["content"] = f"(파일) {f.get('display_name')} · {f.get('size', 0) // 1024}KB — 본문은 안 읽는다"
            elif kind in ("zoom", "attendance"):
                res["content"] = "(영상/출결 항목 — 서버에서 본문을 못 연다. 제목·모듈·같은 모듈 항목으로 정리하라)"
            return res
    raise CanvasError(f"그 항목을 못 찾음: {ref}")
