"""LearningX 조회 — 강의영상의 **출석 인정기간·시청 여부·길이**를 읽는다.

Canvas 는 출결 LTI 항목에 대해 아무것도 안 준다 (`content_details` 가
`{"locked_for_user": false}` 하나뿐). 인정기간도 진도도 전부 LearningX 안에 있다.
경위·경로·안정성 감사는 `memory/verified.md` 의 "LearningX API" 절에 있다.

**읽기만 한다.** 진도·출결이 올라가는 곳은 플레이어가 쏘는
`/courses/{cid}/sections/0/components/{id}/progress` 하나뿐이고, 여기서는 안 건드린다.
메타데이터 GET 이 상태를 안 바꾼다는 건 통제 실험으로 확인했다(verified.md).

**토큰 발급만 쓰기성 요청이다.** LearningX 는 자기 JWT(`xn_api_token`)를 요구하고,
그건 LTI launch 로만 나온다. 실재하는 영상을 launch 하면 열람 기록이 남으므로
**끊어진 항목(DEAD_LAUNCH)** 을 쓴다 — 500 을 내면서도 토큰은 정상 발급되고,
기록될 대상이 없으니 남는 것도 없다. 이 항목이 죽으면 조용히 실재 영상으로
넘어가지 않고 **에러를 낸다** — 대체 항목은 사람이 고른다.
"""

import html as _html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import canvas

BASE = canvas.BASE
TOOL_ID = 305                      # "강의/출결" LTI — 전 과목 공통

# 발자국 0 인 토큰 발급용. (course_id, lti_item_id) — 지난 학기 복사 잔여물이라
# LearningX 에 item 이 없다. verified.md "발자국 0 으로 토큰 찍는 법" 참조.
DEAD_LAUNCH = (77886, 126518)

CACHE_SEC = 300                    # 과목 모듈 캐시
TOKEN_SLACK = 300                  # 만료 5분 전이면 다시 받는다

_token: tuple[float, str] | None = None     # (exp, jwt)
_cache: dict[str, tuple[float, object]] = {}


class LearningXError(RuntimeError):
    pass


# ---------------------------------------------------------------- 토큰

def _launch_form(cid: int, item_id: int) -> tuple[str, bytes]:
    """sessionless_launch → OAuth1 서명된 LTI 폼. URL 생성까지는 기록을 안 남긴다."""
    lti = f"{BASE}/learningx/lti/lecture_attendance/items/view/{item_id}"
    q = urllib.parse.urlencode({"id": TOOL_ID, "url": lti})
    got = canvas._get(f"/courses/{cid}/external_tools/sessionless_launch?{q}", paged=False)
    verifier = got.get("url")
    if not verifier:
        raise LearningXError(f"sessionless_launch 가 url 을 안 줬다: {got}")

    req = urllib.request.Request(verifier, headers={"User-Agent": "erion-vault"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")

    m = re.search(r'<form[^>]*action="([^"]+)"', page)
    if not m:
        raise LearningXError("launch 폼을 못 찾았다 — Canvas 가 응답 형식을 바꿨을 수 있다")
    action = _html.unescape(m.group(1))
    fields = re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)
    body = urllib.parse.urlencode(
        {n: _html.unescape(v) for n, v in fields}).encode()
    return action, body


def _mint() -> tuple[float, str]:
    """LTI launch 로 xn_api_token 을 받는다. 끊어진 항목이라 500 이 정상이다."""
    cid, item = DEAD_LAUNCH
    action, body = _launch_form(cid, item)
    req = urllib.request.Request(
        action, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "erion-vault"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            headers = r.headers
    except urllib.error.HTTPError as e:
        headers = e.headers          # 500 이어도 Set-Cookie 는 온다 — 이게 정상 경로다

    jwt = ""
    for c in headers.get_all("Set-Cookie") or []:
        m = re.match(r"\s*xn_api_token=([^;]+)", c)
        if m:
            jwt = urllib.parse.unquote(m.group(1))
            break
    if not jwt:
        raise LearningXError(
            f"xn_api_token 이 안 나왔다. 발급용 끊어진 항목({cid}:{item})이 죽었을 수 있다 — "
            "실재하는 영상으로 바꾸지 말고 사람에게 물어라 (memory/verified.md)")

    try:
        p = jwt.split(".")[1]
        import base64
        payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        exp = float(payload.get("exp") or 0)
    except Exception:
        exp = time.time() + 3600
    return exp, jwt


def token() -> str:
    global _token
    if _token and _token[0] - TOKEN_SLACK > time.time():
        return _token[1]
    _token = _mint()
    return _token[1]


# ---------------------------------------------------------------- 조회

def _get(path: str):
    url = BASE + "/learningx/api/v1" + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token()}",
        "Accept": "application/json",
        "User-Agent": "erion-vault",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LearningXError(f"LearningX {e.code} at {path}") from e


def modules(cid: int) -> list[dict]:
    """과목의 모듈 + 항목. 과목당 이 요청 하나면 인정기간까지 전부 나온다."""
    return _cached(f"lx:{cid}", lambda: _get(f"/courses/{cid}/modules"))


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# 영상으로 치는 content_type. pdf·기타 자료는 뺀다.
VIDEO_TYPES = {"movie", "everlec", "zoom", "screenlecture"}


def items(cid: int) -> list[dict]:
    """출결 LTI 항목을 납작하게 편다. 인정기간·시청 여부·길이가 붙는다."""
    out = []
    for m in modules(cid):
        for it in m.get("module_items") or []:
            if it.get("content_type") != "attendance_item":
                continue
            cd = it.get("content_data") or {}
            icd = cd.get("item_content_data") or {}
            ctype = icd.get("content_type")
            dur = icd.get("duration")
            out.append({
                "item_id": cd.get("item_id"),
                "module": m.get("name") or m.get("title"),
                "title": it.get("title"),
                "is_video": ctype in VIDEO_TYPES,
                "content_type": ctype,
                "use_attendance": bool(cd.get("use_attendance")),
                "period_status": cd.get("lecture_period_status"),
                "open_at": cd.get("unlock_at"),
                "close_at": cd.get("due_at"),
                "minutes": round(dur / 60) if dur else None,
                "watched": bool(it.get("completed")),
                "opened": bool(cd.get("opened")),
                "url": it.get("url"),
            })
    return out


# ---------------------------------------------------------------- 사람이 여는 링크

def open_url(lti_url: str | None, cid: int | None = None) -> str | None:
    """LTI 주소(…/learningx/lti/lecture_attendance/items/view/<id>)를 **Canvas 모듈 항목 페이지**로 바꾼다.

    LTI 주소는 Canvas 가 서명한 launch 폼을 받아야 열린다 — 브라우저로 바로 열면 LearningX 가
    "Whoops, looks like something went wrong." 을 낸다(2026-09-26 사용자 폰에서). 사람에게 줄 건
    `/courses/<cid>/modules/items/<id>` 다 — Canvas 가 그 안에서 launch 한다.
    Canvas 모듈 목록(5분 캐시)에서 external_url 로 찾는다. 이 GET 은 열람 기록을 안 남긴다.
    못 찾으면 None — 깨진 링크를 주느니 버튼을 안 띄운다."""
    if not lti_url:
        return None
    cids = [cid] if cid else [c["id"] for c in canvas.courses()]
    for c in cids:
        try:
            mods = canvas._modules(c)
        except Exception:                   # noqa: BLE001 — 링크 하나 때문에 목록이 죽으면 안 된다
            continue
        for m in mods:
            for it in m.get("items") or []:
                if it.get("external_url") == lti_url and it.get("html_url"):
                    return it["html_url"]
    return None


# ---------------------------------------------------------------- 브리프

CAP_LECTURES = 30


def brief(within_days: int = 14, course: str | None = None,
          unwatched_only: bool = True, limit: int | None = None) -> list[dict]:
    """인정기간이 within_days 안에 끝나는 강의영상. 마감순, 최대 30행.

    출결 대상(`use_attendance`)이고 영상인 것만 센다 — pdf·자료는 뺀다.
    시각은 KST ISO 로 돌려준다 (볼트의 다른 도구와 같은 형식).
    """
    from datetime import datetime, timedelta, timezone
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    hi = now + timedelta(days=within_days)

    def kst(s):
        if not s:
            return None
        return (datetime.fromisoformat(s.replace("Z", "+00:00"))
                .astimezone(KST).isoformat(timespec="seconds"))

    out = []
    for c in canvas._match_course(course):
        for it in items(c["id"]):
            if not (it["is_video"] and it["use_attendance"]):
                continue
            if unwatched_only and it["watched"]:
                continue
            close = it["close_at"]
            if not close:
                continue
            c_kst = datetime.fromisoformat(close.replace("Z", "+00:00")).astimezone(KST)
            if c_kst > hi:
                continue
            out.append({
                "course": c["short"],
                "module": it["module"],
                "title": it["title"],
                "minutes": it["minutes"],
                "open_at": kst(it["open_at"]),
                "close_at": kst(close),
                "overdue": c_kst < now,
                "period_status": it["period_status"],
                "watched": it["watched"],
                "url": it["url"],                        # LTI 주소 — 식별자로만 쓴다(열리지 않는다)
                "open_url": open_url(it["url"], c["id"]),  # 사람이 누를 링크
            })
    # 임박한 것 먼저, 이미 지난 것(놓친 영상)은 뒤로 — 안 그러면 학기 초 미시청분이
    # 상한 30행을 채워서 정작 급한 게 밀려난다.
    out.sort(key=lambda r: (r["overdue"], r["close_at"]))
    return out[:limit or CAP_LECTURES]
