"""/erion/web — 대시보드. 과제·할 일·강의영상을 네 가지 시선으로 보여준다.

- 거의 읽기 전용이다. 블록을 만들고 고치는 건 claude.ai 챗의 erion 커넥터(item_save 등)가
  하고, 여기서 쓰는 건 **완료 표시 하나뿐**이다 (POST /web/done → tools.mark_done).
- 로그인은 erion 비밀번호(ERION_AS_PASSWORD) 하나 → 서명 쿠키 30일. 단일 사용자.
- 디자인은 여백(pdfreader/src/App.css)의 토큰을 옮겨왔다. 여백을 바꿔도 여기는 안 따라온다.
- nginx 가 /erion 을 벗기므로 앱 안에서는 /web 이다. 브라우저에 주는 절대경로(폼 action,
  리다이렉트, 쿠키 Path)는 ERION_ISSUER 의 경로(/erion)를 붙여 만든다.

2026-09-20 개편 — 카드 그리드를 걷어내고 **행(row) + 탭 네 개**로 바꿨다.
항목이 70개를 넘으니 모든 블록이 같은 무게의 카드로 깔리면 읽을 수가 없었다.

  오늘 · 3일 · 우선순위 · 달력

강의영상(LearningX 출석 인정기간)이 이때 처음 들어왔다. **DB 가 아니라 실시간 조회**라
(learningx.brief, 5분 캐시) 실패해도 대시보드 전체가 죽지 않게 감싸고, 실패하면 그 줄만
조용히 비운 뒤 안내 한 줄을 남긴다.

2026-09-25 — 출처를 색으로 가른다(노랑 Canvas · 보라 LearningX 출석 · 분홍 내가 추가).
할 일·영상의 날짜는 두 칸이다: due_at(진짜 마감) · target_at(내가 정한 목표일). 목표일은
D-n·지난 마감·마감 푸시에 끼지 않는다. 시트에서 예상시간을 고친다(POST /web/est → est_adjust).

2026-09-20 완료 버튼 — 시트 안에 "완료" 가 생겼다. **Canvas·LearningX 가 뭐라 하든 무관하다.**
손으로 찍으면 done_mark 에 남고(과제·강의영상) 목록에서 내려간다. 되돌리는 길은 두 개다:
찍은 직후 시트에 뜨는 "되돌리기", 그리고 오늘 탭 맨 아래 "완료함" 줄.
"""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config, db, learningx, planner, push, tools

PREFIX = urlsplit(config.ISSUER).path.rstrip("/")        # "/erion"
BASE = f"{PREFIX}/web"
COOKIE = "erion_web"
COOKIE_DAYS = 30
# 쿠키는 /erion 전체에 붙는다 — /erion/ 에서도 로그인을 알아봐야 해서다.
# 예전 판은 Path=/erion/web 이었다. 로그아웃 때 옛 경로도 같이 지운다.
COOKIE_PATH = PREFIX or "/"
COOKIE_PATH_OLD = BASE
SECRET_FILE = config.DATA_DIR / "web_secret"

# 달력이 학기 전체를 그려야 해서 MCP 상한(40)보다 넉넉히 받는다.
SPAN_DAYS = 120
PAST_DAYS = 14
ROW_CAP = 400
NOTICE_DAYS = 60        # 대시보드 공지 목록 — 학기 앞쪽 안내(수업 운영 방식)까지 닿게
NOTICE_CAP = 60

# 로그인 연속 실패 제한 — 공개 인터넷에 비밀번호 폼이 하나 더 생기는 거라서.
FAIL_MAX, FAIL_WINDOW = 5, 600
_fails: list[float] = []


def _secret() -> bytes:
    try:
        return SECRET_FILE.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


def _sign(exp: int) -> str:
    mac = hmac.new(_secret(), f"web:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _authed(request: Request) -> bool:
    v = request.cookies.get(COOKIE, "")
    try:
        exp_s, _ = v.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(v, _sign(exp))


def _json_for_script(obj) -> str:
    # </script> 탈출 방지. 값은 JS 가 textContent 로만 넣는다(innerHTML 안 씀).
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


_HEAD = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=robots content=noindex>
<title>erion 대시보드</title>
<style>
:root{
  --sans:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",Pretendard,"Noto Sans KR","Segoe UI",system-ui,sans-serif;
  --serif:"Iowan Old Style",Charter,"Palatino Linotype",Georgia,"Apple SD Gothic Neo",serif;
  --mark:#FFD84D;
  --desk:#201F1D;--desk2:#2A2926;--desk3:#38362F;
  --ink:#F1EFE9;--sub:#D6D2CA;--muted:#8E8A81;--faint:#5F5B54;
  --line:rgba(255,255,255,.09);--line2:rgba(255,255,255,.14);
  --accBg:rgba(255,216,77,.10);--accBd:rgba(255,216,77,.24);--accInk:#D9BC55;
  --mark2:#6FD3C0;--vid:#9DB8FF;--lec:#C79BFF;--mine:#FF8FB8;
  --warnBg:rgba(228,113,63,.14);--warnBd:rgba(228,113,63,.4);--warnFg:#FFB9A3;
  --hot:#E4713F;
  --glass:rgba(26,25,23,.66);--sheet:#232220;--input:rgba(0,0,0,.22);
  --shadow:0 26px 70px rgba(0,0,0,.6);
  color-scheme:dark}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){
  --desk:#F4F1EB;--desk2:#FFFEFB;--desk3:#EAE5DB;
  --ink:#15130F;--sub:#3A362F;--muted:#8C877C;--faint:#A39D91;
  --line:rgba(21,19,15,.09);--line2:rgba(21,19,15,.16);
  --accBg:rgba(255,216,77,.2);--accBd:rgba(201,162,39,.32);--accInk:#8A6F14;
  --mark2:#1F9E8B;--vid:#3F63C7;--lec:#7A3FC7;--mine:#C2336E;
  --warnBg:rgba(228,113,63,.10);--warnBd:rgba(196,84,38,.32);--warnFg:#8A3A15;
  --hot:#C45426;
  --glass:rgba(244,241,235,.8);--sheet:#FFFEFB;--input:#FFFEFB;
  --shadow:0 26px 70px rgba(21,19,15,.22);color-scheme:light}}
:root[data-theme="light"]{
  --desk:#F4F1EB;--desk2:#FFFEFB;--desk3:#EAE5DB;
  --ink:#15130F;--sub:#3A362F;--muted:#8C877C;--faint:#A39D91;
  --line:rgba(21,19,15,.09);--line2:rgba(21,19,15,.16);
  --accBg:rgba(255,216,77,.2);--accBd:rgba(201,162,39,.32);--accInk:#8A6F14;
  --mark2:#1F9E8B;--vid:#3F63C7;--lec:#7A3FC7;--mine:#C2336E;
  --warnBg:rgba(228,113,63,.10);--warnBd:rgba(196,84,38,.32);--warnFg:#8A3A15;
  --hot:#C45426;
  --glass:rgba(244,241,235,.8);--sheet:#FFFEFB;--input:#FFFEFB;
  --shadow:0 26px 70px rgba(21,19,15,.22);color-scheme:light}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--desk);color:var(--ink);font:15px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
a{color:inherit}
.wrap{max-width:760px;margin:0 auto;padding:0 16px calc(48px + env(safe-area-inset-bottom))}
.bar{display:flex;align-items:center;justify-content:space-between;padding:18px 0 4px}
.brand{font-family:var(--serif);font-size:19px;letter-spacing:.05em}
.brand b{font-weight:400;color:var(--mark)}
.tools{display:flex;gap:6px}
.ib{height:32px;min-width:32px;padding:0 10px;border-radius:9px;border:1px solid var(--line);
  color:var(--muted);font-size:12.5px;display:inline-flex;align-items:center;justify-content:center;
  text-decoration:none;background:var(--desk2)}
.ib:active{background:var(--desk3)}

/* 머리말 */
.greet{padding:22px 2px 6px}
.stamp{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.headline{margin-top:9px;font-family:var(--serif);font-size:27px;line-height:1.32}
.headline em{font-style:normal;background:linear-gradient(transparent 62%,var(--accBd) 62%)}
.subline{margin-top:7px;font-size:13.5px;line-height:1.7;color:var(--sub)}

/* 탭 */
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:4px;margin:16px 0 4px;padding:8px 0;
  background:linear-gradient(var(--desk) 72%,transparent);overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:1 0 auto;min-width:74px;height:36px;padding:0 14px;border-radius:10px;
  border:1px solid var(--line);background:var(--desk2);color:var(--muted);font-size:13.5px;
  display:inline-flex;align-items:center;justify-content:center;gap:6px;white-space:nowrap}
.tab.on{background:var(--ink);color:var(--desk);border-color:var(--ink);font-weight:600}
.tab .n{font-size:11.5px;opacity:.75;font-variant-numeric:tabular-nums}
.tab.on .n{opacity:.7}

/* 구획 */
.sect{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  margin:22px 2px 8px;display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.sect span:last-child{letter-spacing:.04em;text-transform:none;color:var(--faint)}
.sect.hot{color:var(--warnFg)}

/* 행 — 카드 대신 이것이 기본 단위다 */
.rows{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:13px;
  overflow:hidden;background:var(--desk2)}
.row{position:relative;display:grid;grid-template-columns:58px 1fr auto;gap:11px;align-items:center;
  width:100%;text-align:left;padding:11px 13px 11px 15px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row:active{background:var(--desk3)}
.row::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2)}
.row.assignment::before{background:var(--mark)}
/* 띠 색 = 어디서 왔나. 노랑 Canvas 과제 · 보라 LearningX 강의(출석) · 분홍 내가 챗으로 추가한 것 */
.row.task::before,.row.video::before{background:var(--mine)}
.row.lecture::before{background:var(--lec);width:4px}
.row.overdue{background:linear-gradient(var(--warnBg),var(--warnBg))}
.row.overdue::before{background:var(--hot)}
.when{text-align:right;font-variant-numeric:tabular-nums;line-height:1.25}
.when b{display:block;font-size:13.5px;font-weight:600;color:var(--ink)}
.when i{display:block;font-size:11px;font-style:normal;color:var(--faint)}
.row.overdue .when b{color:var(--warnFg)}
/* 목표일 — 내가 정한 날. 진짜 마감이 아니라서 굵기를 빼고 분홍으로 */
.when.soft b{font-weight:500;color:var(--mine)}
.when.soft i{color:var(--mine);opacity:.75}
.mid{min-width:0}
.mid .lbl{font-size:14.5px;line-height:1.35;color:var(--ink);overflow-wrap:anywhere;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.mid .sub{margin-top:3px;font-size:11.5px;color:var(--muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.tail{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex:none}
.chip{font-size:11px;line-height:1;padding:4px 6px;border-radius:6px;border:1px solid var(--line);
  color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.chip.est{color:var(--sub)}
.chip.guess{border-style:dashed}
.chip.pts{color:var(--accInk);border-color:var(--accBd)}
.chip.lec{color:var(--lec);border-color:var(--lec)}
.chip.mine{color:var(--mine);border-color:var(--mine)}

/* 우선순위 — 순위와 점수 막대 */
.row.rank{grid-template-columns:22px 1fr auto}
.rk{font-family:var(--serif);font-size:16px;color:var(--faint);text-align:center;
  font-variant-numeric:tabular-nums}
.rk.top{color:var(--mark)}
.pbar{margin-top:5px;height:3px;border-radius:2px;background:var(--line);overflow:hidden}
.pbar i{display:block;height:100%;background:var(--mark);border-radius:2px}
.row.overdue .pbar i{background:var(--hot)}

/* 달력 */
.cal{border:1px solid var(--line);border-radius:13px;background:var(--desk2);padding:12px 10px 10px}
.calbar{display:flex;align-items:center;justify-content:space-between;padding:0 4px 10px}
.calbar .mo{font-family:var(--serif);font-size:18px}
.calnav{display:flex;gap:6px}
.dows{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;padding-bottom:4px}
.dows span{text-align:center;font-size:10.5px;color:var(--faint)}
.dows span:first-child{color:var(--warnFg)}
.days{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.day{aspect-ratio:1/1;border-radius:9px;border:1px solid transparent;display:flex;
  flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:2px;min-height:40px}
.day.mute{color:var(--faint);opacity:.45}
.day.today{border-color:var(--accBd);background:var(--accBg)}
.day.sel{background:var(--ink);color:var(--desk)}
.day.sel .dots i.more{color:var(--desk)}
.day .dnum{font-size:12.5px;font-variant-numeric:tabular-nums}
.day.has .dnum{font-weight:600}
.dots{display:flex;gap:2px;height:5px;align-items:center}
.dots i{width:5px;height:5px;border-radius:50%;display:block}
.dots i.assignment{background:var(--mark)}
.dots i.task,.dots i.video{background:var(--mine)}
.dots i.soft{background:none;box-shadow:inset 0 0 0 1.2px var(--mine)}
.dots i.lecture{background:var(--lec)}
.dots i.more{width:auto;height:auto;border-radius:0;font-size:9px;color:var(--muted);
  font-style:normal;line-height:5px}
.day.over .dnum{color:var(--hot)}
.legend{display:flex;flex-wrap:wrap;gap:10px;padding:10px 4px 2px;font-size:11px;color:var(--muted)}
.legend b{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;
  vertical-align:middle}

.empty{padding:34px 8px;text-align:center;color:var(--muted);font-size:14px;line-height:1.8}
.note{margin-top:10px;font-size:12px;line-height:1.7;color:var(--faint);padding:0 2px}
.hint{margin-top:30px;font-size:12.5px;line-height:1.8;color:var(--faint)}
.hint code{font-family:inherit;color:var(--muted)}

/* 시트 */
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;
  transition:opacity .18s;z-index:10}
.scrim.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;left:50%;bottom:0;transform:translate(-50%,100%);width:min(640px,100%);
  max-height:86dvh;overflow-y:auto;overscroll-behavior:contain;background:var(--sheet);
  border:1px solid var(--line2);border-bottom:0;border-radius:18px 18px 0 0;box-shadow:var(--shadow);
  padding:10px 20px calc(24px + env(safe-area-inset-bottom));transition:transform .22s ease;z-index:11}
.sheet.on{transform:translate(-50%,0)}
.grab{width:38px;height:4px;border-radius:2px;background:var(--line2);margin:2px auto 14px}
.s-kind{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.s-title{margin:8px 0 2px;font-family:var(--serif);font-size:24px;line-height:1.3;overflow-wrap:anywhere}
.s-orig{font-size:12.5px;color:var(--faint);overflow-wrap:anywhere}
.s-grid{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:16px 0 4px;font-size:13.5px}
.s-grid dt{color:var(--muted)}
.s-grid dd{margin:0;color:var(--sub);overflow-wrap:anywhere}
.s-h{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:20px 0 8px}
.s-prep{padding:10px 12px;border-radius:10px;background:var(--accBg);border:1px solid var(--accBd);
  font-size:13.5px;line-height:1.65;color:var(--ink)}
.s-desc{white-space:pre-wrap;font-size:14px;line-height:1.75;color:var(--sub);overflow-wrap:anywhere}
.s-desc.none{color:var(--faint)}
.s-act{display:flex;gap:8px;margin-top:22px}
.s-act+.s-act{margin-top:8px}
.btn{flex:1;height:44px;padding:0 16px;border-radius:11px;display:inline-flex;align-items:center;justify-content:center;
  font-size:14.5px;text-decoration:none;border:1px solid var(--line2);color:var(--ink)}
.btn.primary{background:var(--mark);border-color:var(--mark);color:#241F00;font-weight:600}
/* 완료는 링크(노랑)와 경쟁하면 안 된다 — 색을 나눠 준다. 할 일 띠와 같은 초록이다 */
.btn.done{border-color:var(--mark2);color:var(--mark2);font-weight:600}
.btn.undo{background:none;border-color:var(--line2);color:var(--sub);font-weight:400}
/* 완료된 상태는 글 대신 버튼 자체가 말한다 — 채워진 초록. 다시 누르면 취소 */
.btn.done.on{background:var(--mark2);color:var(--desk);transition:transform .15s}
.btn.done.pop{animation:pop .35s ease-out}
@keyframes pop{40%{transform:scale(1.06)}}
.btn[disabled]{opacity:.5}
.s-title.did{text-decoration:line-through;text-decoration-thickness:1.5px;color:var(--muted)}
/* 바깥에서 끝난 것(Canvas 제출·시청) — 짧은 배지 하나 */
.s-done{display:inline-block;margin-top:10px;padding:3px 9px;border-radius:7px;font-size:12px;
  background:var(--accBg);border:1px solid var(--accBd);color:var(--accInk)}
/* 완료를 찍으면 줄이 접히며 빠진다 */
.row.leaving{animation:leave .32s ease-in forwards;overflow:hidden;pointer-events:none}
@keyframes leave{to{opacity:0;transform:translateX(24px);max-height:0;padding-top:0;padding-bottom:0}}
.row.leaving{max-height:120px}
.fx{position:fixed;left:0;top:0;width:7px;height:7px;border-radius:2px;z-index:80;pointer-events:none}
.s-err{margin-top:10px;font-size:12.5px;line-height:1.6;color:var(--warnFg)}
/* 완료함 — 지운 게 아니라 내려놓은 것뿐이라는 표시. 눌러서 되돌린다 */
.rows.did .row{grid-template-columns:1fr auto;opacity:.62}
.rows.did .row .lbl{text-decoration:line-through;-webkit-line-clamp:1}
.rows.did .row::before{opacity:.45}
.didmore{margin-top:8px;font-size:12px;color:var(--faint);padding:0 2px}
/* 이번 주 — 하루 한 장. 머리(날짜·가용시간) · 막힌 것 · 할 것 · 이미 한 것 */
.wday{border:1px solid var(--line);border-radius:13px;background:var(--desk2);margin-bottom:10px;overflow:hidden}
.wday.today{border-color:var(--accBd)}
.wh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:11px 14px 7px}
.wh .dt{font-family:var(--serif);font-size:17px}
.wh .dt small{font-family:var(--sans);font-size:11px;color:var(--accInk);margin-left:7px;letter-spacing:.04em}
.wh .ld{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.wday.over .wh .ld{color:var(--warnFg)}
.meter{height:3px;background:var(--line);margin:0 14px 9px;border-radius:2px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--mark2);border-radius:2px}
.wday.over .meter i{background:var(--hot)}
.busy{display:flex;flex-wrap:wrap;gap:5px;padding:0 14px 10px}
.bz{font-size:11px;line-height:1.2;padding:3px 7px;border-radius:6px;border:1px solid var(--line);
  color:var(--muted);font-variant-numeric:tabular-nums}
.bz.event{color:var(--mark2);border-color:var(--mark2)}
.bz.allday{color:var(--warnFg);border-color:var(--warnBd);background:var(--warnBg)}
.bz.mark{border-style:dashed;color:var(--sub)}
.wday .rows{border:0;border-radius:0;border-top:1px solid var(--line);background:none}
.row.wt{grid-template-columns:58px 1fr}
.row.wt .hrs{font-size:14px;font-weight:600;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--ink)}
.row.wt .hrs i{display:block;font-size:10.5px;font-style:normal;font-weight:400;color:var(--faint)}
.why{margin-top:4px;font-size:11.5px;line-height:1.5;color:var(--accInk)}
.why.pin{color:var(--mark2)}
.sub .late{color:var(--warnFg)}
.sub .tgt{color:var(--mine)}
.row.drag{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.row.lifted{opacity:.35}
body.dragging{-webkit-user-select:none;user-select:none;cursor:grabbing}
.wday.drop{border-color:var(--mark2);box-shadow:0 0 0 2px var(--mark2) inset}
.ghost{position:fixed;left:0;top:0;z-index:60;pointer-events:none;margin:-22px 0 0 -40px;
  max-width:70vw;padding:9px 13px;border-radius:10px;background:var(--desk3);color:var(--ink);
  border:1px solid var(--line2);box-shadow:var(--shadow);font-size:13.5px;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toast{position:fixed;left:50%;bottom:22px;z-index:70;transform:translate(-50%,20px);opacity:0;
  pointer-events:none;transition:.2s;max-width:calc(100vw - 32px);padding:10px 16px;border-radius:12px;
  background:var(--desk3);color:var(--ink);border:1px solid var(--line2);font-size:13.5px}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast.err{border-color:var(--warnBd);color:var(--warnFg)}
.s-move{margin-top:14px}
.estbox{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.estbox .v{min-width:58px;text-align:center;font-variant-numeric:tabular-nums;color:var(--ink);font-weight:600}
.estbox .st{width:34px;height:30px;border-radius:8px;border:1px solid var(--line2);font-size:16px;line-height:1}
.estbox .sv{height:30px;padding:0 12px;border-radius:8px;background:var(--mark2);color:var(--desk);font-size:13px;font-weight:600}
.estbox .sv[hidden]{display:none}
.estbox .nt{font-size:11.5px;color:var(--faint)}
.mvs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.mv{font:inherit;font-size:13px;padding:7px 10px;border-radius:9px;border:1px solid var(--line2);
  background:var(--desk2);color:var(--sub);cursor:pointer;touch-action:manipulation}
.mv.cur{border-color:var(--mark2);color:var(--mark2);cursor:default}
.s-move .btn{flex:none;margin-top:10px;height:36px;font-size:13px}
.wdone{padding:8px 14px 10px;font-size:12px;line-height:1.7;color:var(--faint);border-top:1px solid var(--line)}
.wdone s{color:var(--muted)}
.wempty{padding:2px 14px 12px;font-size:12.5px;line-height:1.7;color:var(--faint)}
.warnbox{margin-top:14px;padding:10px 12px;border-radius:10px;background:var(--warnBg);
  border:1px solid var(--warnBd);color:var(--warnFg);font-size:13px;line-height:1.65}
/* 달력의 일정 — 마감(점)과 섞이지 않게 가는 막대 */
.evbar{width:16px;height:2px;border-radius:1px;background:var(--mark2)}
.day.sel .evbar{background:var(--desk)}
.dots i.did{opacity:.3}
.evs{border:1px solid var(--line);border-radius:13px;background:var(--desk2);overflow:hidden}
.ev{display:grid;grid-template-columns:96px 1fr;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13.5px}
.ev:last-child{border-bottom:0}
.ev .t{color:var(--muted);font-variant-numeric:tabular-nums;font-size:12.5px}
.didmore{cursor:pointer;text-decoration:underline;text-underline-offset:3px}
/* 로그인 */
.login{min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px 16px}
.login form{width:min(340px,100%);display:flex;flex-direction:column;gap:12px}
.login h1{margin:0 0 6px;font-family:var(--serif);font-weight:400;font-size:28px}
.login p{margin:0 0 8px;color:var(--muted);font-size:13.5px}
.login input{height:46px;padding:0 14px;border-radius:11px;border:1px solid var(--line2);
  background:var(--input);color:var(--ink);font:inherit;font-size:16px;outline:none}
.login input:focus{border-color:var(--accBd);box-shadow:0 0 0 3px var(--accBg)}
.login .err{color:var(--warnFg);font-size:13px}
/* PIN — 여백과 같은 형식(숫자 6자리, 다 차면 자동 제출). 여백 App.css 의 pw6/vb-pad/vb-key 이식 */
.login .pw6{width:100%;height:58px;text-align:center;font-size:26px;letter-spacing:.42em;
  text-indent:.42em;font-variant-numeric:tabular-nums;border-radius:12px;
  border:1px solid var(--line2);background:var(--input);color:var(--ink);outline:none}
.login .pw6:focus{border-color:var(--accBd);box-shadow:0 0 0 3px var(--accBg)}
.login .dots{display:flex;justify-content:center;gap:10px;margin:14px 0 2px;height:auto}
.login .dots i{width:9px;height:9px;border-radius:50%;background:var(--line2);display:block;
  transition:background .12s,transform .12s}
.login .dots i.on{background:var(--mark);transform:scale(1.15)}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:18px}
.key{height:54px;border-radius:11px;background:var(--desk2);border:1px solid var(--line);
  font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--ink);
  display:flex;align-items:center;justify-content:center;touch-action:manipulation}
.key:active{background:var(--desk3)}
.key.sm{font-size:15px;color:var(--muted)}
.key.go{background:var(--mark);color:#241F00;border-color:transparent;font-size:16px}
.key.go:disabled{opacity:.35}
@media (pointer:coarse){.key{height:62px;font-size:22px}}

/* ── 살아 있는 층 (2026-09-25 "너무 정적이야 · 혼자 공부하면 외롭잖아").
   글로 응원하지 않는다(톤은 건조하게) — 대신 시간이 흐르는 게 보이고, 하는 동안 옆에 뭔가 숨 쉰다.
   움직이는 건 transform·opacity 뿐(합성기 몫, 다시 그리기 없음). 동작 줄이기 설정이면 전부 멈춘다. */
.amb{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden;--ao:.08}
:root[data-theme="light"] .amb{--ao:.16}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]) .amb{--ao:.16}}
.amb i{position:absolute;width:80vmax;height:80vmax;border-radius:50%;opacity:var(--ao);
  will-change:transform;transition:opacity 2s}
/* 첫째 = 시간대 빛(아침 노랑 · 낮 청록 · 저녁 주황 · 밤 보라). 지난 마감이 있으면 주황으로 */
.amb i:nth-child(1){left:-30vmax;top:-38vmax;background:radial-gradient(closest-side,var(--tod,var(--mark)),transparent);
  animation:drift1 47s ease-in-out infinite alternate}
.amb i:nth-child(2){right:-38vmax;top:18vh;background:radial-gradient(closest-side,var(--mark2),transparent);
  animation:drift2 61s ease-in-out infinite alternate}
.amb i:nth-child(3){left:-20vmax;bottom:-50vmax;background:radial-gradient(closest-side,var(--lec),transparent);
  animation:drift3 73s ease-in-out infinite alternate}
body.hot .amb i:nth-child(1){background:radial-gradient(closest-side,var(--hot),transparent)}
/* 하는 중이면 청록이 앞으로 나와서 숨을 쉰다 */
body.focusing .amb i:nth-child(2){opacity:calc(var(--ao)*2);animation:drift2 61s ease-in-out infinite alternate,
  swell 6s ease-in-out infinite}
@keyframes drift1{to{transform:translate(18vmax,14vmax) scale(1.15)}}
@keyframes drift2{to{transform:translate(-16vmax,20vmax) scale(.9)}}
@keyframes drift3{to{transform:translate(24vmax,-10vmax) scale(1.2)}}
@keyframes swell{50%{scale:1.12}}
/* 빛은 z-index:-1 로 깐다. .wrap 에 z-index 를 주면 쌓임 맥락이 생겨 시트가 가림막(scrim) 밑으로 깔린다 —
   대신 body 배경을 비우고 html 배경만 남긴다 */
body{background:transparent}
/* 탭 줄은 평소엔 투명하다 — 위에 붙어 목록 위로 떠 있을 때(.stuck)만 유리판이 깔린다.
   늘 깔아 두면 머리말 아래에 색이 다른 띠가 생겨 보였다(2026-09-26 사용자). */
.tabs{background:transparent;transition:background .25s;
  box-shadow:none}
.tabs.stuck{background:var(--glass);-webkit-backdrop-filter:blur(14px) saturate(1.3);backdrop-filter:blur(14px) saturate(1.3);
  box-shadow:0 1px 0 var(--line);
  margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px}
.tab{transition:background .25s,color .25s,border-color .25s}

/* 브랜드 점 — 항상 천천히 숨 쉰다. 여기 누가 있다는 표시 */
.brand b{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--mark);margin-left:3px;
  vertical-align:.1em;position:relative}
.brand b::after{content:"";position:absolute;inset:-4px;border-radius:50%;background:var(--mark);opacity:0;
  animation:halo 5.5s ease-in-out infinite}
@keyframes halo{0%,100%{opacity:0;transform:scale(.6)}45%{opacity:.28;transform:scale(1.35)}}
.stamp .clk{color:var(--muted)}
.stamp .clk b{font-weight:400;animation:blink 2s steps(1) infinite}
@keyframes blink{50%{opacity:.25}}

/* 하루 띠 — 09–24시 창 위로 '지금'이 흘러간다. 수업·일정 구간이 박혀 있다 */
.band{margin-top:16px}
.band .trk{position:relative;height:22px;border-radius:7px;background:var(--desk2);border:1px solid var(--line);overflow:hidden}
.band .gone{position:absolute;left:0;top:0;bottom:0;background:var(--line);transition:width 1s linear}
.band .seg{position:absolute;top:5px;bottom:5px;border-radius:3px;background:var(--line2)}
.band .seg.class{background:color-mix(in srgb,var(--sub) 28%,transparent)}
.band .seg.event{background:color-mix(in srgb,var(--mark2) 45%,transparent)}
.band .seg.past{opacity:.35}
.band .now{position:absolute;top:-1px;bottom:-1px;width:2px;margin-left:-1px;background:var(--mark);
  transition:left 1s linear;box-shadow:0 0 10px var(--mark)}
.band .now::after{content:"";position:absolute;left:50%;top:50%;width:8px;height:8px;margin:-4px 0 0 -4px;
  border-radius:50%;background:var(--mark);animation:halo 3.2s ease-in-out infinite}
.band .tk{position:absolute;bottom:2px;font-size:9px;color:var(--faint);transform:translateX(-50%);
  font-variant-numeric:tabular-nums;pointer-events:none}
.band .meta{display:flex;justify-content:space-between;gap:12px;margin-top:7px;font-size:12px;color:var(--muted);
  font-variant-numeric:tabular-nums}
.band .meta b{font-weight:500;color:var(--ink)}
.band .meta span:last-child{text-align:right;white-space:nowrap}

/* 남은 시간이 굴러간다 — 12시간 안쪽 마감 */
.when i.left{color:var(--accInk)}
.row.overdue .when i.left{color:var(--warnFg)}
.row.overdue::before{animation:throb 2.6s ease-in-out infinite}
@keyframes throb{50%{opacity:.35}}

/* 들어올 때 — 한 장씩 떠오른다 */
.rise{animation:rise .55s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i,0)*45ms)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
.meter i,.pbar i{transform-origin:left;animation:grow 1s cubic-bezier(.2,.8,.2,1) both .15s}
@keyframes grow{from{transform:scaleX(0)}}
.sheet{transition:transform .38s cubic-bezier(.2,.9,.25,1.05)}

/* 지금 하는 것 — 줄의 띠가 흐른다 */
.row.live::before{width:4px;background:linear-gradient(var(--mark2),var(--mark),var(--mark2));
  background-size:100% 300%;animation:flow 3s linear infinite}
@keyframes flow{to{background-position:0 300%}}
.chip.live{color:var(--mark2);border-color:var(--mark2)}
.chip.live::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--mark2);
  margin-right:4px;vertical-align:.1em;animation:blink 1.6s ease-in-out infinite}
.btn.go{border-color:var(--line2);color:var(--sub)}
.btn.go.on{border-color:var(--mark2);color:var(--mark2)}
.spent{display:flex;align-items:center;gap:8px}
.spent .pb{flex:1;max-width:140px;height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.spent .pb i{display:block;height:100%;background:var(--mark2)}

/* 곁에 있는 것 — 하는 동안 바닥에 떠 있는 작은 판 */
.dock{position:fixed;left:50%;bottom:calc(14px + env(safe-area-inset-bottom));z-index:9;width:min(520px,calc(100% - 24px));
  display:flex;align-items:center;gap:12px;padding:9px 9px 9px 12px;border-radius:18px;
  background:var(--glass);-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);
  border:1px solid var(--line2);box-shadow:var(--shadow);
  transform:translate(-50%,0);transition:transform .45s cubic-bezier(.2,.9,.25,1.1),opacity .3s}
.dock[hidden]{display:flex;transform:translate(-50%,140%);opacity:0;pointer-events:none}
.orb{position:relative;width:40px;height:40px;flex:none}
.orb svg{position:absolute;inset:0;transform:rotate(-90deg)}
.orb circle{fill:none;stroke-width:3}
.orb .bg{stroke:var(--line2)}
.orb .fg{stroke:var(--mark2);stroke-linecap:round;transition:stroke-dashoffset 1s linear}
.orb .fg.over{stroke:var(--hot)}
.orb i{position:absolute;left:50%;top:50%;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
  background:var(--mark2);animation:breath 6s ease-in-out infinite}
.orb i::after{content:"";position:absolute;inset:-5px;border-radius:50%;background:var(--mark2);opacity:.25;
  animation:breath 6s ease-in-out infinite reverse}
/* 4초 들이쉬고 · 2초 머물고 · 내쉬는 박자 */
@keyframes breath{0%,100%{transform:scale(.72)}40%,55%{transform:scale(1.12)}}
.dock .dt{flex:1;min-width:0}
.dock .dl{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dock .ds{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dock .ds b{font-weight:600;color:var(--mark2);font-size:13px}
.dock .db{height:36px;padding:0 12px;border-radius:11px;border:1px solid var(--line2);font-size:13px;flex:none}
.dock .db.fin{border-color:var(--mark2);color:var(--mark2);font-weight:600}
body.focusing .wrap{padding-bottom:calc(110px + env(safe-area-inset-bottom))}
body.focusing .toast{bottom:calc(84px + env(safe-area-inset-bottom))}

@media (prefers-reduced-motion:reduce){
  .amb i,.brand b::after,.stamp .clk b,.band .now::after,.row.overdue::before,.row.live::before,
  .chip.live::before,.orb i,.orb i::after,.rise,.meter i,.pbar i{animation:none!important}
  .band .gone,.band .now,.orb .fg{transition:none}}

/* ── 화면 크기 세 단계. 폰/태블릿을 다른 페이지로 나누지 않는다 —
   두 벌이 되면 한쪽만 고쳐져서 어긋난다(이 프로젝트가 문서로 이미 겪은 실패다).
   같은 마크업에 중단점만 둔다. */
.main{display:block}

/* 큰 폰 · 태블릿 세로 — 글자와 손가락 목표를 키운다 */
@media (min-width:560px){
  html,body{font:16px/1.55 var(--sans)}
  .wrap{max-width:820px;padding:0 22px calc(56px + env(safe-area-inset-bottom))}
  .headline{font-size:31px}
  .row{grid-template-columns:70px 1fr auto;padding:13px 15px 13px 17px}
  .mid .lbl{font-size:15.5px}
  .when b{font-size:14.5px}
  .tabs{justify-content:flex-start;margin-left:-22px;margin-right:-22px;padding-left:22px;padding-right:22px}
  .tab{flex:0 0 auto;height:40px;font-size:14.5px;min-width:96px}
  .day{min-height:56px}
  .day .dnum{font-size:14px}
  .dots i{width:6px;height:6px}
}

/* 태블릿 가로 — 목록과 상세를 나란히. 바텀시트 대신 오른쪽 기둥이 된다 */
@media (min-width:900px){
  .wrap{max-width:1140px}
  .main{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:24px;align-items:start}
  .tabs{position:static;background:none;-webkit-backdrop-filter:none;backdrop-filter:none;margin-left:0;margin-right:0;padding-left:0;padding-right:0}
  .scrim{display:none}
  /* .sheet.on 의 transform 이 특이도에서 이기므로 두 선택자를 같이 적어야 한다.
     left/bottom 도 같이 풀지 않으면 sticky 의 오프셋으로 남아 화면을 덮는다. */
  .sheet,.sheet.on{position:sticky;top:16px;left:auto;bottom:auto;transform:none;width:auto;
    max-height:calc(100dvh - 48px);border:1px solid var(--line);border-radius:14px;
    padding:18px 20px 22px;box-shadow:none;background:var(--desk2)}
  .sheet .grab{display:none}
  .sheet .s-act .btn.close{display:none}
  .sheet.empty-pane{color:var(--muted);font-size:13.5px;line-height:1.8}
  .day{min-height:64px}
}

/* ── 2026-09-26 정리. 글자 두어 개만 다음 줄로 떨어지지 않게: 한국어는 어절 단위로만 끊고(keep-all),
   제목은 줄 길이를 고르게(balance), 본문은 마지막 줄이 너무 짧지 않게(pretty). */
body{word-break:keep-all;overflow-wrap:anywhere}
.headline,.s-title,.fz-title{text-wrap:balance}
.subline,.mid .lbl,.s-desc,.s-orig,.band .meta{text-wrap:pretty}

/* 톱니 하나 — 알림·테마·나가기 */
.tools{position:relative}
.gear{font-size:15px;width:36px;padding:0;position:relative;transition:transform .35s cubic-bezier(.2,.9,.25,1.2)}
.gear.on{transform:rotate(60deg)}
.gear.dot::after{content:"";position:absolute;right:5px;top:5px;width:6px;height:6px;border-radius:50%;background:var(--hot)}
.menu{position:absolute;right:0;top:40px;z-index:30;min-width:200px;padding:6px;border-radius:14px;
  background:var(--sheet);border:1px solid var(--line2);box-shadow:var(--shadow);display:flex;flex-direction:column;
  transform-origin:top right;animation:pop-in .22s cubic-bezier(.2,.9,.25,1.15)}
.menu[hidden]{display:none}
@keyframes pop-in{from{opacity:0;transform:scale(.85) translateY(-6px)}}
.mi{display:flex;align-items:center;height:42px;padding:0 12px;border-radius:9px;font-size:14px;color:var(--ink);
  text-decoration:none;text-align:left}
.mi:active{background:var(--desk3)}
.mi[hidden]{display:none}

/* 시트 — ✕ · 한 줄 메타 · 접히는 설명 */
.sheet{position:fixed}
.s-x{position:absolute;right:12px;top:10px;width:34px;height:34px;border-radius:50%;color:var(--muted);font-size:15px}
.s-x:active{background:var(--desk3)}
.s-kind{padding-right:40px}
.s-orig{margin-top:4px;font-size:12.5px;color:var(--muted)}
.s-desc.fold{max-height:5.3em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 55%,transparent);
  mask-image:linear-gradient(#000 55%,transparent);transition:max-height .35s ease}
.s-desc.fold.open{-webkit-mask-image:none;mask-image:none}
.s-more{margin-top:6px;font-size:12.5px;color:var(--accInk)}
.s-grid{align-items:center}
.s-grid dt{line-height:34px;align-self:start}
.s-grid dd{min-height:34px;display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.s-link{display:inline-flex;align-items:center;height:34px;margin-top:12px;padding:0 13px;border-radius:10px;
  border:1px solid var(--mark);color:var(--accInk);font-size:13.5px;font-weight:600;text-decoration:none}
:root:not([data-theme="light"]) .s-link{color:var(--mark)}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]) .s-link{color:var(--accInk)}}
.s-link:active{background:var(--accBg)}

/* 계획한 날 — 칩을 누르면 바퀴 */
.pchip{display:inline-flex;align-items:center}
.pchip i{font-style:normal;font-size:11px;color:var(--mark2);border:1px solid var(--mark2);border-radius:5px;padding:1px 4px}
.wheelbox{margin:10px 0 2px;animation:unfold .3s cubic-bezier(.2,.9,.25,1) both;transform-origin:top}
.wheelbox.bye{animation:unfold .24s ease-in reverse both}
@keyframes unfold{from{opacity:0;transform:scaleY(.6)}}
.wheel{position:relative;height:220px;border-radius:14px;background:var(--desk2);border:1px solid var(--line);overflow:hidden}
.wsc{height:100%;overflow-y:scroll;scroll-snap-type:y mandatory;scrollbar-width:none;padding:88px 0;
  perspective:420px;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
.wsc::-webkit-scrollbar{display:none}
.wsc.free{scroll-snap-type:none}
.wi{height:44px;scroll-snap-align:center;display:flex;align-items:center;justify-content:space-between;padding:0 22px;
  cursor:pointer;transform-origin:center;backface-visibility:hidden;font-variant-numeric:tabular-nums}
.wi b{font-weight:500;font-size:16px;color:var(--sub)}
.wi i{font-style:normal;font-size:12px;color:var(--faint)}
.wi.sel b{color:var(--ink);font-weight:600}
.wi.sel i{color:var(--mark2)}
.wi.cur b::after{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--mark2);margin-left:7px;vertical-align:.2em}
.wband{position:absolute;left:8px;right:8px;top:50%;height:44px;margin-top:-22px;border-radius:10px;pointer-events:none;
  border:1px solid var(--line2);background:color-mix(in srgb,var(--mark2) 7%,transparent)}
.wheel::before,.wheel::after{content:"";position:absolute;left:0;right:0;height:70px;z-index:1;pointer-events:none}
.wheel::before{top:0;background:linear-gradient(var(--desk2),transparent)}
.wheel::after{bottom:0;background:linear-gradient(transparent,var(--desk2))}
.wact{display:flex;gap:8px;margin-top:8px}
.wact .btn{height:40px;font-size:13.5px}
.wgo{border-color:var(--mark2);color:var(--mark2);font-weight:600}
.wgo[disabled]{border-color:var(--line2);color:var(--muted);opacity:1}
.wact .btn.undo{flex:none}

/* 온 화면 집중판 — 누른 자리에서 원으로 퍼진다 */
.fz{position:fixed;inset:0;z-index:40;background:var(--desk);display:flex;flex-direction:column;
  padding:calc(10px + env(safe-area-inset-top)) 18px calc(22px + env(safe-area-inset-bottom));
  clip-path:circle(0 at var(--ox,50%) var(--oy,100%));transition:clip-path .55s cubic-bezier(.3,.8,.2,1);overflow:hidden}
.fz[hidden]{display:none}
.fz.on{clip-path:circle(150vmax at var(--ox,50%) var(--oy,100%))}
.fz-glow{position:absolute;inset:-20%;pointer-events:none;opacity:.22;
  background:radial-gradient(40% 35% at 50% 45%,var(--mark2),transparent 70%);animation:swell 6s ease-in-out infinite}
.fz.paused .fz-glow{opacity:.07;animation:none}
.fz-top{position:relative;display:flex;align-items:center;justify-content:space-between;gap:10px}
.fz-ic{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;font-size:22px;color:var(--muted);flex:none}
.fz-ic:active{background:var(--desk3)}
.fz-k{font-size:11px;letter-spacing:.14em;color:var(--muted);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fz-mid{position:relative;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center}
.fz-title{font-family:var(--serif);font-size:25px;line-height:1.35;max-width:560px;
  opacity:0;transform:translateY(12px);transition:opacity .5s .25s,transform .5s .25s}
.fz.on .fz-title,.fz.on .fz-ring,.fz.on .fz-bot,.fz.on .fz-link{opacity:1;transform:none}
.fz-link{font-size:13px;color:var(--accInk);text-decoration:none;opacity:0;transition:opacity .5s .35s}
.fz-ring{position:relative;width:min(240px,62vw);aspect-ratio:1;margin-top:10px;
  opacity:0;transform:scale(.85);transition:opacity .6s .3s,transform .6s .3s cubic-bezier(.2,.9,.25,1.1)}
.fz-ring svg{position:absolute;inset:0;transform:rotate(-90deg)}
.fz-ring circle{fill:none;stroke-width:4}
.fz-ring .bg{stroke:var(--line2)}
.fz-ring .fg{stroke:var(--mark2);stroke-linecap:round;transition:stroke-dashoffset 1s linear}
.fz-ring .fg.over{stroke:var(--hot)}
.fz-orb{position:absolute;left:50%;top:50%;width:62%;height:62%;margin:-31% 0 0 -31%;border-radius:50%;
  background:radial-gradient(closest-side,color-mix(in srgb,var(--mark2) 30%,transparent),transparent);
  animation:breath 6s ease-in-out infinite}
.fz.paused .fz-orb{animation:none;opacity:.35}
.fz-clk{position:absolute;left:0;right:0;top:50%;margin-top:-26px;font-size:40px;font-weight:300;letter-spacing:.02em;
  font-variant-numeric:tabular-nums;color:var(--ink)}
.fz.paused .fz-clk{animation:blink 1.6s steps(1) infinite;color:var(--muted)}
.fz-sub{position:absolute;left:0;right:0;top:50%;margin-top:22px;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.fz-bot{position:relative;display:flex;align-items:center;justify-content:center;gap:14px;
  opacity:0;transform:translateY(16px);transition:opacity .5s .4s,transform .5s .4s}
.fz-pp{width:64px;height:64px;border-radius:50%;border:1px solid var(--line2);font-size:20px;color:var(--ink);flex:none;
  transition:transform .15s}
.fz-pp:active{transform:scale(.92)}
.fz.paused .fz-pp{border-color:var(--mark2);color:var(--mark2)}
.fz-fin{flex:0 1 200px;height:56px;border-radius:16px}
.fz-menu{position:absolute;left:12px;right:12px;bottom:calc(14px + env(safe-area-inset-bottom));z-index:2;padding:14px;
  border-radius:18px;background:var(--sheet);border:1px solid var(--line2);box-shadow:var(--shadow);
  display:flex;flex-direction:column;gap:8px;animation:rise .3s cubic-bezier(.2,.8,.2,1) both}
.fz-menu[hidden]{display:none}
.fz-menu .btn{flex:none;height:46px}
.fz-mq{font-size:13.5px;color:var(--sub);padding:2px 4px 6px;text-align:center}
.fz.asking .fz-bot{opacity:0;pointer-events:none}
body.fzon{overflow:hidden}
.dock{cursor:pointer}
.dock.paused .orb i{animation:none;opacity:.4}
.dock .db{width:42px;padding:0;font-size:14px}
@media (min-width:900px){.sheet .s-x{display:none}}
@media (prefers-reduced-motion:reduce){
  .fz{transition:none}.fz-glow,.fz-orb,.fz.paused .fz-clk{animation:none!important}
  .fz-title,.fz-ring,.fz-bot,.fz-link{transition:none}
  .menu,.wheelbox,.wheelbox.bye,.fz-menu{animation:none}}

/* 아이콘 (Bootstrap Icons) */
.ico{display:inline-flex;width:1em;height:1em;flex:none}
.ico svg{width:100%;height:100%}
.btn .ico{margin-right:7px;font-size:15px}
.fz-pp .ico{font-size:24px}
.fz-ic .ico{font-size:20px}
.s-x .ico{font-size:15px}
.dock .db .ico{font-size:17px}
.gear .ico{font-size:16px}
.s-link .ico,.fz-link .ico{font-size:12px;margin-left:6px}
.fz-link{display:inline-flex;align-items:center}
.s-x,.fz-pp,.dock .db,.gear{display:inline-flex;align-items:center;justify-content:center}
/* 예상·계획 — 같은 모양의 옅은 알약. 칸마다 테두리를 두르던 것(−/+/칩)을 한 덩어리로 */
.fld{display:inline-flex;align-items:center;height:34px;border-radius:10px;border:0;
  background:color-mix(in srgb,var(--ink) 7%,transparent);color:var(--ink);font-size:13.5px;font-variant-numeric:tabular-nums}
.estbox{gap:8px}
.estbox .fld.step{padding:0 2px}
.estbox .st{width:32px;height:30px;border:0;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;
  color:var(--muted);font-size:13px}
.estbox .st:active{background:color-mix(in srgb,var(--ink) 10%,transparent)}
.estbox .st .ico{margin:0}
.estbox .v{min-width:54px;font-weight:600}
.estbox .sv{height:34px;border-radius:10px;padding:0 13px;animation:pop-in .2s ease-out}
.pchip{gap:8px;padding:0 10px 0 11px}
.pchip>.ico{font-size:13px;color:var(--muted)}
.pchip .chev{font-size:11px;color:var(--muted);transition:transform .25s}
.pchip.on{box-shadow:inset 0 0 0 1px var(--mark2)}
.pchip.on .chev{transform:rotate(180deg)}

/* 하늘 띠 (2026-09-26) — 하루 띠를 대신한다. 하늘은 테마와 상관없이 하늘색이다 */
.band{margin-top:18px}
.sky{position:relative;height:60px;border-radius:14px;overflow:hidden;border:1px solid var(--line);
  box-shadow:inset 0 -18px 30px rgba(0,0,0,.18)}
.skyfx{position:absolute;inset:0;pointer-events:none}
.star{position:absolute;width:2px;height:2px;border-radius:50%;background:#fff;opacity:.75;animation:twinkle 4s ease-in-out infinite}
.star.big{width:3px;height:3px;box-shadow:0 0 4px #fff}
@keyframes twinkle{50%{opacity:.15}}
.wxc{position:absolute;top:0;bottom:0;overflow:hidden}
.cld{position:absolute;top:-30%;bottom:10%;border-radius:50%;background:radial-gradient(closest-side,rgba(190,198,212,.95),transparent);
  opacity:calc(var(--cl)*.45);filter:blur(4px)}
.cld.part{opacity:calc(var(--cl)*.3)}
.cld.rain,.cld.storm{background:radial-gradient(closest-side,rgba(110,120,140,1),transparent);opacity:.6}
.cld.snow{background:radial-gradient(closest-side,rgba(225,230,240,1),transparent);opacity:.45}
.cld.fog{top:30%;bottom:-20%;background:radial-gradient(closest-side,rgba(215,220,230,1),transparent);opacity:.55}
.drop{position:absolute;top:-12px;width:1px;height:10px;background:linear-gradient(transparent,rgba(210,225,255,.9));
  animation:fall .8s linear infinite}
@keyframes fall{to{transform:translate(-3px,74px)}}
.flake{position:absolute;top:-6px;width:3px;height:3px;border-radius:50%;background:#fff;opacity:.9;animation:snow 4s linear infinite}
@keyframes snow{50%{transform:translate(3px,34px)}to{transform:translate(-2px,70px)}}
.flash{position:absolute;inset:0;background:#fff;opacity:0;animation:flash 7s infinite}
@keyframes flash{0%,93%,97%,100%{opacity:0}94%,96%{opacity:.5}}
.sky .seg{position:absolute;bottom:5px;height:5px;border-radius:3px;background:rgba(255,255,255,.62)}
.sky .seg.event{background:var(--mark2)}
.sky .seg.past{opacity:.35}
.sky .gone{position:absolute;left:0;top:0;bottom:0;background:rgba(8,10,20,.42);transition:width 1s linear;
  border-right:1px solid rgba(255,255,255,.25)}
.sky .now{position:absolute;top:0;bottom:0;width:0;transition:left 1s linear}
.sky .now::before{content:"";position:absolute;top:0;bottom:0;left:-1px;width:2px;background:rgba(255,255,255,.55)}
.cel{position:absolute;left:-8px;width:16px;height:16px;border-radius:50%;transition:top 1s linear}
.cel.sunb{background:#FFE27A;box-shadow:0 0 12px 4px rgba(255,210,90,.75),0 0 30px 10px rgba(255,200,80,.35);animation:halo2 5s ease-in-out infinite}
.cel.moon{width:14px;height:14px;left:-7px;background:transparent;box-shadow:inset -4px 2px 0 0 #F3EFD8;filter:drop-shadow(0 0 5px rgba(243,239,216,.7))}
@keyframes halo2{50%{box-shadow:0 0 16px 6px rgba(255,210,90,.85),0 0 40px 14px rgba(255,200,80,.4)}}
.tks{position:relative;height:16px;margin-top:3px}
.tks span{position:absolute;top:0;white-space:nowrap;transform:translateX(-50%);font-size:10px;color:var(--faint);font-variant-numeric:tabular-nums}
.tks span:first-child{transform:none}.tks span:nth-child(5){transform:translateX(-100%)}
.tks .sun{top:4px;width:5px;height:5px;border-radius:50%;padding:0}
.tks .sun.sr{background:#f0a878}.tks .sun.ss{background:#f08a5c}
.band .meta{margin-top:4px}
@media (prefers-reduced-motion:reduce){.star,.drop,.flake,.flash,.cel.sunb{animation:none!important}.drop,.flake{display:none}}

/* 알림함 */
.inb{position:relative;width:36px;padding:0;display:inline-flex;align-items:center;justify-content:center}
.inb .ico{font-size:15px}
.inb.has{color:var(--mark)}
.inb .cnt{position:absolute;top:-5px;right:-5px;min-width:16px;height:16px;padding:0 4px;border-radius:8px;background:var(--hot);
  color:#fff;font-size:10px;font-weight:700;line-height:16px;text-align:center;animation:pop-in .3s cubic-bezier(.2,.9,.25,1.3)}
.ib-off{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;padding:10px 12px;border-radius:12px;
  background:var(--warnBg);border:1px solid var(--warnBd);font-size:13px;color:var(--warnFg)}
.ib-off .s-link{margin:0;height:30px;background:none;font:inherit;font-weight:600;cursor:pointer}
.inn{position:relative;margin-bottom:8px;border-radius:13px;background:var(--desk2);border:1px solid var(--line);overflow:hidden;
  animation:rise .4s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i,0)*35ms)}
.inn::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2)}
.inn.morning::before{background:var(--mark)}.inn.evening::before{background:var(--lec)}
.inn.due::before{background:var(--hot)}.inn.lecopen::before{background:var(--lec)}
.inn.new{border-color:var(--accBd)}
.inn.new .inh .t::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--hot);margin-left:7px;vertical-align:.15em}
.inh{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:11px 13px 6px 15px}
.inh .ico{font-size:15px;color:var(--muted)}
.inn.morning .inh .ico{color:var(--mark)}.inn.due .inh .ico{color:var(--hot)}.inn.evening .inh .ico,.inn.lecopen .inh .ico{color:var(--lec)}
.inh .t{flex:1;min-width:0;font-size:14px;font-weight:600;color:var(--ink);text-wrap:pretty}
.inh .w{font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums;flex:none}
.inl{display:flex;flex-direction:column;padding:0 13px 10px 40px}
.inl .ln{font-size:13px;line-height:1.5;color:var(--sub);text-align:left;padding:3px 0;text-wrap:pretty}
.inl .ln.go{text-decoration:underline;text-decoration-color:var(--line2);text-underline-offset:3px}
.inl .ln.go:active{color:var(--ink)}
.nsent{padding:0 13px 10px 40px;font-size:11.5px;color:var(--warnFg)}
/* 공지 (2026-09-26) — 알림함 카드를 그대로 쓰고, 누르면 본문이 펼쳐진다 */
.inn.notice::before{background:var(--mark)}
.inn.notice .inh .ico{color:var(--mark)}
.inn.notice .inh{padding-bottom:2px}
.nmeta{padding:0 13px 10px 40px;font-size:12px;color:var(--muted)}
.nbody{padding:0 13px 13px 40px}
.nbody[hidden]{display:none}
.nbody .nt{white-space:pre-wrap;font-size:13.5px;line-height:1.75;color:var(--sub);overflow-wrap:anywhere;text-wrap:pretty}
.nfiles{margin-top:8px;font-size:12.5px;color:var(--muted);display:flex;flex-direction:column;gap:2px}
.nall{margin:6px 0 -4px}
.nall .s-link{margin-top:6px;height:30px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
/* 연결 상태 */
.mi.dot::after{content:"";width:6px;height:6px;border-radius:50%;background:var(--hot);margin-left:8px}
.srow{display:flex;gap:11px;margin-top:10px;padding:12px 13px;border-radius:13px;background:var(--desk2);border:1px solid var(--line);
  animation:rise .4s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i,0)*35ms)}
.sdot{flex:none;width:8px;height:8px;margin-top:7px;border-radius:50%;background:var(--mark2)}
.srow.err .sdot{background:var(--hot)}
.srow.stale .sdot,.srow.never .sdot{background:var(--warnFg)}
.srow.err{border-color:var(--warnBd)}
.srow .sm{flex:1;min-width:0}
.srow .st{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.srow .sn{font-size:14px;font-weight:600;color:var(--ink)}
.srow .sw{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.srow.err .sw{color:var(--hot)}
.srow .ss{margin-top:3px;font-size:12.5px;line-height:1.5;color:var(--sub);overflow-wrap:anywhere}
.srow .s-link{margin-top:8px;height:30px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
@media (prefers-reduced-motion:reduce){.inn,.inb .cnt,.srow{animation:none}}
</style>
<script>try{var t=localStorage.getItem("erion-theme");if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
</head>"""

# erion 자기 아이콘 (2026-09-23). 없을 때 Safari 가 같은 도메인의 여백 favicon 을 끌어다 썼다.
# 파일은 app/static/ — scripts/make_icons.py 가 굽는다. 경로는 전부 /erion 아래다.
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_OK = {"icon-16.png", "icon-32.png", "icon-180.png", "icon-192.png", "icon-512.png",
              "icon-512-maskable.png", "badge-96.png"}
_ICONS = (f'<link rel=icon type=image/png sizes=32x32 href="{PREFIX}/static/icon-32.png">'
          f'<link rel=icon type=image/png sizes=16x16 href="{PREFIX}/static/icon-16.png">'
          f'<link rel=apple-touch-icon href="{PREFIX}/static/icon-180.png">'
          f'<link rel=manifest href="{PREFIX}/manifest.webmanifest">'
          '<meta name=apple-mobile-web-app-title content=erion>'
          '<meta name=theme-color content="#201F1D">')
_HEAD = _HEAD.replace("<title>", _ICONS + "<title>", 1)


def _login_page(err: str = "", status: int = 200) -> HTMLResponse:
    """PIN 화면. 여백과 같은 형식 — 숫자 6자리, 다 차면 엔터 없이 자동 제출.

    폼 POST 를 그대로 쓴다(fetch 로 안 바꾼다). `login()` 의 10분 5회 잠금과
    비밀번호 비교가 손대지 않은 채 살아 있어야 해서다.
    """
    e = f"<div class=err>{err}</div>" if err else ""
    return HTMLResponse(
        _HEAD + f"""<body><div class=amb aria-hidden=true><i></i><i></i><i></i></div><div class=login><form method=post action="{BASE}/login" id=f>
<h1>erion</h1><p>숫자 6자리 비밀번호를 넣어주세요. 6자리가 다 차면 자동으로 들어갑니다.
이 기기에서는 30일간 유지됩니다.</p>
<input class=pw6 id=pw type=password name=password inputmode=numeric maxlength=6
 autocomplete=current-password spellcheck=false autocapitalize=off autocorrect=off>
<div class=dots id=dots><i></i><i></i><i></i><i></i><i></i><i></i></div>
{e}
<div class=pad id=pad>
<button class=key type=button data-k=1>1</button><button class=key type=button data-k=2>2</button>
<button class=key type=button data-k=3>3</button><button class=key type=button data-k=4>4</button>
<button class=key type=button data-k=5>5</button><button class=key type=button data-k=6>6</button>
<button class=key type=button data-k=7>7</button><button class=key type=button data-k=8>8</button>
<button class=key type=button data-k=9>9</button>
<button class="key sm" type=button data-k=del aria-label="지우기">지움</button>
<button class=key type=button data-k=0>0</button>
<button class="key go" type=submit id=go aria-label="들어가기">확인</button>
</div>
</form></div>
<script>{_PIN_JS}</script></body></html>""",
        status_code=status)


_PIN_JS = r'''
(function(){
const pw=document.getElementById("pw"),f=document.getElementById("f"),
      go=document.getElementById("go"),dots=document.getElementById("dots").children;
let sent=false;
function paint(){
  for(let i=0;i<6;i++)dots[i].classList.toggle("on",i<pw.value.length);
  go.disabled=!pw.value}
function submit(){if(sent)return;sent=true;f.submit()}
function set(v){
  pw.value=v.replace(/\D/g,"").slice(0,6);paint();
  if(pw.value.length===6)submit()}                 // 6자리가 차면 엔터 없이 바로
pw.addEventListener("input",()=>set(pw.value));
document.getElementById("pad").addEventListener("mousedown",e=>e.preventDefault());  // 포커스 유지
document.getElementById("pad").addEventListener("click",e=>{
  const b=e.target.closest("[data-k]");if(!b)return;
  const k=b.dataset.k;
  set(k==="del"?pw.value.slice(0,-1):pw.value+k)});
f.addEventListener("submit",()=>{sent=true});
// 화면 키보드가 뜨면 패드가 가려진다. 터치 기기에서는 자동 포커스를 안 준다.
if(!matchMedia("(pointer:coarse)").matches)pw.focus();
paint();
})();
'''


def _mark_source(source: str, err: str | None = None, note: str | None = None) -> None:
    """연결 상태 한 줄 (tools.sources). 적다가 실패해도 대시보드는 그대로 간다."""
    try:
        conn = db.vault()
        try:
            tools.source_mark(conn, source, err, note)
        finally:
            conn.close()
    except Exception:                           # noqa: BLE001
        pass


def _lectures(marks: set[str]) -> tuple[list[dict], list[dict], str | None]:
    """강의영상을 블록 모양으로 맞춰 준다. 실패해도 대시보드는 살아야 한다.

    돌려주는 것: (아직 안 본 것, LearningX 가 시청 완료로 아는 것, 오류).
    2026-09-23 부터 본 것도 받는다 — 목록에서 그냥 사라지면 "한 일이 날아갔다" 로 보인다.
    marks 는 손으로 완료를 찍은 id 집합(done_mark). LearningX 의 시청 여부와 별개다 —
    강의를 딴 데서 봤거나 볼 필요가 없다고 판단한 경우가 있다. 그건 done_recent 가 이미 든다."""
    try:
        rows = learningx.brief(within_days=SPAN_DAYS, unwatched_only=False, limit=ROW_CAP)
    except Exception as e:                      # noqa: BLE001 — 어떤 실패든 대시보드보다 가볍다
        err = f"{type(e).__name__}: {e}"[:160]
        _mark_source("learningx", err)
        return [], [], err
    _mark_source("learningx", note=f"{len(rows)}편")
    out, seen = [], []
    for r in rows:
        if f"lx:{r['url']}" in marks:
            continue
        (seen if r.get("watched") else out).append({
            "id": f"lx:{r['url']}", "kind": "lecture", "label": r["title"], "title": r["title"],
            "course": r["course"], "due_at": r["close_at"], "points": None,
            "est_hours": (r["minutes"] / 60) if r.get("minutes") else None,
            "est_basis": "duration", "actual_hours": None, "prep_note": None,
            "description": None, "url": r.get("open_url"), "sub_types": None, "done": False,
            "minutes": r.get("minutes"), "module": r.get("module"),
            "open_at": r.get("open_at"), "period_status": r.get("period_status"),
        })
    for b in seen:
        b.update(done=True, done_src="watched", done_at=None)
    return out, seen, None


async def page(request: Request) -> Response:
    if not _authed(request):
        return _login_page()
    conn = db.vault()
    try:
        marks = tools.done_ids(conn)
    finally:
        conn.close()
    (lec, lec_seen, lec_err), wx = await asyncio.gather(asyncio.to_thread(_lectures, marks),
                                                         asyncio.to_thread(_weather))
    now = datetime.now(tools.KST)
    conn = db.vault()
    try:
        # 끝난 것까지 받는다 — Canvas 에 낸 과제가 그냥 사라지면 "한 일이 날아갔다" 로 보인다.
        every = tools.board(conn, within_days=SPAN_DAYS, past_days=PAST_DAYS, limit=ROW_CAP,
                            include_done=True)
        done = tools.done_recent(conn)
        events = tools.blocks(conn, (now - timedelta(days=PAST_DAYS)).isoformat(timespec="seconds"),
                              (now + timedelta(days=SPAN_DAYS)).isoformat(timespec="seconds"),
                              limit=ROW_CAP)
        live = [b for b in every if not b["done"]] + lec
        focus_st = _focus_state(conn)
        inbox = _inbox(conn)
        notices = tools.notices(conn, days=NOTICE_DAYS, limit=NOTICE_CAP, body_max=None)
        sources = tools.sources(conn)
        try:
            plan, plan_err = planner.compute(conn, live, now), None
        except Exception as e:                  # noqa: BLE001 — 계획이 틀려도 목록은 보여야 한다
            plan, plan_err = None, f"{type(e).__name__}: {e}"[:160]
    finally:
        conn.close()
    # 완료함 = 손으로 찍은 것(done_recent) + Canvas 가 제출로 아는 것 + LearningX 시청 완료.
    for d in done:
        d["done_src"] = "manual"
    seen = {d["id"] for d in done}
    for b in every:
        if b["done"] and b["id"] not in seen:
            done.append(dict(b, done_src="canvas" if b["kind"] == "assignment" and not b["done_manual"]
                             else "manual", done_at=None))
    done += lec_seen
    data = {
        "now": now.isoformat(timespec="seconds"),
        "blocks": live,
        "done": done,
        "events": events,
        "plan": plan,
        "plan_err": plan_err,
        "lec_err": lec_err,
        "doneUrl": f"{BASE}/done",
        "planUrl": f"{BASE}/plan",
        "estUrl": f"{BASE}/est",
        "focusUrl": f"{BASE}/focus",
        "focus": focus_st,
        "weather": wx,
        "inbox": inbox,
        "notices": notices,
        "noticeUrl": f"{BASE}/notice",
        "sources": sources,
        "gcalUrl": f"{PREFIX}/gcal/start",
        "pushUrl": f"{BASE}/push",
        "swUrl": f"{PREFIX}/sw.js",
        "vapid": push.public_key(),
    }
    return HTMLResponse(_HEAD + f"""<body><div class=amb aria-hidden=true><i></i><i></i><i></i></div><div class=wrap>
<div class=bar><div class=brand>erion<b></b></div>
<div class=tools><button class="ib inb" id=ntc aria-label="공지"></button><button class="ib inb" id=inb aria-label="알림함"></button><button class="ib gear" id=gear aria-label="설정" aria-expanded=false>⚙</button>
<div class=menu id=menu hidden><button class=mi id=bell hidden>🔕 알림 켜기</button><button class=mi id=theme>◐ 밝게 · 어둡게</button><button class=mi id=conn>◎ 연결 상태</button>
<a class=mi href="{BASE}/logout">나가기</a></div></div></div>
<div class=greet><div class=stamp id=stamp></div><div class=headline id=headline></div>
<div class=subline id=subline></div><div class=band id=band></div></div>
<div class=tabs id=tabs></div>
<div class=main>
<div id=view></div>
<aside class=sheet id=sheet role=dialog aria-modal=true></aside>
</div>
</div>
<div class=scrim id=scrim></div>
<div class=dock id=dock hidden></div>
<div class=fz id=focus hidden role=dialog aria-modal=true></div>
<script>const DATA={_json_for_script(data)};</script>
<script>{_JS}</script></body></html>""")


async def root(request: Request) -> Response:
    """`/erion/` — 로그인돼 있으면 대시보드, 아니면 **기존 공개 홈 그대로.**

    `/erion/` 은 구글 OAuth 심사에 등록된 공개 홈페이지 주소다 (개인정보처리방침 본문에도
    `운영 주소 …/erion/` 으로 박혀 있다). 비로그인 방문자에게 다른 걸 보여주면 심사가 깨진다.
    그래서 여기서 갈래를 친다 — 비밀번호 폼조차 안 띄운다. 로그인은 `/erion/web` 이다.
    """
    if _authed(request):
        return await page(request)
    from . import pages
    return await pages.home(request)


async def login(request: Request) -> Response:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < FAIL_WINDOW]
    if len(_fails) >= FAIL_MAX:
        return _login_page("실패가 너무 많아요. 10분 뒤에 다시 해주세요.", 429)
    form = await request.form()
    pw = str(form.get("password", ""))
    if not (config.AS_PASSWORD and hmac.compare_digest(pw, config.AS_PASSWORD)):
        _fails.append(now)
        return _login_page("비밀번호가 틀렸어요.", 401)
    _fails.clear()
    exp = int(now) + COOKIE_DAYS * 86400
    resp = RedirectResponse(f"{PREFIX}/", status_code=303)
    resp.set_cookie(COOKIE, _sign(exp), max_age=COOKIE_DAYS * 86400, path=COOKIE_PATH,
                    httponly=True, secure=True, samesite="lax")
    return resp


async def done(request: Request) -> Response:
    """완료 찍기/되돌리기. 시트의 버튼이 부른다. JSON {id, done} → {ok, id, done}.

    CSRF: 쿠키가 SameSite=lax 라 남의 사이트에서 띄운 POST 에는 안 실린다.
    그래도 fetch 가 붙이는 X-Erion 헤더를 한 번 더 본다 — 폼 전송으로는 못 붙이는 헤더다.
    """
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    if request.headers.get("x-erion") != "web":
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
    try:
        body = await request.json()
    except Exception:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
    bid = str(body.get("id") or "")
    if not bid:
        return JSONResponse({"ok": False, "error": "id 가 없어요"}, status_code=400)
    conn = db.vault()
    try:
        # 강의영상은 DB 에 행이 없다 — 화면이 들고 있는 이름·과목·마감을 같이 넘긴다.
        r = tools.mark_done(conn, bid, bool(body.get("done", True)),
                            label=body.get("label"), course=body.get("course"),
                            due_at=body.get("due_at"))
    finally:
        conn.close()
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


async def notice_read(request: Request) -> Response:
    """공지를 펼쳤다 → 읽음. JSON {id} 또는 {all: true}. Canvas 엔 안 알린다(읽기 전용 토큰으로 쓴다)."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    if request.headers.get("x-erion") != "web":
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
    try:
        body = await request.json()
    except Exception:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
    ids = None if body.get("all") else [str(body.get("id") or "")]
    if ids == [""]:
        return JSONResponse({"ok": False, "error": "id 가 없어요"}, status_code=400)
    conn = db.vault()
    try:
        n = tools.notice_read(conn, ids)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "n": n})


async def plan_move(request: Request) -> Response:
    """이번 주 탭에서 끌어 옮기기. JSON {id, from, to, hours, whole} → planner.move.
    to=null 이면 박음을 전부 풀어 자동 배치로 되돌린다. 인증·CSRF 는 done 과 같다."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    if request.headers.get("x-erion") != "web":
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
    try:
        body = await request.json()
        hours = float(body["hours"]) if body.get("hours") is not None else None
    except Exception:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
    bid = str(body.get("id") or "")
    if not bid:
        return JSONResponse({"ok": False, "error": "id 가 없어요"}, status_code=400)
    conn = db.vault()
    try:
        r = planner.move(conn, bid, body.get("from"), body.get("to"), hours,
                         bool(body.get("whole", True)))
    finally:
        conn.close()
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


async def est(request: Request) -> Response:
    """시트에서 예상시간 고치기. JSON {id, hours} → tools.est_adjust — 챗에서 고친 것과 같은 길이라
    est_feedback 에 한 줄 쌓이고 다음 추정이 참고한다. 강의영상은 길이가 곧 예상이라 안 받는다."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    if request.headers.get("x-erion") != "web":
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
    try:
        body = await request.json()
        hours = float(body["hours"])
    except Exception:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
    bid = str(body.get("id") or "")
    if not bid or bid.startswith("lx:"):
        return JSONResponse({"ok": False, "error": "고칠 수 없는 항목이에요"}, status_code=400)
    conn = db.vault()
    try:
        r = tools.est_adjust(conn, bid, hours, "대시보드에서 고침")
    finally:
        conn.close()
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


# 날씨 (2026-09-26) — 하루 띠가 하늘이 된다: 해 뜨고 지는 시각으로 밤/낮 색을 칠하고, 비·눈이 오는 시간대엔
# 그 구간에 비·눈이 내린다. Open-Meteo(키 없음). 보내는 건 고정 좌표뿐이다(기본 = 성대 자연과학캠퍼스).
# 20분 캐시, 3초 안에 못 받으면 날씨 없이 그린다 — 대시보드가 날씨 때문에 느려지거나 죽으면 안 된다.
WEATHER_LAT = float(os.environ.get("ERION_WEATHER_LAT", "37.2939"))
WEATHER_LON = float(os.environ.get("ERION_WEATHER_LON", "126.9745"))
_wx: tuple[float, dict | None] = (0.0, None)


def _weather() -> dict | None:
    global _wx
    if time.time() - _wx[0] < 1200:
        return _wx[1]
    import urllib.request
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
           "&current=temperature_2m,weather_code,cloud_cover"
           "&hourly=weather_code,cloud_cover,precipitation_probability"
           "&daily=sunrise,sunset&timezone=Asia%2FSeoul&forecast_days=1")
    try:
        with urllib.request.urlopen(url, timeout=3) as res:
            j = json.loads(res.read())
        hr = lambda s: int(s[11:13]) + int(s[14:16]) / 60      # noqa: E731 — "…T06:22" → 6.37
        out = {"temp": j["current"]["temperature_2m"], "code": j["current"]["weather_code"],
               "cloud": j["current"]["cloud_cover"],
               "sunrise": hr(j["daily"]["sunrise"][0]), "sunset": hr(j["daily"]["sunset"][0]),
               "hourly": [{"code": c, "cloud": cl, "pop": p} for c, cl, p in zip(
                   j["hourly"]["weather_code"], j["hourly"]["cloud_cover"],
                   j["hourly"]["precipitation_probability"])][:24]}
    except Exception:                           # noqa: BLE001
        out = None
    _wx = (time.time(), out)
    return out


def _inbox(conn, limit: int = 40) -> list[dict]:
    """알림함 (2026-09-26) — 보낸 푸시(push_log). 한 알림이 여러 key 를 덮으니(마감 알림은 과제마다 한 줄)
    (시각·제목·본문)으로 묶는다. 보낼 게 없어서 제목이 빈 아침·저녁 확인 줄은 뺀다.
    받은 기기가 0 이어도(구독이 끊겼을 때) 보여준다 — 그래서 놓친 걸 여기서 본다."""
    out, seen = [], set()
    for r in conn.execute("SELECT key,at,title,body,sent,url FROM push_log WHERE title IS NOT NULL "
                          "ORDER BY at DESC LIMIT 400"):
        k = (r["at"], r["title"], r["body"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"at": r["at"], "title": r["title"], "body": r["body"], "sent": r["sent"],
                    "kind": r["key"].split(":")[0], "url": r["url"]})
        if len(out) >= limit:
            break
    return out


FOCUS_CAP_MS = 4 * 3600 * 1000     # 켜두고 잊은 타이머가 기록을 부풀리지 않게, 한 번에 최대 4시간만 센다


def _focus_state(conn) -> dict:
    """화면에 주는 집중 상태 — 지금 켜진 것 + 블록별 합계 + 오늘 합계. 시각은 서버 epoch ms."""
    r = conn.execute("SELECT * FROM focus_now WHERE k=1").fetchone()
    cur = None
    if r:
        cur = {"id": r["id"], "label": r["label"], "est": r["est"], "start": r["start_ms"],
               "acc": r["acc_ms"], "began": r["began_ms"]}
    spent = {x["id"]: x["s"] for x in conn.execute("SELECT id, SUM(ms) s FROM focus_log GROUP BY id")}
    today = datetime.now(tools.KST).strftime("%Y-%m-%d")
    t = conn.execute("SELECT COALESCE(SUM(ms),0) FROM focus_log WHERE day=?", [today]).fetchone()[0]
    return {"cur": cur, "spent": spent, "today": t, "now": int(time.time() * 1000)}


def _focus_ms(r, now_ms: int) -> int:
    run = (now_ms - r["start_ms"]) if r["start_ms"] is not None else 0
    return max(0, min(FOCUS_CAP_MS, r["acc_ms"] + run))


async def focus(request: Request) -> Response:
    """집중 타이머. GET → 상태. POST {op, id?, label?, est?}:
    start(다른 게 켜져 있으면 그건 기록하고 끝낸다) · pause · resume · stop(기록하고 끝) · cancel(기록 없이 끝).
    서버 시각이 기준이라 폰이 앱을 재우거나 새로고침해도 시간이 안 날아간다."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    conn = db.vault()
    try:
        if request.method == "GET":
            return JSONResponse({"ok": True, **_focus_state(conn)})
        if request.headers.get("x-erion") != "web":
            return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
        op = body.get("op")
        now_ms = int(time.time() * 1000)
        r = conn.execute("SELECT * FROM focus_now WHERE k=1").fetchone()

        def close(keep: bool) -> None:
            if not r:
                return
            ms = _focus_ms(r, now_ms)
            if keep and ms >= 1000:
                day = datetime.now(tools.KST).strftime("%Y-%m-%d")
                conn.execute("INSERT INTO focus_log(id,day,ms) VALUES(?,?,?) "
                             "ON CONFLICT(id,day) DO UPDATE SET ms=ms+excluded.ms", [r["id"], day, ms])
            conn.execute("DELETE FROM focus_now WHERE k=1")

        if op == "start":
            bid = str(body.get("id") or "")
            if not bid:
                return JSONResponse({"ok": False, "error": "id 가 없어요"}, status_code=400)
            if r and r["id"] == bid:            # 이미 켜져 있으면 멈춤만 푼다
                if r["start_ms"] is None:
                    conn.execute("UPDATE focus_now SET start_ms=? WHERE k=1", [now_ms])
            else:
                close(True)
                est = body.get("est")
                conn.execute("INSERT INTO focus_now(k,id,label,est,start_ms,acc_ms,began_ms) "
                             "VALUES(1,?,?,?,?,0,?)",
                             [bid, str(body.get("label") or "")[:200],
                              float(est) if isinstance(est, (int, float)) else None, now_ms, now_ms])
        elif op in ("pause", "resume"):
            if not r:
                return JSONResponse({"ok": False, "error": "켜진 게 없어요"}, status_code=409)
            if op == "pause" and r["start_ms"] is not None:
                conn.execute("UPDATE focus_now SET acc_ms=?, start_ms=NULL WHERE k=1",
                             [_focus_ms(r, now_ms)])
            elif op == "resume" and r["start_ms"] is None:
                conn.execute("UPDATE focus_now SET start_ms=? WHERE k=1", [now_ms])
        elif op in ("stop", "cancel"):
            close(op == "stop")
        else:
            return JSONResponse({"ok": False, "error": "모르는 op"}, status_code=400)
        conn.commit()
        return JSONResponse({"ok": True, **_focus_state(conn)})
    finally:
        conn.close()


async def push_sub(request: Request) -> Response:
    """알림 켜기. JSON {sub: PushSubscription.toJSON(), test: bool}. test 면 바로 한 통 보낸다."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "로그인이 풀렸어요"}, status_code=401)
    if request.headers.get("x-erion") != "web":
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=403)
    try:
        body = await request.json()
    except Exception:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": "JSON 이 아니에요"}, status_code=400)
    conn = db.vault()
    try:
        r = push.subscribe(conn, body.get("sub") or {}, request.headers.get("user-agent"))
        if r["ok"] and body.get("test"):
            st = await asyncio.to_thread(
                push.send_one, dict(endpoint=body["sub"]["endpoint"], **body["sub"]["keys"]),
                {"title": "알림 켜짐",
                 "body": "오늘 할 것과 마감 임박을 여기로 알려 드릴게요.",
                 "url": f"{PREFIX}/", "tag": "test"})
            r["test"] = st
    finally:
        conn.close()
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


_SW = """// erion 서비스 워커 — 푸시를 받아 띄우고, 누르면 대시보드를 연다. fetch 는 안 가로챈다.
self.addEventListener("install",()=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
// 푸시를 받고 알림을 못 띄우면 크로미움 계열(특히 삼성 인터넷)은 구독을 바로 끊는다(→ 서버엔 410).
// 그래서 옵션을 빼 가며 세 번까지 띄워 보고, 어떻게 됐는지를 서버에 한 줄 알린다(/web/push/ack).
function ack(o){return fetch("%(p)s/web/push/ack",{method:"POST",credentials:"same-origin",
  headers:{"content-type":"application/json","x-erion":"sw"},
  body:JSON.stringify(Object.assign({perm:self.Notification?Notification.permission:"?"},o))}).catch(()=>{})}
self.addEventListener("push",e=>{
  let d={};try{d=e.data.json()}catch(_){d={title:"erion",body:e.data?e.data.text():""}}
  const t=d.title||"erion",data={url:d.url||"%(p)s/"};
  const tries=[
    // icon 은 안 준다 — 안드로이드는 왼쪽에 이미 앱 아이콘을 두고 icon 을 오른쪽에 또 그려서 e. 가 두 번 보였다.
    // badge 는 상태바의 흰 실루엣. iOS 는 둘 다 무시한다.
    {body:d.body||"",tag:d.tag||undefined,badge:"%(p)s/static/badge-96.png",data:data},
    {body:d.body||"",data:data},
    {}];
  const errs=[];
  const go=i=>i>=tries.length?Promise.reject(new Error(errs.join(" | "))):
    self.registration.showNotification(t,tries[i]).then(()=>i,x=>{errs.push(String(x&&x.message||x));return go(i+1)});
  e.waitUntil(go(0).then(i=>ack({ok:true,step:i,tag:d.tag||null,errs:errs}),
                         x=>ack({ok:false,tag:d.tag||null,err:String(x&&x.message||x)})))});
self.addEventListener("notificationclick",e=>{
  e.notification.close();
  const url=(e.notification.data&&e.notification.data.url)||"%(p)s/";
  e.waitUntil(clients.matchAll({type:"window",includeUncontrolled:true}).then(cs=>{
    for(const c of cs){if(c.url.startsWith(self.registration.scope)&&"focus" in c)
      return c.focus().then(w=>w.navigate?w.navigate(url):w)}
    return clients.openWindow(url)}))});
"""


async def push_ack(request: Request) -> Response:
    """서비스 워커가 "푸시 받음 / 알림 띄움·실패" 를 알려 온다. 진단용 — journal 에 한 줄 적을 뿐이다.
    쿠키가 안 실려 오는 기기도 있어서 로그인을 요구하지 않는다. 대신 아무것도 저장하지 않고 짧게 자른다."""
    if request.headers.get("x-erion") != "sw":
        return Response(status_code=403)
    try:
        body = (await request.body())[:600].decode("utf-8", "replace")
    except Exception:                           # noqa: BLE001
        body = "?"
    ua = (request.headers.get("user-agent") or "")[-60:]
    print(f"push-ack {body} ua={ua}", flush=True)
    return Response(status_code=204)


async def sw(request: Request) -> Response:
    """서비스 워커. 로그인 없이 준다(브라우저가 쿠키 없이 갱신 확인을 한다). 비밀은 없다.
    `/erion/sw.js` 에 있으니 범위가 `/erion/` 로 갇힌다 — 여백과 안 섞인다."""
    return Response(_SW % {"p": PREFIX}, media_type="application/javascript",
                    headers={"cache-control": "no-cache"})


async def logout(request: Request) -> Response:
    resp = RedirectResponse(f"{PREFIX}/", status_code=303)
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    resp.delete_cookie(COOKIE, path=COOKIE_PATH_OLD)     # 2026-09-20 이전 판의 쿠키
    return resp


_JS = r"""
(function(){
const $=s=>document.querySelector(s);
// 지금은 흐른다. 서버 시각을 기준으로 잡고(기기 시계가 틀려도) 매초 앞으로 민다 — 아래 '살아 있는 층'.
const SKEW=new Date(DATA.now).getTime()-Date.now(), T0=new Date(DATA.now);
let NOW=new Date(DATA.now);
const DOW="일월화수목금토";
const KIND={assignment:"과제",task:"할 일",video:"영상",lecture:"강의영상"};
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
// 아이콘은 Bootstrap Icons 1.11.3(MIT)에서 쓰는 것만 옮겨 왔다. 글자 기호(▶ ❚❚ ✕ ⚙)는 기기마다 모양이 달랐다.
const ICO={"megaphone":"<path d=\"M13 2.5a1.5 1.5 0 0 1 3 0v11a1.5 1.5 0 0 1-3 0v-.214c-2.162-1.241-4.49-1.843-6.912-2.083l.405 2.712A1 1 0 0 1 5.51 15.1h-.548a1 1 0 0 1-.916-.599l-1.85-3.49-.202-.003A2.014 2.014 0 0 1 0 9V7a2.02 2.02 0 0 1 1.992-2.013 75 75 0 0 0 2.483-.075c3.043-.154 6.148-.849 8.525-2.199zm1 0v11a.5.5 0 0 0 1 0v-11a.5.5 0 0 0-1 0m-1 1.35c-2.344 1.205-5.209 1.842-8 2.033v4.233q.27.015.537.036c2.568.189 5.093.744 7.463 1.993zm-9 6.215v-4.13a95 95 0 0 1-1.992.052A1.02 1.02 0 0 0 1 7v2c0 .55.448 1.002 1.006 1.009A61 61 0 0 1 4 10.065m-.657.975 1.609 3.037.01.024h.548l-.002-.014-.443-2.966a68 68 0 0 0-1.722-.082z\"/>","megaphone-fill":"<path d=\"M13 2.5a1.5 1.5 0 0 1 3 0v11a1.5 1.5 0 0 1-3 0zm-1 .724c-2.067.95-4.539 1.481-7 1.656v6.237a25 25 0 0 1 1.088.085c2.053.204 4.038.668 5.912 1.56zm-8 7.841V4.934c-.68.027-1.399.043-2.008.053A2.02 2.02 0 0 0 0 7v2c0 1.106.896 1.996 1.994 2.009l.496.008a64 64 0 0 1 1.51.048m1.39 1.081q.428.032.85.078l.253 1.69a1 1 0 0 1-.983 1.187h-.548a1 1 0 0 1-.916-.599l-1.314-2.48a66 66 0 0 1 1.692.064q.491.026.966.06\"/>","plus-lg":"<path fill-rule=\"evenodd\" d=\"M8 2a.5.5 0 0 1 .5.5v5h5a.5.5 0 0 1 0 1h-5v5a.5.5 0 0 1-1 0v-5h-5a.5.5 0 0 1 0-1h5v-5A.5.5 0 0 1 8 2\"/>","check-lg":"<path d=\"M12.736 3.97a.733.733 0 0 1 1.047 0c.286.289.29.756.01 1.05L7.88 12.01a.733.733 0 0 1-1.065.02L3.217 8.384a.757.757 0 0 1 0-1.06.733.733 0 0 1 1.047 0l3.052 3.093 5.4-6.425z\"/>","x-lg":"<path d=\"M2.146 2.854a.5.5 0 1 1 .708-.708L8 7.293l5.146-5.147a.5.5 0 0 1 .708.708L8.707 8l5.147 5.146a.5.5 0 0 1-.708.708L8 8.707l-5.146 5.147a.5.5 0 0 1-.708-.708L7.293 8z\"/>","gear":"<path d=\"M8 4.754a3.246 3.246 0 1 0 0 6.492 3.246 3.246 0 0 0 0-6.492M5.754 8a2.246 2.246 0 1 1 4.492 0 2.246 2.246 0 0 1-4.492 0\"/> <path d=\"M9.796 1.343c-.527-1.79-3.065-1.79-3.592 0l-.094.319a.873.873 0 0 1-1.255.52l-.292-.16c-1.64-.892-3.433.902-2.54 2.541l.159.292a.873.873 0 0 1-.52 1.255l-.319.094c-1.79.527-1.79 3.065 0 3.592l.319.094a.873.873 0 0 1 .52 1.255l-.16.292c-.892 1.64.901 3.434 2.541 2.54l.292-.159a.873.873 0 0 1 1.255.52l.094.319c.527 1.79 3.065 1.79 3.592 0l.094-.319a.873.873 0 0 1 1.255-.52l.292.16c1.64.893 3.434-.902 2.54-2.541l-.159-.292a.873.873 0 0 1 .52-1.255l.319-.094c1.79-.527 1.79-3.065 0-3.592l-.319-.094a.873.873 0 0 1-.52-1.255l.16-.292c.893-1.64-.902-3.433-2.541-2.54l-.292.159a.873.873 0 0 1-1.255-.52zm-2.633.283c.246-.835 1.428-.835 1.674 0l.094.319a1.873 1.873 0 0 0 2.693 1.115l.291-.16c.764-.415 1.6.42 1.184 1.185l-.159.292a1.873 1.873 0 0 0 1.116 2.692l.318.094c.835.246.835 1.428 0 1.674l-.319.094a1.873 1.873 0 0 0-1.115 2.693l.16.291c.415.764-.42 1.6-1.185 1.184l-.291-.159a1.873 1.873 0 0 0-2.693 1.116l-.094.318c-.246.835-1.428.835-1.674 0l-.094-.319a1.873 1.873 0 0 0-2.692-1.115l-.292.16c-.764.415-1.6-.42-1.184-1.185l.159-.291A1.873 1.873 0 0 0 1.945 8.93l-.319-.094c-.835-.246-.835-1.428 0-1.674l.319-.094A1.873 1.873 0 0 0 3.06 4.377l-.16-.292c-.415-.764.42-1.6 1.185-1.184l.292.159a1.873 1.873 0 0 0 2.692-1.115z\"/>","moon-stars":"<path d=\"M6 .278a.77.77 0 0 1 .08.858 7.2 7.2 0 0 0-.878 3.46c0 4.021 3.278 7.277 7.318 7.277q.792-.001 1.533-.16a.79.79 0 0 1 .81.316.73.73 0 0 1-.031.893A8.35 8.35 0 0 1 8.344 16C3.734 16 0 12.286 0 7.71 0 4.266 2.114 1.312 5.124.06A.75.75 0 0 1 6 .278M4.858 1.311A7.27 7.27 0 0 0 1.025 7.71c0 4.02 3.279 7.276 7.319 7.276a7.32 7.32 0 0 0 5.205-2.162q-.506.063-1.029.063c-4.61 0-8.343-3.714-8.343-8.29 0-1.167.242-2.278.681-3.286\"/> <path d=\"M10.794 3.148a.217.217 0 0 1 .412 0l.387 1.162c.173.518.579.924 1.097 1.097l1.162.387a.217.217 0 0 1 0 .412l-1.162.387a1.73 1.73 0 0 0-1.097 1.097l-.387 1.162a.217.217 0 0 1-.412 0l-.387-1.162A1.73 1.73 0 0 0 9.31 6.593l-1.162-.387a.217.217 0 0 1 0-.412l1.162-.387a1.73 1.73 0 0 0 1.097-1.097zM13.863.099a.145.145 0 0 1 .274 0l.258.774c.115.346.386.617.732.732l.774.258a.145.145 0 0 1 0 .274l-.774.258a1.16 1.16 0 0 0-.732.732l-.258.774a.145.145 0 0 1-.274 0l-.258-.774a1.16 1.16 0 0 0-.732-.732l-.774-.258a.145.145 0 0 1 0-.274l.774-.258c.346-.115.617-.386.732-.732z\"/>","play-circle":"<path d=\"M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14m0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16\"/> <path d=\"M6.271 5.055a.5.5 0 0 1 .52.038l3.5 2.5a.5.5 0 0 1 0 .814l-3.5 2.5A.5.5 0 0 1 6 10.5v-5a.5.5 0 0 1 .271-.445\"/>","sun":"<path d=\"M8 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6m0 1a4 4 0 1 0 0-8 4 4 0 0 0 0 8M8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0m0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13m8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2a.5.5 0 0 1 .5.5M3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8m10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0m-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0m9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707M4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708\"/>","alarm":"<path d=\"M8.5 5.5a.5.5 0 0 0-1 0v3.362l-1.429 2.38a.5.5 0 1 0 .858.515l1.5-2.5A.5.5 0 0 0 8.5 9z\"/> <path d=\"M6.5 0a.5.5 0 0 0 0 1H7v1.07a7.001 7.001 0 0 0-3.273 12.474l-.602.602a.5.5 0 0 0 .707.708l.746-.746A6.97 6.97 0 0 0 8 16a6.97 6.97 0 0 0 3.422-.892l.746.746a.5.5 0 0 0 .707-.708l-.601-.602A7.001 7.001 0 0 0 9 2.07V1h.5a.5.5 0 0 0 0-1zm1.038 3.018a6 6 0 0 1 .924 0 6 6 0 1 1-.924 0M0 3.5c0 .753.333 1.429.86 1.887A8.04 8.04 0 0 1 4.387 1.86 2.5 2.5 0 0 0 0 3.5M13.5 1c-.753 0-1.429.333-1.887.86a8.04 8.04 0 0 1 3.527 3.527A2.5 2.5 0 0 0 13.5 1\"/>","bell-fill":"<path d=\"M8 16a2 2 0 0 0 2-2H6a2 2 0 0 0 2 2m.995-14.901a1 1 0 1 0-1.99 0A5 5 0 0 0 3 6c0 1.098-.5 6-2 7h14c-1.5-1-2-5.902-2-7 0-2.42-1.72-4.44-4.005-4.901\"/>","pause-fill":"<path d=\"M5.5 3.5A1.5 1.5 0 0 1 7 5v6a1.5 1.5 0 0 1-3 0V5a1.5 1.5 0 0 1 1.5-1.5m5 0A1.5 1.5 0 0 1 12 5v6a1.5 1.5 0 0 1-3 0V5a1.5 1.5 0 0 1 1.5-1.5\"/>","arrow-up-right":"<path fill-rule=\"evenodd\" d=\"M14 2.5a.5.5 0 0 0-.5-.5h-6a.5.5 0 0 0 0 1h4.793L2.146 13.146a.5.5 0 0 0 .708.708L13 3.707V8.5a.5.5 0 0 0 1 0z\"/>","bell":"<path d=\"M8 16a2 2 0 0 0 2-2H6a2 2 0 0 0 2 2M8 1.918l-.797.161A4 4 0 0 0 4 6c0 .628-.134 2.197-.459 3.742-.16.767-.376 1.566-.663 2.258h10.244c-.287-.692-.502-1.49-.663-2.258C12.134 8.197 12 6.628 12 6a4 4 0 0 0-3.203-3.92zM14.22 12c.223.447.481.801.78 1H1c.299-.199.557-.553.78-1C2.68 10.2 3 6.88 3 6c0-2.42 1.72-4.44 4.005-4.901a1 1 0 1 1 1.99 0A5 5 0 0 1 13 6c0 .88.32 4.2 1.22 6\"/>","dash-lg":"<path fill-rule=\"evenodd\" d=\"M2 8a.5.5 0 0 1 .5-.5h11a.5.5 0 0 1 0 1h-11A.5.5 0 0 1 2 8\"/>","chevron-down":"<path fill-rule=\"evenodd\" d=\"M1.646 4.646a.5.5 0 0 1 .708 0L8 10.293l5.646-5.647a.5.5 0 0 1 .708.708l-6 6a.5.5 0 0 1-.708 0l-6-6a.5.5 0 0 1 0-.708\"/>","calendar3":"<path d=\"M14 0H2a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V2a2 2 0 0 0-2-2M1 3.857C1 3.384 1.448 3 2 3h12c.552 0 1 .384 1 .857v10.286c0 .473-.448.857-1 .857H2c-.552 0-1-.384-1-.857z\"/> <path d=\"M6.5 7a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m-9 3a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m-9 3a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2m3 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2\"/>","play-fill":"<path d=\"m11.596 8.697-6.363 3.692c-.54.313-1.233-.066-1.233-.697V4.308c0-.63.692-1.01 1.233-.696l6.363 3.692a.802.802 0 0 1 0 1.393\"/>"};
function ic(n,cls){const i=el("span","ico"+(cls?" "+cls:""));i.setAttribute("aria-hidden","true");
  i.innerHTML='<svg viewBox="0 0 16 16" fill="currentColor">'+ICO[n]+'</svg>';return i}
function withIc(b,n,txt){b.textContent="";b.append(ic(n));if(txt)b.append(el("span",null,txt));return b}

// 날짜 계산은 기기 시간대와 무관하게 KST 로 한다. 9시간 밀어 놓고 getUTC* 로 읽는 방식.
// 기기 시간대를 따르면 UTC 기기에서 23:59 마감이 14:59 로 보인다 (2026-09-18 헤드리스 렌더에서 실측).
const K=d=>new Date(new Date(d).getTime()+9*36e5);
function day0(d){const x=K(d);return Date.UTC(x.getUTCFullYear(),x.getUTCMonth(),x.getUTCDate())}
function ddays(d){return Math.round((day0(d)-day0(NOW))/864e5)}
function hm(d){const x=K(d);return String(x.getUTCHours()).padStart(2,"0")+":"+String(x.getUTCMinutes()).padStart(2,"0")}
function dow(d){return DOW[K(d).getUTCDay()]}
function hrs(h){if(h==null)return null;return(h<1?Math.round(h*60)+"분":(+h.toFixed(1))+"시간")}
function dueLong(iso){if(!iso)return"없음";const k=K(iso);
  return(k.getUTCMonth()+1)+"월 "+k.getUTCDate()+"일 ("+dow(iso)+") "+hm(iso)}
// 행 왼쪽 시간칸 — 굵은 줄과 얇은 줄 두 개로 나눈다.
// sft = 목표일(내가 정한 날). 아랫줄에 "목표" 를 붙여 진짜 마감과 갈라 읽히게 한다.
function when(iso,sft){const w=when0(iso);if(sft)w[1]="목표"+(w[1]?" · "+w[1]:"");return w}
function when0(iso){
  if(!iso)return["—",""];
  const d=new Date(iso),n=ddays(d),t=hm(d)==="23:59"||hm(d)==="23:59:59"?"자정":hm(d);
  if(d<NOW)return[n===0?"오늘":(-n)+"일 전",t];
  if(n===0)return["오늘",t];
  if(n===1)return["내일",t];
  return["D-"+n,dow(d)+" "+t]}

// 완료를 찍으면 목록이 바뀐다 — ALL 은 한 번 만들고 마는 값이 아니라 다시 계산되는 값이다.
// BLOCKS = 살아 있는 블록, DID = 완료로 찍힌 것(되돌릴 수 있게 들고 있는다). 둘은 겹치지 않는다.
let BLOCKS=DATA.blocks.slice();
let DID=(DATA.done||[]).slice();
// 최근 것이 위. 손으로 찍은 건 찍은 시각, Canvas 제출·시청은 시각을 모르니 마감으로 줄 세운다.
const didKey=b=>b.done_at||b.due_at||"";
function sortDid(){DID.sort((a,b)=>didKey(a)<didKey(b)?1:-1)}
sortDid();
const SRC={canvas:"Canvas 제출",watched:"시청함",manual:null};
let ALL=BLOCKS.filter(b=>!b.done);
const isOver=b=>b.due_at&&new Date(b.due_at)<NOW;
// 날짜 두 칸 — due_at 은 진짜 마감, target_at 은 내가 정한 날. 줄은 둘 중 있는 걸로 세우되
// "지난 마감"·빨강·머리말 숫자는 진짜 마감만 센다.
const at=b=>b.due_at||b.target_at||null;
const soft=b=>!b.due_at&&!!b.target_at;
const softLate=b=>soft(b)&&new Date(b.target_at)<NOW;
const mine=b=>b.kind==="task"||b.kind==="video";
const SRCNAME=b=>b.kind==="assignment"?"Canvas":b.kind==="lecture"?"LearningX 출석":"내가 추가";

// ---------------------------------------------------------------- 완료 찍기
// 서버(POST /web/done)가 참이고 화면은 그 뒤를 따른다. 실패하면 아무것도 안 바꾸고 시트에 적는다.
function postDone(b,done){
  return fetch(DATA.doneUrl,{method:"POST",credentials:"same-origin",
    headers:{"content-type":"application/json","x-erion":"web"},
    body:JSON.stringify({id:b.id,done:done,label:b.label,course:b.course,due_at:b.due_at})})
    .then(r=>r.json().catch(()=>({ok:false,error:"HTTP "+r.status})))}
function applyDone(b,done){
  b.done=done;b.done_manual=done;
  const di=DID.indexOf(b),bi=BLOCKS.indexOf(b);
  if(done){if(di<0){b.done_at=new Date().toISOString();b.done_src="manual";DID.unshift(b)}}
  else{if(di>=0)DID.splice(di,1);if(bi<0)BLOCKS.push(b)}
  ALL=BLOCKS.filter(x=>!x.done);
  head();renderTabs();render()}

// 우선순위 = 임박도 × 무게. 임박도는 하루 단위로 급하게 떨어지고, 무게는 배점과 예상시간.
// 근거를 UI 에 같이 적는다 — 점수만 보여주면 왜 위에 있는지 알 수 없다.
function score(b){
  const n=at(b)?ddays(at(b)):999;
  let urg;
  if(!at(b))urg=.15;
  else if(isOver(b))urg=1;
  else if(n<=0)urg=.95;
  else urg=Math.max(.08,1/(1+n*.45));
  if(soft(b))urg*=.6;             // 내가 정한 날은 어겨도 제출이 막히지 않는다
  const pts=Math.min(1,(b.points||0)/100), eff=Math.min(1,(b.est_hours||0)/4);
  return urg*(.55+.9*pts+.5*eff)}
function why(b){
  const bits=[];
  if(isOver(b))bits.push("마감 지남");
  else if(at(b)){const n=ddays(at(b));bits.push((soft(b)?"목표 ":"")+(n<0?(-n)+"일 지남":n===0?"오늘":n===1?"내일":"D-"+n))}
  if(b.points)bits.push((+b.points)+"점");
  const h=hrs(b.est_hours);if(h)bits.push(h);
  return bits.join(" · ")}

// ---------------------------------------------------------------- 행
function chips(b){
  const t=el("div","tail");
  // 채운 "출석" 배지는 "출석 됨" 으로 읽혔다(2026-09-25 사용자). 여기 뜨는 강의는 전부 아직 안 본 것이라 그 상태를 그대로 쓴다.
  if(b.kind==="lecture"){t.append(el("span","chip lec","미시청"));if(b.minutes)t.append(el("span","chip lec",b.minutes+"분"))}
  else{
    const h=hrs(b.est_hours);
    if(h){const c=el("span","chip est","≈ "+h);if(b.est_basis==="title"){c.classList.add("guess");c.title="설명 없이 제목으로 추정"}t.append(c)}
  }
  if(b.points)t.append(el("span","chip pts",(+b.points)+"점"));
  if(mine(b))t.append(el("span","chip mine","직접"));
  if(isLive(b.id))t.prepend(el("span","chip live","하는 중"));
  return t}
function row(b,opt){
  opt=opt||{};
  const r=el("button","row "+b.kind);r.type="button";r.dataset.id=b.id;
  if(isOver(b))r.classList.add("overdue");
  if(opt.rank!=null){
    r.classList.add("rank");
    const rk=el("div","rk",String(opt.rank));if(opt.rank<=3)rk.classList.add("top");r.append(rk);
  }else{
    const w=when(at(b),soft(b)),wd=el("div","when"+(soft(b)?" soft":""));
    const wi=el("i",null,w[1]);
    // 12시간 안쪽 진짜 마감은 남은 시간이 굴러간다(매초 tick 이 고친다)
    if(!soft(b)&&b.due_at){const ms=new Date(b.due_at)-NOW;
      if(ms>0&&ms<12*36e5){wi.className="left";wi.dataset.left=b.due_at;wi.textContent=leftTxt(ms)}}
    wd.append(el("b",null,w[0]),wi);r.append(wd);
  }
  const mid=el("div","mid");
  mid.append(el("div","lbl",b.label));
  const sub=[b.course||KIND[b.kind]];
  if(b.kind!=="assignment")sub.push(b.kind==="lecture"?"출석 강의":KIND[b.kind]);
  if(opt.rank!=null)sub.push(why(b));
  mid.append(el("div","sub",sub.filter(Boolean).join(" · ")));
  if(opt.bar!=null){const bar=el("div","pbar");const i=el("i");i.style.width=Math.round(opt.bar*100)+"%";bar.append(i);mid.append(bar)}
  r.append(mid,chips(b));
  if(isLive(b.id))r.classList.add("live");
  r.onclick=()=>openSheet(b);
  return r}
function listOf(xs,opt){
  const box=el("div","rows");
  xs.forEach((b,i)=>box.append(row(b,Object.assign({},opt,opt&&opt.ranked?{rank:i+1,bar:score(b)/maxScore}:{}))));
  return box}
// 완료함 — 시간칸·칩을 뺀 납작한 줄. 누르면 시트가 열리고 거기서 되돌린다.
function didList(xs){
  const box=el("div","rows did");
  xs.forEach(b=>{
    const r=el("button","row "+b.kind);r.type="button";
    const mid=el("div","mid");
    mid.append(el("div","lbl",b.label));
    mid.append(el("div","sub",[b.course||KIND[b.kind],
      SRC[b.done_src]||(b.done_at?didWhen(b.done_at):null),
      !b.done_at&&b.due_at?"마감 "+shortDate(b.due_at):null].filter(Boolean).join(" · ")));
    r.append(mid,el("div","tail",""));
    r.onclick=()=>openSheet(b);
    box.append(r)});
  return box}
function shortDate(iso){const k=K(iso);return(k.getUTCMonth()+1)+"/"+k.getUTCDate()+" ("+dow(iso)+")"}
function didWhen(iso){
  const n=ddays(iso);
  return(n===0?"오늘":n===-1?"어제":(-n)+"일 전")+" 완료"}

function sect(name,xs,hot){
  const h=el("div","sect"+(hot?" hot":""));
  const hh=xs.reduce((s,b)=>s+(b.est_hours||0),0);
  h.append(el("span",null,name),el("span",null,xs.length+"개"+(hh?" · ≈ "+hrs(hh):"")));
  return h}
let maxScore=1;

// ---------------------------------------------------------------- 탭
const TABS=[["week","이번 주"],["today","오늘"],["d3","3일"],["prio","우선순위"],["cal","달력"]];
let tab=(function(){try{return localStorage.getItem("erion-tab")||"week"}catch(e){return"week"}})();
if(!TABS.some(t=>t[0]===tab))tab="week";

function counts(){
  const over=ALL.filter(isOver);
  const late=ALL.filter(softLate);
  const up=b=>!isOver(b)&&!softLate(b)&&at(b);
  const today=ALL.filter(b=>up(b)&&ddays(at(b))===0);
  const d3=ALL.filter(b=>up(b)&&ddays(at(b))>=0&&ddays(at(b))<=2);
  return{over:over,late:late,today:today,d3:d3}}

function renderTabs(){
  const c=counts(),box=$("#tabs");box.textContent="";
  const n={week:null,today:c.over.length+c.today.length,d3:c.over.length+c.d3.length,prio:ALL.length,cal:null};
  for(const[k,name]of TABS){
    const b=el("button","tab"+(k===tab?" on":""));b.type="button";
    b.append(el("span",null,name));
    if(n[k])b.append(el("span","n",String(n[k])));
    b.onclick=()=>{tab=k;try{localStorage.setItem("erion-tab",k)}catch(e){}renderTabs();render(true)};
    box.append(b)}}

// ---------------------------------------------------------------- 각 화면
function viewToday(root){
  const c=counts();
  const d0=DATA.plan&&DATA.plan.days[0];
  if(d0){
    if(d0.busy.length){const hh=el("div","sect");hh.append(el("span",null,"오늘 일정"),el("span",null,""));root.append(hh);const bz=el("div","busy");bz.style.padding="0 2px";
      d0.busy.forEach(x=>bz.append(busyChip(x)));root.append(bz)}
    const ts=planTasks(d0);
    if(ts.length){const h=el("div","sect");h.append(el("span",null,"오늘 할 것 — 이번 주 계획에서"),
      el("span",null,"≈ "+hrs(ts.reduce((s,t)=>s+t.hours,0))));root.append(h,planRows(ts,d0))}
  }
  if(c.over.length){root.append(sect("지난 마감 — 아직 안 냄",c.over,true),listOf(c.over))}
  if(c.late.length)root.append(sect("목표일 지남 — 내가 정한 날",c.late),listOf(c.late));
  if(c.today.length)root.append(sect("오늘 마감·목표",c.today),listOf(c.today));
  if(!c.today.length){
    if(!c.over.length)root.append(el("div","empty","오늘 마감은 없어요."));
    const next=ALL.filter(b=>at(b)&&new Date(at(b))>=NOW).sort((a,b)=>at(a)<at(b)?-1:1).slice(0,3);
    if(next.length)root.append(sect("다음",next),listOf(next))}
  didSection(root)}

// 완료는 지우기가 아니다. 찍은 것들이 여기 남아 있어야 새로고침 뒤에도 되돌릴 수 있다.
const DID_SHOW=8;
let didAll=false;
function didSection(root){
  if(!DID.length)return;
  const xs=didAll?DID:DID.slice(0,DID_SHOW);
  root.append(sect("완료함",xs),didList(xs));
  if(DID.length>xs.length){const m=el("button","didmore note","그 밖에 "+(DID.length-xs.length)+"개 더 보기");
    m.type="button";m.onclick=()=>{didAll=true;render()};root.append(m)}}

// ---------------------------------------------------------------- 이번 주
// 계획은 서버(planner.py)가 연 순간에 짠 것이다. 여기서 완료를 찍으면 그 줄만 빼고,
// 다시 짜는 건 새로고침 때 한다 — 브라우저에서 배치를 흉내 내면 두 벌이 된다.
function busyChip(x){
  const t=x.kind==="allday"||x.kind==="mark"?"종일 "+x.label:x.s+"–"+x.e+" "+x.label;
  return el("span","bz "+x.kind,t)}
function planTasks(d){const gone=new Set(DID.map(b=>b.id));return d.tasks.filter(t=>!gone.has(t.id))}
function planRows(ts,d,drag){
  const box=el("div","rows");
  ts.forEach(t=>{
    const b=BLOCKS.find(x=>x.id===t.id)||t;
    const r=el("button","row wt "+t.kind);r.type="button";r.dataset.id=t.id;
    const hh=el("div","hrs",hrs(t.hours));if(t.part)hh.append(el("i",null,t.part));
    const mid=el("div","mid");
    mid.append(el("div","lbl",t.label));
    // 박은 건 📌 하나로 충분하다 — 따로 줄을 쓰지 않고 과목 줄 앞에 붙인다. 사유 문장은 claude.ai 쪽에 남는다
    const sub=el("div","sub",(t.pinned?"📌 ":"")+[t.course||KIND[t.kind],t.due_at?(t.kind==="lecture"?"출석 마감 ":"마감 ")+when(t.due_at).join(" "):null,
      t.est_guess?"추정 없음":null].filter(Boolean).join(" · "));
    if(!t.due_at&&t.target_at)sub.append(el("span","tgt"," · 목표 "+when(t.target_at).join(" ")));
    const lt=t.pinned&&/(마감|목표) 뒤/.exec(t.reason||"");
    if(lt)sub.append(el("span","late"," · "+lt[0]));
    mid.append(sub);
    if(!t.pinned&&t.reason)mid.append(el("div","why",t.reason));
    if(isLive(t.id)){r.classList.add("live");hh.append(el("i",null,"하는 중"))}
    r.append(hh,mid);r.onclick=()=>{if(!justDragged)openSheet(b,{t:t,d:d})};
    if(drag)draggable(r,t,d);
    box.append(r)});
  return box}

// ---------------------------------------------------------------- 끌어 옮기기
// 이번 주 탭의 줄을 다른 날 카드에 놓으면 그 날로 박는다(plan_pin, POST /web/plan).
// 마우스는 조금 움직이면 집히고, 손가락은 꾹 눌러야(0.35초) 집힌다 — 그냥 쓸면 스크롤이어야 한다.
// 놓은 뒤 배치는 서버가 다시 짠다 → 새로고침. 브라우저에서 배치를 흉내 내지 않는다(위와 같은 이유).
let drag=null,justDragged=false,moving=false;
const HOLD=350;
function draggable(r,t,d){
  r.classList.add("drag");
  r.addEventListener("pointerdown",e=>{
    if(moving||(e.pointerType==="mouse"&&e.button!==0))return;
    const s={r:r,t:t,d:d,x:e.clientX,y:e.clientY,cx:e.clientX,cy:e.clientY,pid:e.pointerId,
             touch:e.pointerType!=="mouse",live:false,timer:0,over:null,ghost:null};
    drag=s;
    if(s.touch)s.timer=setTimeout(()=>{if(drag===s)lift(s)},HOLD)})}
function lift(s){
  s.live=true;
  s.ghost=el("div","ghost",s.t.label+" · "+hrs(s.t.hours));
  document.body.append(s.ghost);
  s.r.classList.add("lifted");document.body.classList.add("dragging");
  try{navigator.vibrate&&navigator.vibrate(12)}catch(e){}
  track(s);scrollLoop(s)}
function track(s){
  s.ghost.style.transform="translate("+s.cx+"px,"+s.cy+"px)";
  const hit=document.elementFromPoint(s.cx,s.cy),c=hit&&hit.closest(".wday[data-date]");
  if(c!==s.over){if(s.over)s.over.classList.remove("drop");s.over=c;
    if(c&&c.dataset.date!==s.d.date)c.classList.add("drop")}}
// 7일 카드가 화면보다 길다 — 위아래 가장자리에 대고 있으면 스스로 굴러간다.
function scrollLoop(s){
  if(drag!==s||!s.live)return;
  const E=70,H=innerHeight;
  const v=s.cy<E?-(E-s.cy)/3:s.cy>H-E?(s.cy-(H-E))/3:0;
  if(v){scrollBy(0,v);track(s)}
  requestAnimationFrame(()=>scrollLoop(s))}
function endDrag(){
  const s=drag;drag=null;if(!s)return;
  clearTimeout(s.timer);
  if(s.ghost)s.ghost.remove();
  if(s.over)s.over.classList.remove("drop");
  s.r.classList.remove("lifted");document.body.classList.remove("dragging");
  if(s.live){justDragged=true;setTimeout(()=>{justDragged=false},60)}
  return s}
addEventListener("pointermove",e=>{
  const s=drag;if(!s||e.pointerId!==s.pid)return;
  s.cx=e.clientX;s.cy=e.clientY;
  if(!s.live){
    if(Math.hypot(s.cx-s.x,s.cy-s.y)<(s.touch?10:5))return;
    if(s.touch){endDrag();return}           // 누르기 전에 움직였으면 스크롤이다
    lift(s)}
  track(s)});
addEventListener("pointerup",e=>{
  if(!drag||e.pointerId!==drag.pid)return;
  const s=endDrag();
  if(s&&s.live&&s.over&&s.over.dataset.date!==s.d.date)moveTo(s.t,s.d.date,s.over.dataset.date)});
addEventListener("pointercancel",e=>{if(drag&&e.pointerId===drag.pid)endDrag()});
// 집은 뒤에는 손가락이 움직여도 스크롤하지 않는다. passive:false 여야 막힌다(iOS Safari).
document.addEventListener("touchmove",e=>{if(drag&&drag.live)e.preventDefault()},{passive:false});
document.addEventListener("contextmenu",e=>{if(drag)e.preventDefault()});

function toast(msg,err,ms){
  let t=$("#toast");if(!t){t=el("div","toast");t.id="toast";document.body.append(t)}
  t.textContent=msg;t.classList.toggle("err",!!err);t.classList.add("on");
  clearTimeout(toast.h);if(err||ms)toast.h=setTimeout(()=>t.classList.remove("on"),err?4000:ms)}
// to=null → 박음을 전부 풀고 자동 배치로.
function moveTo(t,from,to){
  if(moving)return;moving=true;
  toast(to?"옮기는 중…":"자동 배치로 되돌리는 중…");
  fetch(DATA.planUrl,{method:"POST",credentials:"same-origin",
    headers:{"content-type":"application/json","x-erion":"web"},
    body:JSON.stringify({id:t.id,from:from,to:to,hours:t.hours,whole:!t.part})})
    .then(r=>r.json().catch(()=>({ok:false,error:"HTTP "+r.status})))
    .then(r=>{if(!r||!r.ok)throw new Error((r&&r.error)||"알 수 없는 오류");
      try{sessionStorage.setItem("erion-scroll",String(scrollY))}catch(e){}
      location.reload()})
    .catch(e=>{moving=false;toast("못 옮겼어요 — "+(e.message||e),true)})}
function viewWeek(root){
  const P=DATA.plan;
  if(!P){root.append(el("div","empty","계획을 못 짰어요."+(DATA.plan_err?" — "+DATA.plan_err:"")));return}
  const days=P.days.slice(0,7);
  const need=days.reduce((s,d)=>s+planTasks(d).reduce((a,t)=>a+t.hours,0),0);
  const cap=days.reduce((s,d)=>s+d.cap,0);
  const h=el("div","sect");h.append(el("span",null,"7일 계획"),el("span",null,hrs(need||0)+" / "+hrs(cap)));
  root.append(h);
  if(P.short.length){
    root.append(el("div","warnbox","안 들어감 · "+P.short.map(x=>x.label+" −"+hrs(x.missing)).join(", ")))}
  const didBy={};
  DID.forEach(b=>{if(!at(b))return;const k=K(at(b));
    const key=k.getUTCFullYear()+"-"+String(k.getUTCMonth()+1).padStart(2,"0")+"-"+String(k.getUTCDate()).padStart(2,"0");
    (didBy[key]=didBy[key]||[]).push(b)});
  days.forEach(d=>{
    const ts=planTasks(d),used=ts.reduce((s,t)=>s+t.hours,0);
    const card=el("div","wday"+(d.today?" today":"")+(used>d.cap?" over":""));card.dataset.date=d.date;
    const wh=el("div","wh"),dt=el("div","dt",+d.date.slice(5,7)+"/"+(+d.date.slice(8))+" ("+d.dow+")");
    if(d.today)dt.append(el("small",null,"오늘"));
    wh.append(dt,el("div","ld",(used?hrs(used):"0")+" / "+(d.cap?hrs(d.cap):"0")+(d.cap_set?" · 직접 정함":"")));
    card.append(wh);
    const m=el("div","meter"),mi=el("i");mi.style.width=Math.min(100,d.cap?used/d.cap*100:used?100:0)+"%";m.append(mi);card.append(m);
    if(d.busy.length){const bz=el("div","busy");d.busy.forEach(x=>bz.append(busyChip(x)));card.append(bz)}
    if(ts.length)card.append(planRows(ts,d,true));
    else if(!d.cap)card.append(el("div","wempty",d.cap_note||"쉬는 날"));
    const dn=didBy[d.date];
    if(dn&&dn.length){const w=el("div","wdone");w.append("✓ ");
      dn.forEach((b,i)=>{if(i)w.append(", ");w.append(el("s",null,b.label))});card.append(w)}
    root.append(card)});
}

function viewD3(root){
  const c=counts();
  if(c.over.length)root.append(sect("지난 마감 — 아직 안 냄",c.over,true),listOf(c.over));
  if(c.late.length)root.append(sect("목표일 지남 — 내가 정한 날",c.late),listOf(c.late));
  const names=["오늘","내일","모레"];
  let any=false;
  for(let d=0;d<3;d++){
    const xs=c.d3.filter(b=>ddays(at(b))===d);
    if(!xs.length)continue;any=true;
    const k=K(new Date(NOW.getTime()+d*864e5));
    root.append(sect(names[d]+" · "+(k.getUTCMonth()+1)+"/"+k.getUTCDate()+" ("+DOW[k.getUTCDay()]+")",xs),listOf(xs))}
  if(!any&&!c.over.length&&!c.late.length)root.append(el("div","empty","3일 안에 마감이 없어요."))}

function viewPrio(root){
  const xs=ALL.slice().sort((a,b)=>score(b)-score(a));
  if(!xs.length){root.append(el("div","empty","남은 게 없어요."));return}
  maxScore=score(xs[0])||1;
  root.append(sect("급한 순",xs));
  root.append(listOf(xs,{ranked:true}));}

let calMonth=null, calSel=null;
function viewCal(root){
  const kn=K(NOW);
  if(!calMonth)calMonth=[kn.getUTCFullYear(),kn.getUTCMonth()];
  const[Y,M]=calMonth;
  const box=el("div","cal");
  const bar=el("div","calbar");
  bar.append(el("div","mo",Y+"년 "+(M+1)+"월"));
  const nav=el("div","calnav");
  const mk=(t,dm)=>{const b=el("button","ib",t);b.type="button";
    b.onclick=()=>{const d=new Date(Date.UTC(Y,M+dm,1));calMonth=[d.getUTCFullYear(),d.getUTCMonth()];calSel=null;render()};return b};
  nav.append(mk("‹",-1),mk("오늘",0));
  nav.lastChild.onclick=()=>{calMonth=[kn.getUTCFullYear(),kn.getUTCMonth()];calSel=null;render()};
  nav.append(mk("›",1));
  bar.append(nav);box.append(bar);

  const dows=el("div","dows");for(let i=0;i<7;i++)dows.append(el("span",null,DOW[i]));box.append(dows);

  // 날짜별로 묶는다
  const by={},dby={},eby={};
  const keyOf=iso=>{const k=K(iso);return k.getUTCFullYear()+"-"+k.getUTCMonth()+"-"+k.getUTCDate()};
  ALL.forEach(b=>{if(!at(b))return;const key=keyOf(at(b));(by[key]=by[key]||[]).push(b)});
  DID.forEach(b=>{if(!at(b))return;const key=keyOf(at(b));(dby[key]=dby[key]||[]).push(b)});
  // 일정은 시작한 날에 건다. 종일(자정~자정) 이벤트의 끝은 다음 날 0시라 거기엔 안 건다.
  (DATA.events||[]).forEach(e=>{const key=keyOf(e.start_at);(eby[key]=eby[key]||[]).push(e)});

  const first=new Date(Date.UTC(Y,M,1)), start=first.getUTCDay();
  const last=new Date(Date.UTC(Y,M+1,0)).getUTCDate();
  const prevLast=new Date(Date.UTC(Y,M,0)).getUTCDate();
  const days=el("div","days");
  const todayKey=kn.getUTCFullYear()+"-"+kn.getUTCMonth()+"-"+kn.getUTCDate();
  for(let i=0;i<42;i++){
    let y=Y,m=M,d=i-start+1,mute=false;
    if(d<1){d=prevLast+d;m=M-1;mute=true}
    else if(d>last){d=d-last;m=M+1;mute=true}
    const dt=new Date(Date.UTC(y,m,d)),key=dt.getUTCFullYear()+"-"+dt.getUTCMonth()+"-"+dt.getUTCDate();
    const xs=by[key]||[],ds=dby[key]||[],es=eby[key]||[];
    const cell=el("button","day"+(mute?" mute":"")+(xs.length?" has":"")+(key===todayKey?" today":"")+(key===calSel?" sel":""));
    cell.type="button";
    if(xs.some(isOver))cell.classList.add("over");
    cell.append(el("div","dnum",String(d)));
    const dots=el("div","dots");
    const kinds=[...new Set(xs.map(b=>b.kind))].slice(0,3);
    // 그 날 그 종류가 전부 목표일뿐이면 속 빈 점
    kinds.forEach(k=>dots.append(el("i",k+(xs.filter(b=>b.kind===k).every(soft)?" soft":""))));
    if(xs.length>kinds.length)dots.append(el("i","more","+"+(xs.length-kinds.length)));
    if(!xs.length&&ds.length)dots.append(el("i",ds[0].kind+" did"));
    cell.append(dots);
    if(es.length)cell.append(el("i","evbar"));
    cell.onclick=()=>{calSel=(calSel===key?null:key);render()};
    days.append(cell)}
  box.append(days);
  const lg=el("div","legend");
  [["mark","Canvas 과제"],["lec","강의영상 · 출석"],["mine","내가 추가"]].forEach(([k,n])=>{
    const s=el("span");const b=el("b");b.style.background="var(--"+k+")";
    s.append(b,el("span",null,n));lg.append(s)});
  {const s=el("span"),b=el("b");b.style.cssText="background:none;box-shadow:inset 0 0 0 1.2px var(--mine)";
   s.append(b,el("span",null,"내가 정한 날"));lg.append(s)}
  {const s=el("span"),b=el("b");b.style.cssText="width:14px;height:2px;border-radius:1px;background:var(--mark2)";
   s.append(b,el("span",null,"캘린더 일정"));lg.append(s)}
  box.append(lg);
  root.append(box);

  if(calSel){
    const xs=(by[calSel]||[]).slice().sort((a,b)=>at(a)<at(b)?-1:1);
    const ds=dby[calSel]||[],es=(eby[calSel]||[]).slice().sort((a,b)=>a.start_at<b.start_at?-1:1);
    const p=calSel.split("-"),name=(+p[1]+1)+"월 "+p[2]+"일";
    if(es.length){
      root.append(sect(name+" · 일정",es));
      const box=el("div","evs");
      es.forEach(e=>{const r=el("div","ev");
        const allday=hm(e.start_at)==="00:00"&&e.end_at&&(new Date(e.end_at)-new Date(e.start_at))%864e5===0;
        r.append(el("span","t",allday?"종일":hm(e.start_at)+(e.end_at?"–"+(hm(e.end_at)==="00:00"?"24:00":hm(e.end_at)):"")),
          el("span",null,e.title+(e.location?" · "+e.location.split("(")[0]:"")));box.append(r)});
      root.append(box)}
    root.append(sect(name+" · 마감",xs),xs.length?listOf(xs):el("div","empty","이 날 남은 마감은 없어요."));
    if(ds.length)root.append(sect("이 날 마감 — 이미 끝냄",ds),didList(ds))}
}

// ---------------------------------------------------------------- 머리말·렌더
// 머리말 — 2026-09-26 "인사말이 고정인 것 같아, 안 바뀌어". 예전엔 '오늘 마감 개수'만 봐서 마감 없는 날은
// 하루 종일 "오늘은 비었어요" 였다(계획엔 5시간이 있는데도). 이제는 오늘 계획·한 것·지금 시각·하는 중을 본다.
// 분마다, 완료를 찍을 때마다, 타이머를 켜고 끌 때마다 다시 쓴다. 톤은 건조하게 — 응원 문구는 안 넣는다.
function freeToday(){
  const d0=DATA.plan&&DATA.plan.days[0];if(!d0||!d0.today)return null;
  const h=nowH(),segs=d0.busy.filter(x=>x.kind==="class"||x.kind==="event").map(x=>({s:hnum(x.s),e:hnum(x.e)}));
  let free=0,t=Math.max(h,WS);
  segs.sort((a,b)=>a.s-b.s).forEach(x=>{if(x.e<=t)return;if(x.s>t)free+=x.s-t;t=Math.max(t,x.e)});
  if(t<WE)free+=WE-t;return free}
function head(){
  const k=K(NOW),st=$("#stamp");st.textContent=(k.getUTCMonth()+1)+"월 "+k.getUTCDate()+"일 "+dow(NOW)+"요일";
  const ck=el("span","clk"," · "+hm(NOW).slice(0,2));ck.append(el("b",null,":"),hm(NOW).slice(3));st.append(ck);
  const c=counts(),hl=$("#headline"),sub=$("#subline");hl.textContent="";sub.textContent="";
  const E=t=>el("em",null,t);
  if(!ALL.length){hl.textContent="남은 게 없어요.";return}
  const d0=DATA.plan&&DATA.plan.days[0]&&DATA.plan.days[0].today?DATA.plan.days[0]:null;
  const all0=d0?d0.tasks:[],left0=d0?planTasks(d0):[];
  const lh=left0.reduce((a,t)=>a+t.hours,0),doneN=all0.length-left0.length;
  const hr=k.getUTCHours(),free=freeToday();
  if(FS.cur){
    const m=Math.floor(run()/6e4);
    hl.append(E(FS.cur.label),paused()?" 멈춤.":m?" "+(m<60?m+"분":dur(m*6e4))+"째.":" 시작.")}
  else if(c.over.length)hl.append("지난 마감 ",E(c.over.length+"개"),".");
  else{
    if(!all0.length){if(c.today.length)hl.append("오늘 마감 ",E(c.today.length+"개"),".");else hl.append("오늘 계획은 비었어요.")}
    else if(!left0.length)hl.append("오늘 몫 ",E(all0.length+"개"),", 다 했어요.");
    else if(hr<6)hl.append("새벽이에요. 오늘 몫은 ",E(hrs(lh)),".");
    else if(doneN)hl.append(all0.length+"개 중 ",E(doneN+"개"),", 남은 건 ",E(hrs(lh)),".");
    else hl.append("오늘 ",E(left0.length+"개"),", ",E(hrs(lh)),".")}
  const bits=[];
  // 남은 빈 시간보다 할 게 많으면 그걸 먼저 말한다 — 새벽엔 빈 시간이 9시부터라 해당 없음
  if(left0.length&&free!=null&&hr>=6&&lh>free+.01)bits.push("남은 빈 시간("+hrs(free)+")보다 "+hrs(lh-free)+" 많아요");
  else if(left0.length&&!FS.cur){const t=BLOCKS.find(b=>b.id===left0[0].id);bits.push("먼저 "+(t?t.label:left0[0].label))}
  const next=ALL.filter(b=>b.due_at&&new Date(b.due_at)>=NOW).sort((a,b)=>a.due_at<b.due_at?-1:1)[0];
  if(next)bits.push("가장 가까운 마감 "+next.label+" ("+when(next.due_at).join(" ")+")");
  if(DATA.plan){const ds=DATA.plan.days.slice(0,7),need=ds.reduce((a,d)=>a+planTasks(d).reduce((x,t)=>x+t.hours,0),0);
    bits.push("7일 계획 "+hrs(need||0)+" / "+hrs(ds.reduce((a,d)=>a+d.cap,0)))}
  sub.textContent=bits.join(" · ")}

function render(anim){
  const root=$("#view");root.textContent="";
  if(tab==="week")viewWeek(root);
  else if(tab==="today")viewToday(root);
  else if(tab==="d3")viewD3(root);
  else if(tab==="prio")viewPrio(root);
  else viewCal(root);
  if(DATA.lec_err)root.append(el("div","note","강의영상 인정기간을 못 읽었어요 — "+DATA.lec_err));
  if(anim&&!calm())[...root.children].slice(0,16).forEach((c,i)=>{c.style.setProperty("--i",i);c.classList.add("rise")})}

// ---------------------------------------------------------------- 완료 폭죽
// 버튼에서 조각 몇 개가 튀었다 떨어진다. 동작 줄이기 설정이면 안 한다.
const calm=()=>matchMedia("(prefers-reduced-motion: reduce)").matches;
function burst(from){
  if(calm()||!Element.prototype.animate)return;
  const r=from.getBoundingClientRect(),cx=r.left+r.width/2,cy=r.top+r.height/2;
  const cs=getComputedStyle(document.documentElement),
        cols=["--mark","--mark2","--lec","--hot"].map(v=>cs.getPropertyValue(v).trim());
  for(let i=0;i<22;i++){
    const p=el("i","fx");p.style.background=cols[i%cols.length];document.body.append(p);
    const a=-Math.PI/2+(Math.random()-.5)*2.2,v=70+Math.random()*90,
          dx=Math.cos(a)*v,dy=Math.sin(a)*v,rot=(Math.random()-.5)*720;
    p.animate([{transform:`translate(${cx}px,${cy}px) rotate(0)`,opacity:1},
               {transform:`translate(${cx+dx}px,${cy+dy}px) rotate(${rot/2}deg)`,opacity:1,offset:.45},
               {transform:`translate(${cx+dx*1.3}px,${cy+dy+140}px) rotate(${rot}deg)`,opacity:0}],
              {duration:850+Math.random()*300,easing:"cubic-bezier(.2,.7,.4,1)"}).onfinish=()=>p.remove()}}

// ---------------------------------------------------------------- 시트
// 2026-09-26 "정보가 너무 많고 버튼도 많아 보여" — 지우지 않고 접었다. 늘 보이는 것: 제목 · 기한 · 계획한 날 ·
// 예상시간 · 완료/지금 하기/링크. 접힌 것: 원제목 · 주차·길이(제목 밑 한 줄로) · 정상일 때의 '열려 있음' · 긴 설명.
// 닫기 버튼은 뺐다 — 바깥을 누르거나 끌어내리면 닫히고, 오른쪽 위 ✕ 가 남아 있다.
const sheet=$("#sheet"),scrim=$("#scrim");
function srow(dl,k,v){if(v==null||v==="")return;dl.append(el("dt",null,k),el("dd",null,v))}
const linkName=b=>b.kind==="lecture"?"강의 열기":/canvas/.test(b.url)?"Canvas 에서 열기":"링크 열기";
function md(date){return+date.slice(5,7)+"/"+(+date.slice(8))}
// 이 블록이 이번 주 계획의 어디에 있나. 조각이 여럿이면 어느 걸 옮길지 모르니 ctx 가 있어야 한다.
function planOf(b,ctx){
  if(ctx&&ctx.t&&ctx.d)return ctx;
  if(!DATA.plan)return null;
  const hit=[];DATA.plan.days.forEach(d=>d.tasks.forEach(t=>{if(t.id===b.id)hit.push({t:t,d:d})}));
  return hit.length===1?hit[0]:hit.length?{many:hit}:null}
function openSheet(b,ctx){
  sheet.textContent="";sheet.append(el("div","grab"));
  const x=withIc(el("button","s-x"),"x-lg");x.type="button";x.setAttribute("aria-label","닫기");x.onclick=closeSheet;sheet.append(x);
  sheet.append(el("div","s-kind",KIND[b.kind]+(b.course?" · "+b.course:"")));
  sheet.append(el("div","s-title"+(b.done?" did":""),b.label));
  const meta=[];
  if(b.kind==="lecture"){if(b.module)meta.push(b.module);if(b.minutes)meta.push(b.minutes+"분")}
  if(b.points)meta.push((+b.points)+"점");
  if(b.title&&b.title!==b.label)meta.push("원제목 "+b.title);
  if(meta.length)sheet.append(el("div","s-orig",meta.join(" · ")));
  if(b.url&&/^https?:\/\//.test(b.url)){
    const a=el("a","s-link",linkName(b));a.append(ic("arrow-up-right"));
    a.href=b.url;a.target="_blank";a.rel="noopener";sheet.append(a)}
  const ext=b.done&&(b.done_src==="canvas"||b.done_src==="watched");
  if(ext)sheet.append(el("div","s-done",b.done_src==="canvas"?"✓ Canvas 제출":"✓ 시청함"));
  const dl=el("dl","s-grid");
  if(b.kind==="lecture"){
    srow(dl,"기한",b.due_at?dueLong(b.due_at)+" · "+when(b.due_at).join(" "):null);
    if(b.period_status==="not_open")srow(dl,"열림",b.open_at?dueLong(b.open_at):"아직 안 열림");
    else if(b.period_status==="late_after")srow(dl,"상태","지각 구간");
    else if(b.period_status&&b.period_status!=="open")srow(dl,"상태",b.period_status);
  }else{
    srow(dl,"마감",b.due_at?dueLong(b.due_at)+" · "+when(b.due_at).join(" "):"없음");
    if(b.target_at)srow(dl,"목표",dueLong(b.target_at)+" · 내가 정한 날");
    if(!b.done){dl.append(el("dt",null,"예상"));const dd=el("dd");dd.append(estBox(b));dl.append(dd)}
    else srow(dl,"예상",hrs(b.est_hours));
    if(b.actual_hours)srow(dl,"실제",hrs(b.actual_hours));
  }
  if(!b.done&&!moving){const p=planOf(b,ctx);if(p){dl.append(el("dt",null,"계획"));const dd=el("dd");dd.append(planPick(b,p));dl.append(dd)}}
  spentRow(dl,b);
  sheet.append(dl);
  if(b.prep_note){sheet.append(el("div","s-h","준비할 것"),el("div","s-prep",b.prep_note))}
  if(b.kind!=="lecture"&&b.description){
    sheet.append(el("div","s-h","설명"));
    const d=el("div","s-desc fold",b.description);sheet.append(d);
    requestAnimationFrame(()=>{if(d.scrollHeight<=d.clientHeight+4){d.classList.remove("fold");return}
      const more=el("button","s-more","더 보기");more.type="button";
      more.onclick=()=>{d.style.maxHeight=d.scrollHeight+"px";d.classList.add("open");more.remove()};
      d.after(more)})}
  const top=el("div","s-act");
  if(ext)top.style.display="none";
  // 상태는 버튼 모양이 말한다: 빈 테두리 = 아직, 채워진 초록 = 끝남(다시 누르면 취소).
  const dn=withIc(el("button","btn done"+(b.done?" on":"")),"check-lg",b.done?"완료됨":"완료");
  dn.type="button";dn.title=b.done?"눌러서 완료 취소":"";
  dn.onclick=()=>{
    const want=!b.done;
    dn.disabled=true;
    const fail=msg=>{dn.disabled=false;toast("못 적었어요 — "+msg,true)};
    postDone(b,want)
      .then(r=>{if(!r||!r.ok){fail((r&&r.error)||"알 수 없는 오류");return}
                if(!want){applyDone(b,false);openSheet(b);return}
                dn.classList.add("on","pop");withIc(dn,"check-lg","완료됨");burst(dn);
                if(isLive(b.id))focusOp("stop").catch(()=>{});
                const rs=document.querySelectorAll('.row[data-id="'+CSS.escape(b.id)+'"]');
                rs.forEach(x=>x.classList.add("leaving"));
                setTimeout(()=>{applyDone(b,true);openSheet(b)},rs.length&&!calm()?330:0)})
      .catch(e=>fail(String(e)))};
  top.append(dn);
  if(!b.done){
    const on=isLive(b.id),go=withIc(el("button","btn go"+(on?" on":"")),on?"pause-fill":"play-fill",on?"하는 중":"지금 하기");go.type="button";
    go.onclick=()=>{if(on)openFocus(go);else startFocus(b,go)};
    top.append(go)}
  sheet.append(top);
  sheet.scrollTop=0;sheet.classList.remove("empty-pane");sheet.style.transform="";
  if(!wide())scrim.classList.add("on");
  sheet.classList.add("on")}
// 바텀시트를 손가락으로 끌어내려 닫는다 — 닫기 버튼을 뺀 대신.
{let sy=null,dy=0;
 sheet.addEventListener("touchstart",e=>{if(wide()||sheet.scrollTop>0||e.target.closest(".wheel,.estbox"))return;sy=e.touches[0].clientY;dy=0},{passive:true});
 sheet.addEventListener("touchmove",e=>{if(sy==null)return;dy=Math.max(0,e.touches[0].clientY-sy);
   if(dy>0){sheet.style.transition="none";sheet.style.transform="translate(-50%,"+dy+"px)"}},{passive:true});
 sheet.addEventListener("touchend",()=>{if(sy==null)return;sy=null;sheet.style.transition="";
   if(dy>90)closeSheet();sheet.style.transform=""})}
// 예상시간 고치기 — −/+ 로 맞추고 저장. 계획이 바뀌니 저장하면 새로고침하고 이 시트를 다시 연다.
function estBox(b){
  const box=el("div","estbox"),orig=b.est_hours||null;let v=orig||1;
  const val=el("span","v"),mi=withIc(el("button","st"),"dash-lg"),pl=withIc(el("button","st"),"plus-lg"),sv=el("button","sv","저장");
  mi.setAttribute("aria-label","줄이기");pl.setAttribute("aria-label","늘리기");
  mi.type=pl.type=sv.type="button";
  const nt=el("span","nt",!orig?"아직 없음 — 맞춰서 저장":b.est_basis==="title"?"제목으로 추정":b.est_basis==="user"?"직접 고침":"");
  const upd=()=>{val.textContent=hrs(v);sv.hidden=v===orig};
  mi.onclick=()=>{v=Math.max(.25,+(v-(v<=1?.25:.5)).toFixed(2));upd()};
  pl.onclick=()=>{v=Math.min(40,+(v+(v<1?.25:.5)).toFixed(2));upd()};
  sv.onclick=()=>{
    sv.disabled=true;
    fetch(DATA.estUrl,{method:"POST",credentials:"same-origin",
      headers:{"content-type":"application/json","x-erion":"web"},body:JSON.stringify({id:b.id,hours:v})})
      .then(r=>r.json().catch(()=>({ok:false,error:"HTTP "+r.status})))
      .then(r=>{if(!r||!r.ok)throw new Error((r&&r.error)||"알 수 없는 오류");
        try{sessionStorage.setItem("erion-scroll",String(scrollY))}catch(e){}
        location.href=location.pathname+"?tab="+tab+"&open="+encodeURIComponent(b.id)})
      .catch(e=>{sv.disabled=false;toast("못 고쳤어요 — "+(e.message||e),true)})};
  upd();const f=el("span","fld step");f.append(mi,val,pl);box.append(f,sv);if(nt.textContent)box.append(nt);
  return box}
// 계획한 날 — 누르면 날짜 바퀴가 펼쳐지며 한 바퀴 돌아 지금 날에 선다. 굴려서 고르고 "옮기기".
// 예전의 날짜 버튼 7개 줄을 이걸로 바꿨다(2026-09-26). 끌어 옮기기(이번 주 탭)는 그대로다.
function planPick(b,p){
  const box=el("span","ppick");
  if(p.many){box.append(p.many.map(x=>md(x.d.date)+"("+x.d.dow+") "+hrs(x.t.hours)).join(" · "));return box}  // 나눠 담긴 건 몫이 곧 정보다
  const pinned=!!p.t.pinned;
  const c=el("button","pchip fld");c.type="button";c.append(ic("calendar3"));
  c.append(md(p.d.date)+" ("+p.d.dow+")"+(p.d.today?" 오늘":"")+(p.t.part?" · 이 조각 "+hrs(p.t.hours):""));
  if(pinned)c.append(el("i",null,"고정"));
  c.append(ic("chevron-down","chev"));
  c.onclick=()=>{const open=sheet.querySelector(".wheelbox");if(open){foldWheel(open,c);return}
    c.classList.add("on");sheet.querySelector(".s-grid").after(wheel(p,pinned,c))};
  box.append(c);return box}
function foldWheel(w,c){c.classList.remove("on");w.classList.add("bye");setTimeout(()=>w.remove(),calm()?0:260)}
const WH=44;                                        // 바퀴 한 칸 높이
function wheel(p,pinned,chip){
  const days=DATA.plan.days.slice(0,7),cur=Math.max(0,days.findIndex(d=>d.date===p.d.date));
  const box=el("div","wheelbox"),wl=el("div","wheel"),sc=el("div","wsc");
  const items=days.map((d,i)=>{
    const it=el("div","wi"+(i===cur?" cur":""));
    it.append(el("b",null,md(d.date)+" "+d.dow+(d.today?" · 오늘":"")));
    const free=Math.max(0,d.cap-d.used);
    it.append(el("i",null,i===cur?"지금 여기":"여유 "+(hrs(free)||"0분")));
    it.onclick=()=>spin(i,260);sc.append(it);return it});
  wl.append(sc,el("div","wband"));box.append(wl);
  const act=el("div","wact"),go=el("button","btn wgo","");go.type="button";act.append(go);
  if(pinned){const u=el("button","btn undo","자동 배치로");u.type="button";u.onclick=()=>moveTo(p.t,null,null);act.append(u)}
  box.append(act);
  let sel=cur,anim=0;
  const paint=()=>{
    const mid=sc.scrollTop/WH;
    items.forEach((it,i)=>{const o=i-mid,a=Math.max(-1,Math.min(1,o/3));
      it.style.transform="rotateX("+(-a*62)+"deg) translateZ(0)";it.style.opacity=String(1-Math.abs(a)*.8)});
    const n=Math.round(mid);
    if(n!==sel&&n>=0&&n<items.length){sel=n;items.forEach((x,i)=>x.classList.toggle("sel",i===n));
      try{anim||navigator.vibrate&&navigator.vibrate(4)}catch(e){}
      label()}};
  const label=()=>{const d=days[sel];go.disabled=sel===cur;
    go.textContent=sel===cur?"날짜를 굴려 고르세요":md(d.date)+" ("+d.dow+")로 옮기기"};
  go.onclick=()=>{if(sel!==cur)moveTo(p.t,p.d.date,days[sel].date)};
  // 한 바퀴: 끝까지 갔다가 현재 날로 감속해 돌아온다. 스냅은 도는 동안만 끈다.
  function spin(to,ms,from){
    const a0=from!=null?from:sc.scrollTop,a1=to*WH,t0=performance.now();
    if(calm()){sc.scrollTop=a1;paint();return}
    anim=1;sc.classList.add("free");
    const step=t=>{const k=Math.min(1,(t-t0)/ms),e=1-Math.pow(1-k,4);
      sc.scrollTop=a0+(a1-a0)*e;paint();
      if(k<1)requestAnimationFrame(step);else{anim=0;sc.classList.remove("free")}};
    requestAnimationFrame(step)}
  sc.addEventListener("scroll",()=>{if(!anim)paint()},{passive:true});
  items.forEach((x,i)=>x.classList.toggle("sel",i===cur));label();
  requestAnimationFrame(()=>{sc.scrollTop=(items.length-1)*WH;paint();spin(cur,900)});
  return box}
// 태블릿(≥900px)에서는 시트가 오른쪽 기둥으로 상주한다. 닫으면 사라지는 게 아니라
// 안내 문구로 돌아간다. 폰에서는 그대로 바텀시트라 닫히면 안 보인다.
const wide=()=>matchMedia("(min-width:900px)").matches;
function paneIdle(){
  sheet.textContent="";sheet.classList.add("empty-pane");
  sheet.append(el("div","grab"));
  sheet.append(el("div","s-kind","상세"));
  sheet.append(el("div","s-desc none","항목을 고르면 여기 열려요."));
  if(wide())sheet.classList.add("on")}
function closeSheet(){
  scrim.classList.remove("on");
  if(wide())paneIdle();
  else sheet.classList.remove("on")}
scrim.onclick=closeSheet;
document.addEventListener("keydown",e=>{if(e.key!=="Escape")return;if(!$("#menu").hidden)$("#gear").click();else if(fzOpen)closeFocus();else closeSheet()});
// 톱니 하나에 알림·테마·나가기를 모았다(2026-09-26). 자주 안 누르는 것들이다.
{const g=$("#gear"),m=$("#menu");
 const set=on=>{m.hidden=!on;g.setAttribute("aria-expanded",on);g.classList.toggle("on",on)};
 g.onclick=e=>{e.stopPropagation();set(m.hidden)};
 document.addEventListener("click",e=>{if(!m.hidden&&!m.contains(e.target))set(false)});
 m.addEventListener("click",e=>{if(e.target.closest("#theme"))return;setTimeout(()=>set(false),150)})}
withIc($("#gear"),"gear");
$("#theme").onclick=()=>{
  const r=document.documentElement,cur=r.dataset.theme||(matchMedia("(prefers-color-scheme: light)").matches?"light":"dark");
  const nx=cur==="light"?"dark":"light";r.dataset.theme=nx;try{localStorage.setItem("erion-theme",nx)}catch(e){}};
// ---------------------------------------------------------------- 알림 켜기
// 홈화면에 깐 erion 에서 누른다. 서비스 워커(/erion/sw.js) → 푸시 구독 → 서버에 등록 → 시험 한 통.
// 이미 켜져 있으면 누를 때 시험 알림만 다시 보낸다(구독도 새로 적는다 — 서버가 지웠을 수 있다).
const bell=$("#bell");
function b64u(s){s=s.replace(/-/g,"+").replace(/_/g,"/");const r=atob(s+"=".repeat((4-s.length%4)%4));
  return Uint8Array.from(r,c=>c.charCodeAt(0))}
let bellOff=false;
function setBell(on){bell.textContent=on?"🔔 알림 켜짐 · 시험 보내기":"🔕 알림 켜기";bellOff=!on;gearDot()}
if("serviceWorker" in navigator&&"PushManager" in window&&"Notification" in window){
  bell.hidden=false;
  navigator.serviceWorker.register(DATA.swUrl).then(reg=>reg.pushManager.getSubscription())
    .then(s=>setBell(!!s&&Notification.permission==="granted")).catch(()=>setBell(false));
  bell.onclick=async()=>{
    try{
      const perm=await Notification.requestPermission();
      if(perm!=="granted"){toast("알림 권한이 꺼져 있어요 — 폰 설정에서 erion 알림을 허용해 주세요",true);return}
      const reg=await navigator.serviceWorker.ready;
      let s=await reg.pushManager.getSubscription();
      if(!s)s=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64u(DATA.vapid)});
      if(!s)throw new Error("구독을 못 만들었어요");
      toast("알림 등록 중…");
      const r=await fetch(DATA.pushUrl,{method:"POST",credentials:"same-origin",
        headers:{"content-type":"application/json","x-erion":"web"},
        body:JSON.stringify({sub:s.toJSON(),test:true})}).then(r=>r.json());
      if(!r.ok)throw new Error(r.error||"등록 실패");
      setBell(true);
      toast(r.test>=200&&r.test<300?"켜졌어요 — 시험 알림을 보냈어요":"등록은 됐는데 시험 알림이 실패했어요 ("+r.test+", 권한 "+Notification.permission+")",!(r.test>=200&&r.test<300));
      setTimeout(()=>{const t=$("#toast");if(t)t.classList.remove("on")},3500)
    }catch(e){toast("알림을 못 켰어요 — "+(e.message||e),true)}}}


// ---------------------------------------------------------------- 알림함 (2026-09-26)
// 보낸 푸시를 모아 둔다(push_log). 알림을 밀어 지웠거나 기기가 구독이 끊겨 못 받은 것도 여기 있다.
// 읽음 표시는 이 기기에만(localStorage) — 기기마다 따로 봐도 괜찮은 편의 기능이다.
const INB=DATA.inbox||[],inb=$("#inb");
const INK={morning:["sun","아침"],evening:["moon-stars","저녁"],lecopen:["play-circle","강의 열림"],notice:["megaphone","공지"]};
const inKind=k=>INK[k]||(/^due/.test(k)?["alarm","마감 전"]:["bell","알림"]);
let seenAt=(function(){try{return localStorage.getItem("erion-inbox-seen")||""}catch(e){return""}})();
function inbBadge(){
  const n=INB.filter(x=>x.at>seenAt).length;
  withIc(inb,n?"bell-fill":"bell");inb.classList.toggle("has",!!n);
  if(n)inb.append(el("b","cnt",n>9?"9+":String(n)))}
function inbWhen(iso){const n=ddays(iso);return(n===0?"오늘":n===-1?"어제":shortDate(iso))+" "+hm(iso)}
// 본문 한 줄("· 이름 — 내일 자정")에서 블록을 찾아 누르면 그 시트가 열리게 한다
function lineBlock(t){const x=t.replace(/^·\s*/,"");let best=null;
  BLOCKS.concat(DID).forEach(b=>{if(b.label&&x.startsWith(b.label)&&(!best||b.label.length>best.label.length))best=b});return best}
function go(url){
  try{const q=new URL(url,location.href).searchParams,t=q.get("tab"),o=q.get("open");
    if(q.get("notice")){openNotices(q.get("notice"));return}
    if(t&&TABS.some(x=>x[0]===t)&&t!==tab){tab=t;try{localStorage.setItem("erion-tab",tab)}catch(e){}renderTabs();render(true)}
    const b=o&&BLOCKS.concat(DID).find(x=>x.id===o);if(b){openSheet(b);return}
    closeSheet();scrollTo({top:$("#tabs").offsetTop-4,behavior:calm()?"auto":"smooth"})}catch(e){closeSheet()}}
function openInbox(){
  const prev=seenAt;
  sheet.textContent="";sheet.append(el("div","grab"));
  const x=withIc(el("button","s-x"),"x-lg");x.type="button";x.setAttribute("aria-label","닫기");x.onclick=closeSheet;sheet.append(x);
  sheet.append(el("div","s-kind","알림함"));
  sheet.append(el("div","s-title",INB.length?"받은 알림":"받은 알림이 없어요"));
  if(bell.hidden===false&&bellOff){
    const w=el("div","ib-off");w.append(el("span",null,"이 기기는 알림이 꺼져 있어요"));
    const b=el("button","s-link","켜기");b.type="button";b.onclick=()=>bell.click();w.append(b);sheet.append(w)}
  let day="";
  INB.forEach((n,i)=>{
    const d=ddays(n.at),dl=d===0?"오늘":d===-1?"어제":shortDate(n.at);
    if(dl!==day){day=dl;sheet.append(el("div","s-h",dl))}
    const k=inKind(n.kind),card=el("div","inn "+(n.kind.startsWith("due")?"due":n.kind)+(n.at>prev?" new":""));
    card.style.setProperty("--i",Math.min(i,12));
    const hd=el("button","inh");hd.type="button";hd.append(ic(k[0]),el("span","t",n.title),el("span","w",hm(n.at)));
    hd.onclick=()=>go(n.url||"?tab="+(n.kind==="morning"||n.kind==="evening"?"today":"d3"));
    card.append(hd);
    if(n.body){const ls=el("div","inl");n.body.split("\n").forEach(t=>{
      const b=lineBlock(t),row=el(b?"button":"div","ln"+(b?" go":""),t.replace(/^·\s*/,""));
      if(b){row.type="button";row.onclick=()=>openSheet(b)}ls.append(row)});card.append(ls)}
    if(!n.sent)card.append(el("div","nsent","기기로는 못 보냄"));
    sheet.append(card)});
  if(INB.length)sheet.append(el("div","note","최근 45일 · 알림을 눌러 지워도 여기엔 남아요."));
  sheet.scrollTop=0;sheet.classList.remove("empty-pane");sheet.style.transform="";
  if(!wide())scrim.classList.add("on");
  sheet.classList.add("on");
  if(INB.length){seenAt=INB[0].at;try{localStorage.setItem("erion-inbox-seen",seenAt)}catch(e){}inbBadge()}}
inb.onclick=openInbox;inbBadge();

// ---------------------------------------------------------------- 공지 (2026-09-26)
// Canvas 공지. 폴러가 30분마다 notice 에 담는다. 안 읽음 = Canvas 가 unread 이고 여기서도 안 펼친 것.
// 펼치면 서버에 읽음으로 적는다(기기 사이에 같이 간다). Canvas 쪽 읽음 표시는 안 바꾼다.
const NTC=DATA.notices||[],ntc=$("#ntc");
function ntcBadge(){
  const n=NTC.filter(x=>x.unread).length;
  withIc(ntc,n?"megaphone-fill":"megaphone");ntc.classList.toggle("has",!!n);
  if(n)ntc.append(el("b","cnt",n>9?"9+":String(n)))}
function ntcRead(n,card){
  if(!n.unread)return;n.unread=false;card.classList.remove("new");ntcBadge();
  fetch(DATA.noticeUrl,{method:"POST",credentials:"same-origin",headers:{"content-type":"application/json","x-erion":"web"},
    body:JSON.stringify({id:n.id})}).catch(()=>{})}
function openNotices(focusId){
  sheet.textContent="";sheet.append(el("div","grab"));
  const x=withIc(el("button","s-x"),"x-lg");x.type="button";x.setAttribute("aria-label","닫기");x.onclick=closeSheet;sheet.append(x);
  sheet.append(el("div","s-kind","공지"));
  const un=NTC.filter(n=>n.unread).length;
  sheet.append(el("div","s-title",!NTC.length?"공지가 없어요":un?"안 읽은 공지 "+un+"개":"과목 공지"));
  if(un>1){const w=el("div","nall");const b=el("button","s-link","모두 읽음으로");b.type="button";
    b.onclick=()=>{NTC.forEach(n=>n.unread=false);sheet.querySelectorAll(".inn.new").forEach(c=>c.classList.remove("new"));
      ntcBadge();b.remove();
      fetch(DATA.noticeUrl,{method:"POST",credentials:"same-origin",headers:{"content-type":"application/json","x-erion":"web"},
        body:JSON.stringify({all:true})}).catch(()=>{})};
    w.append(b);sheet.append(w)}
  let day="",target=null;
  NTC.forEach((n,i)=>{
    const d=ddays(n.posted_at),dl=d===0?"오늘":d===-1?"어제":shortDate(n.posted_at);
    if(dl!==day){day=dl;sheet.append(el("div","s-h",dl))}
    const card=el("div","inn notice"+(n.unread?" new":""));card.style.setProperty("--i",Math.min(i,12));
    const hd=el("button","inh");hd.type="button";hd.setAttribute("aria-expanded","false");
    hd.append(ic("megaphone"),el("span","t",n.title),el("span","w",hm(n.posted_at)));
    card.append(hd);
    const meta=[n.course];if(n.files.length)meta.push("첨부 "+n.files.length);
    card.append(el("div","nmeta",meta.join(" · ")));
    const body=el("div","nbody");body.hidden=true;
    body.append(el("div","nt",n.body||"(본문 없음)"));
    if(n.files.length){const f=el("div","nfiles");n.files.forEach(t=>f.append(el("div",null,"📎 "+t)));body.append(f)}
    if(n.url&&/^https?:\/\//.test(n.url)){const a=el("a","s-link","Canvas 에서 보기");a.append(ic("arrow-up-right"));
      a.href=n.url;a.target="_blank";a.rel="noopener";body.append(a)}
    card.append(body);
    hd.onclick=()=>{const open=body.hidden;body.hidden=!open;hd.setAttribute("aria-expanded",String(open));
      card.classList.toggle("open",open);if(open)ntcRead(n,card)};
    if(n.id===focusId)target=[hd,card];
    sheet.append(card)});
  if(NTC.length)sheet.append(el("div","note","최근 60일 · 30분마다 Canvas 에서 받아요. 여기서 읽어도 Canvas 의 읽음 표시는 그대로예요."));
  sheet.scrollTop=0;sheet.classList.remove("empty-pane");sheet.style.transform="";
  if(!wide())scrim.classList.add("on");
  sheet.classList.add("on");
  if(target){target[0].click();setTimeout(()=>target[1].scrollIntoView({block:"start",behavior:calm()?"auto":"smooth"}),60)}}
ntc.onclick=()=>openNotices();ntcBadge();

// ---------------------------------------------------------------- 연결 상태 (2026-09-26)
// 소스마다 마지막 성공. 토큰이 죽으면 옛 데이터가 조용히 남아 있을 뿐이라, 여기와 톱니 점으로 드러낸다.
const CONN=DATA.sources||[];
const srcBad=CONN.some(x=>x.source!=="push"&&x.state!=="ok");
function gearDot(){$("#gear").classList.toggle("dot",bellOff||srcBad);$("#conn").classList.toggle("dot",srcBad)}
function ago(iso){if(!iso)return"없음";const m=Math.round((Date.now()-new Date(iso).getTime())/6e4);
  return m<1?"방금":m<60?m+"분 전":m<60*24?Math.round(m/60)+"시간 전":Math.round(m/1440)+"일 전"}
const SST={ok:"정상",stale:"오래됨",err:"실패",never:"기록 없음"};
function openConn(){
  sheet.textContent="";sheet.append(el("div","grab"));
  const x=withIc(el("button","s-x"),"x-lg");x.type="button";x.setAttribute("aria-label","닫기");x.onclick=closeSheet;sheet.append(x);
  sheet.append(el("div","s-kind","설정"));
  sheet.append(el("div","s-title",srcBad?"끊긴 연결이 있어요":"모두 연결돼 있어요"));
  CONN.forEach((r,i)=>{
    const row=el("div","srow "+r.state);row.style.setProperty("--i",i);
    row.append(el("span","sdot"));
    const m=el("div","sm");
    const top=el("div","st");top.append(el("span","sn",r.name),el("span","sw",r.source==="push"?(r.last_ok?"마지막 전달 "+ago(r.last_ok):""):SST[r.state]+" · "+ago(r.last_ok)));
    m.append(top);
    const sub=r.err||r.note;if(sub)m.append(el("div","ss",sub));
    if(r.state==="stale"&&!r.err)m.append(el("div","ss","마지막 성공이 오래됐어요 — 폴러가 안 돌고 있을 수 있어요"));
    if(r.source==="gcal"&&r.state!=="ok"){const a=el("a","s-link","다시 연결");a.append(ic("arrow-up-right"));a.href=DATA.gcalUrl;m.append(a)}
    if(r.source==="push"&&bell.hidden===false&&bellOff){const b=el("button","s-link","이 기기 알림 켜기");b.type="button";b.onclick=()=>bell.click();m.append(b)}
    row.append(m);sheet.append(row)});
  sheet.append(el("div","note","Canvas 는 30분, 캘린더는 20분마다 받아요. 강의영상은 대시보드를 열 때 바로 물어봐요."));
  sheet.scrollTop=0;sheet.classList.remove("empty-pane");sheet.style.transform="";
  if(!wide())scrim.classList.add("on");
  sheet.classList.add("on")}
$("#conn").onclick=openConn;gearDot();
// 탭 줄이 화면 위에 붙었는지 — 붙었을 때만 유리판
{const tb=$("#tabs");const f=()=>tb.classList.toggle("stuck",tb.getBoundingClientRect().top<=.5&&scrollY>0);
 addEventListener("scroll",f,{passive:true});f()}
// ---------------------------------------------------------------- 살아 있는 층 · 집중
// 2026-09-25 "너무 정적이야 · 혼자 공부하면 외롭잖아". 글로 응원하지 않는다 — 시간이 흐르는 게 보이고,
// 하는 동안 곁에 뭔가 숨을 쉰다.
// 2026-09-26 집중 타이머를 서버로 옮겼다(POST /web/focus). 폰이 앱을 재우거나 새로고침해도 시간이 안 날아간다.
// "지금 하기" = 온 화면 집중판이 버튼 자리에서 퍼지며 열린다. 멈춤은 '잠깐 멈춤'이다 — 판은 그대로 있고
// 계속·완료·끝내기가 남는다(예전엔 멈춤이 판째로 없애서 버튼이 다 사라졌다). 끝내기는 기록하고 끝 / 기록 없이 취소.
const LS={get(k,d){try{const v=localStorage.getItem(k);return v?JSON.parse(v):d}catch(e){return d}},
          set(k,v){try{v==null?localStorage.removeItem(k):localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
["erion-focus","erion-spent","erion-days"].forEach(k=>LS.set(k,null));   // 기기에만 두던 옛 기록
const CAP=4*36e5;
let FS=DATA.focus||{cur:null,spent:{},today:0,now:Date.now()};
let FSKEW=FS.now-Date.now();
const snow=()=>Date.now()+FSKEW;
const isLive=id=>!!FS.cur&&FS.cur.id===id;
const paused=()=>!!FS.cur&&FS.cur.start==null;
const run=()=>{const c=FS.cur;if(!c)return 0;return Math.min(CAP,Math.max(0,c.acc+(c.start!=null?snow()-c.start:0)))};
function spent(id){return(FS.spent[id]||0)+(isLive(id)?run():0)}
function todayMs(){return(FS.today||0)+run()}
function leftTxt(ms){const s=Math.max(0,Math.floor(ms/1e3)),h=Math.floor(s/3600),m=Math.floor(s%3600/60),p=n=>String(n).padStart(2,"0");
  return(h?h+":"+p(m):m+":"+p(s%60))+" 남음"}
function clock(ms){const s=Math.floor(ms/1e3),p=n=>String(n).padStart(2,"0");
  return Math.floor(s/3600)+":"+p(Math.floor(s%3600/60))+":"+p(s%60)}
function dur(ms){const m=Math.round(ms/6e4);return m<60?m+"분":Math.floor(m/60)+"시간"+(m%60?" "+m%60+"분":"")}
function focusOp(op,b){
  const body={op:op};if(b){body.id=b.id;body.label=b.label;body.est=b.est_hours||null}
  return fetch(DATA.focusUrl,{method:"POST",credentials:"same-origin",
    headers:{"content-type":"application/json","x-erion":"web"},body:JSON.stringify(body)})
    .then(r=>r.json().catch(()=>({ok:false,error:"HTTP "+r.status})))
    .then(r=>{if(!r||!r.ok)throw new Error((r&&r.error)||"알 수 없는 오류");
      const was=FS.cur&&FS.cur.id;FS=r;FSKEW=r.now-Date.now();syncFocus(was!==(FS.cur&&FS.cur.id));return r})}
function focusPull(){
  return fetch(DATA.focusUrl,{credentials:"same-origin"}).then(r=>r.json()).then(r=>{
    if(!r||!r.ok)return;const was=FS.cur&&FS.cur.id;FS=r;FSKEW=r.now-Date.now();
    syncFocus(was!==(FS.cur&&FS.cur.id))}).catch(()=>{})}
function startFocus(b,from){
  // 판은 먼저 연다(누른 손이 기다리지 않게). 서버가 거절하면 도로 닫는다.
  const prev=FS;
  FS=Object.assign({},FS,{cur:{id:b.id,label:b.label,est:b.est_hours||null,start:snow(),acc:0}});
  try{navigator.vibrate&&navigator.vibrate(10)}catch(e){}
  syncFocus(true);openFocus(from);
  focusOp("start",b).catch(e=>{FS=prev;syncFocus(true);closeFocus();toast("못 켰어요 — "+(e.message||e),true)})}
function spentRow(dl,b){
  const ms=spent(b.id);if(!ms)return;
  dl.append(el("dt",null,"들인 시간"));
  const dd=el("dd","spent");dd.append(el("span",null,dur(ms)));
  if(b.est_hours){const pb=el("span","pb"),i=el("i");i.style.width=Math.min(100,ms/36e5/b.est_hours*100)+"%";pb.append(i);dd.append(pb)}
  dl.append(dd)}
const curBlock=()=>FS.cur&&BLOCKS.find(x=>x.id===FS.cur.id);

// 온 화면 집중판 — 주변을 걷어내고 이것 하나만 남긴다
const fz=$("#focus"),dock=$("#dock"),BIG=2*Math.PI*104,RING=2*Math.PI*17;
let fzOpen=false,wake=null;
function wakeOn(){if(!fzOpen||!FS.cur||paused()||!navigator.wakeLock||wake)return;
  navigator.wakeLock.request("screen").then(w=>{wake=w;w.addEventListener("release",()=>{wake=null})}).catch(()=>{})}
function wakeOff(){if(wake){wake.release().catch(()=>{});wake=null}}
function openFocus(from){
  if(!FS.cur)return;
  buildFocus();closeSheet();
  const r=from&&from.getBoundingClientRect?from.getBoundingClientRect():null;
  fz.style.setProperty("--ox",r?(r.left+r.width/2)+"px":"50%");
  fz.style.setProperty("--oy",r?(r.top+r.height/2)+"px":"100%");
  fz.hidden=false;fzOpen=true;document.body.classList.add("fzon");
  requestAnimationFrame(()=>requestAnimationFrame(()=>fz.classList.add("on")));
  syncDock();focusTick();wakeOn()}
function closeFocus(){
  if(!fzOpen)return;fzOpen=false;wakeOff();
  fz.classList.remove("on");document.body.classList.remove("fzon");
  setTimeout(()=>{if(!fzOpen)fz.hidden=true},calm()?0:420);
  syncDock()}
function buildFocus(){
  fz.textContent="";const c=FS.cur,b=curBlock();
  const top=el("div","fz-top");
  const down=withIc(el("button","fz-ic"),"chevron-down");down.type="button";down.setAttribute("aria-label","작게");down.onclick=closeFocus;
  const end=withIc(el("button","fz-ic"),"x-lg");end.type="button";end.setAttribute("aria-label","끝내기");end.onclick=()=>endMenu(true);
  top.append(down,el("span","fz-k",b?(KIND[b.kind]+(b.course?" · "+b.course:"")):""),end);
  const mid=el("div","fz-mid");
  mid.append(el("div","fz-title",c.label));
  if(b&&b.url&&/^https?:\/\//.test(b.url)){const a=el("a","fz-link",linkName(b));a.append(ic("arrow-up-right"));
    a.href=b.url;a.target="_blank";a.rel="noopener";mid.append(a)}
  const ring=el("div","fz-ring");
  ring.innerHTML='<svg viewBox="0 0 220 220"><circle class=bg cx=110 cy=110 r=104></circle><circle class=fg cx=110 cy=110 r=104></circle></svg>';
  const fg=ring.querySelector(".fg");fg.style.strokeDasharray=BIG;fg.style.strokeDashoffset=BIG;
  ring.append(el("i","fz-orb"),el("div","fz-clk"),el("div","fz-sub"));
  mid.append(ring);
  const bot=el("div","fz-bot");
  const pp=el("button","fz-pp");pp.type="button";pp.onclick=()=>{
    pp.disabled=true;focusOp(paused()?"resume":"pause").catch(e=>toast("못 바꿨어요 — "+(e.message||e),true)).finally(()=>{pp.disabled=false})};
  const fin=withIc(el("button","btn done fz-fin"),"check-lg","완료");fin.type="button";fin.onclick=()=>finishFocus(fin);
  bot.append(pp,fin);
  const menu=el("div","fz-menu");menu.hidden=true;
  const keep=el("button","btn","기록하고 끝내기"),drop=el("button","btn undo","기록 없이 취소"),back=el("button","btn undo","계속하기");
  keep.type=drop.type=back.type="button";
  keep.onclick=()=>endFocus("stop");drop.onclick=()=>endFocus("cancel");back.onclick=()=>endMenu(false);
  menu.append(el("div","fz-mq"),keep,drop,back);
  fz.append(el("div","fz-glow"),top,mid,bot,menu);
  paintFocus()}
function endMenu(on){const m=fz.querySelector(".fz-menu");if(!m)return;
  if(on){m.querySelector(".fz-mq").textContent="지금까지 "+dur(run())+" · 어떻게 끝낼까요?"}
  m.hidden=!on;fz.classList.toggle("asking",on)}
function endFocus(op){
  const ms=run();
  focusOp(op).then(()=>{closeFocus();toast(op==="stop"?dur(ms)+" 기록했어요":"취소했어요 — 기록 안 함",false,2600)})
    .catch(e=>toast("못 끝냈어요 — "+(e.message||e),true))}
function finishFocus(btn){
  const b=curBlock();if(!b){endFocus("stop");return}
  btn.disabled=true;
  postDone(b,true).then(r=>{
    if(!r||!r.ok)throw new Error((r&&r.error)||"알 수 없는 오류");
    burst(btn);const tot=spent(b.id);
    return focusOp("stop").catch(()=>{}).then(()=>{
      setTimeout(()=>{closeFocus();
        const rs=document.querySelectorAll('.row[data-id="'+CSS.escape(b.id)+'"]');rs.forEach(x=>x.classList.add("leaving"));
        setTimeout(()=>{applyDone(b,true);toast("✓ "+b.label+" · "+dur(tot),false,2600)},rs.length&&!calm()?330:0)},calm()?0:500)})})
    .catch(e=>{btn.disabled=false;toast("못 적었어요 — "+(e.message||e),true)})}
function paintFocus(){
  if(fz.hidden||!FS.cur)return;
  const ps=paused();fz.classList.toggle("paused",ps);
  const pp=fz.querySelector(".fz-pp");if(pp){withIc(pp,ps?"play-fill":"pause-fill");pp.setAttribute("aria-label",ps?"계속":"잠깐 멈춤")}
  focusTick();if(ps)wakeOff();else wakeOn()}
function focusTick(){
  if(fz.hidden||!FS.cur)return;
  const ms=run(),all=spent(FS.cur.id),est=FS.cur.est,fg=fz.querySelector(".fz-ring .fg");
  fz.querySelector(".fz-clk").textContent=clock(ms);
  const sub=fz.querySelector(".fz-sub");
  if(paused())sub.textContent="멈춤";
  else if(est){const f=all/36e5/est;sub.textContent="예상 "+hrs(est)+" 중 "+Math.round(f*100)+"%"}
  else sub.textContent=all>ms?"합계 "+dur(all):"";
  const f=est?all/36e5/est:ms/36e5%1;
  fg.style.strokeDashoffset=BIG*(1-Math.min(1,f));fg.classList.toggle("over",!!est&&f>1)}

// 작게 접으면 바닥에 작은 판 — 누르면 다시 온 화면. 버튼은 멈춤/계속 하나뿐이다.
function syncDock(){
  dock.textContent="";
  if(!FS.cur||fzOpen){dock.hidden=true;return}
  const o=el("div","orb");
  o.innerHTML='<svg viewBox="0 0 40 40"><circle class=bg cx=20 cy=20 r=17></circle><circle class=fg cx=20 cy=20 r=17></circle></svg>';
  o.append(el("i"));
  const fg=o.querySelector(".fg");fg.style.strokeDasharray=RING;fg.style.strokeDashoffset=RING;
  const tx=el("div","dt");tx.append(el("div","dl",FS.cur.label),el("div","ds"));
  const pp=withIc(el("button","db"),paused()?"play-fill":"pause-fill");pp.type="button";pp.setAttribute("aria-label",paused()?"계속":"잠깐 멈춤");
  pp.onclick=e=>{e.stopPropagation();pp.disabled=true;
    focusOp(paused()?"resume":"pause").catch(x=>toast("못 바꿨어요 — "+(x.message||x),true)).finally(()=>{pp.disabled=false})};
  dock.onclick=()=>openFocus(dock);
  dock.append(o,tx,pp);dock.hidden=false;dock.classList.toggle("paused",paused());
  dockTick()}
function dockTick(){
  if(!FS.cur||dock.hidden)return;
  const ms=run(),all=spent(FS.cur.id),ds=dock.querySelector(".ds"),fg=dock.querySelector(".fg");
  ds.textContent="";ds.append(el("b",null,clock(ms)));
  if(paused())ds.append(" · 멈춤");
  else if(FS.cur.est)ds.append(" · 예상 "+hrs(FS.cur.est)+" 중 "+Math.round(all/36e5/FS.cur.est*100)+"%");
  const f=FS.cur.est?all/36e5/FS.cur.est:ms/36e5%1;
  fg.style.strokeDashoffset=RING*(1-Math.min(1,f));fg.classList.toggle("over",!!FS.cur.est&&f>1)}
function syncFocus(rerender){
  document.body.classList.toggle("focusing",!!FS.cur);
  head();
  if(!FS.cur&&fzOpen)closeFocus();
  if(fzOpen)paintFocus();
  syncDock();
  if(rerender)render()}

// 하늘 띠 — 2026-09-26 "시간 바가 애매해. 두께를 주고 밤하늘-낮 전환으로, 비 오면 비 내리고 눈 오면 눈 내리고".
// 0–24시 하루 전체가 하늘이다. 해 뜨고 지는 시각(Open-Meteo)으로 밤·새벽·낮·노을을 칠하고, 지금 자리에
// 해(낮엔 높이가 고도를 따른다)나 달이 떠 있다. 지난 시간은 어둡게 덮인다. 비·눈·흐림은 **예보된 그 시간대에만**
// 내린다 — 저녁에 비 소식이면 18–21시 칸에만 비가 온다. 수업·일정은 바닥의 가는 막대.
// 빈 시간 계산은 여전히 계획의 창(09–24시) 기준이다.
const WS=9,WE=24,hnum=s=>{const p=s.split(":");return+p[0]+ +p[1]/60};
function nowH(){const k=K(NOW);return k.getUTCHours()+k.getUTCMinutes()/60+k.getUTCSeconds()/3600}
const px=h=>Math.min(24,Math.max(0,h))/24*100;
const WX=DATA.weather,SR=WX?WX.sunrise:6.5,SS=WX?WX.sunset:18.5;
// WMO 날씨 코드 → 모양
function wxKind(c){return c==null?null:c>=95?"storm":c>=85?"snow":c>=80?"rain":c>=71?"snow":c>=51?"rain":c>=45?"fog":c>=3?"cloud":c>=2?"part":null}
const WXNAME={storm:"뇌우",snow:"눈",rain:"비",fog:"안개",cloud:"흐림",part:"구름 조금"};
function skyGrad(){
  const st=[[0,"#0b1030"],[SR-1.3,"#0f1638"],[SR-.4,"#3d3266"],[SR+.25,"#f0a878"],[SR+1.4,"#8fc4ea"],
    [(SR+SS)/2,"#6aaee6"],[SS-1.4,"#88b8e2"],[SS-.1,"#f08a5c"],[SS+.7,"#4a3b7c"],[SS+1.6,"#111838"],[24,"#0b1030"]];
  return"linear-gradient(90deg,"+st.map(x=>x[1]+" "+px(x[0]).toFixed(2)+"%").join(",")+")"}
let BAND=null;
function bandBuild(){
  const bx=$("#band");bx.textContent="";BAND=null;
  const d0=DATA.plan&&DATA.plan.days[0];if(!d0||!d0.today)return;
  const segs=d0.busy.filter(x=>x.kind==="class"||x.kind==="event").map(x=>({s:hnum(x.s),e:hnum(x.e),label:x.label,kind:x.kind}));
  const sky=el("div","sky");sky.style.background=skyGrad();
  const fx=el("div","skyfx");sky.append(fx);
  const moving=!calm(),H=WX&&WX.hourly||[];
  const night=h=>h<SR-.8||h>SS+1;
  // 별 — 밤이고 구름이 적은 시간에만
  for(let i=0;i<34;i++){const h=Math.random()*24,c=H[Math.floor(h)];if(!night(h)||(c&&c.cloud>70))continue;
    const s=el("i","star");s.style.left=px(h)+"%";s.style.top=(6+Math.random()*60)+"%";
    s.style.animationDelay=(-Math.random()*4)+"s";if(Math.random()<.3)s.classList.add("big");fx.append(s)}
  // 시간마다: 구름 덮개 + 비/눈 알갱이
  H.forEach((w,h)=>{const k=wxKind(w.code);if(!k)return;
    const cloud=el("div","cld "+k);cloud.style.left=px(h-.9)+"%";cloud.style.width=(100/24*2.8)+"%";
    cloud.style.setProperty("--cl",Math.max(.25,(w.cloud||0)/100));fx.append(cloud);
    const cell=el("div","wxc "+k);cell.style.left=px(h)+"%";cell.style.width=(100/24)+"%";fx.append(cell);
    if(k==="rain"||k==="storm"||k==="snow"){
      const n=k==="snow"?5:7;
      for(let i=0;i<n;i++){const d=el("i",k==="snow"?"flake":"drop");
        d.style.left=(Math.random()*100)+"%";d.style.animationDelay=(-Math.random()*(k==="snow"?4:1))+"s";
        d.style.animationDuration=(k==="snow"?3+Math.random()*2:.7+Math.random()*.4)+"s";cell.append(d)}}
    if(k==="storm"&&moving){const f=el("i","flash");f.style.animationDelay=(-Math.random()*7)+"s";cell.append(f)}});
  segs.forEach(x=>{x.n=el("div","seg "+x.kind);x.n.style.left=px(x.s)+"%";x.n.style.width=(px(x.e)-px(x.s))+"%";
    x.n.title=x.label+" "+x.s.toFixed(0)+"–"+x.e.toFixed(0)+"시";sky.append(x.n)});
  const gone=el("div","gone"),now=el("div","now"),body=el("i","cel");now.append(body);sky.append(gone,now);
  const tks=el("div","tks");[["0",0],["6",6],["12",12],["18",18],["24",24]].forEach(([t,h])=>{const s=el("span",null,t);s.style.left=px(h)+"%";tks.append(s)});
  // 해 뜨고 지는 자리 표시
  const meta=el("div","meta"),l=el("span"),r=el("span");meta.append(l,r);
  bx.append(sky,tks,meta);BAND={segs:segs,gone:gone,now:now,body:body,l:l,r:r,sky:sky};bandTick()}
function bandTick(){
  if(!BAND)return;
  const h=nowH(),{segs,l,r,body}=BAND;
  BAND.gone.style.width=px(h)+"%";BAND.now.style.left=px(h)+"%";
  // 낮엔 해 — 고도를 따라 오르내린다. 밤엔 달이 위에 떠 있다.
  const day=h>=SR&&h<=SS;body.className="cel "+(day?"sunb":"moon");
  body.style.top=day?(10+(1-Math.sin(Math.PI*(h-SR)/(SS-SR)))*48)+"%":"16%";
  segs.forEach(x=>x.n.classList.toggle("past",x.e<=h));
  const hd=x=>dur(x*36e5);
  l.textContent="";
  const cur=segs.find(x=>x.s<=h&&h<x.e),nx=segs.find(x=>x.s>h);
  if(cur)l.append("지금 · ",el("b",null,cur.label)," · "+hd(cur.e-h)+" 남음");
  else if(nx)l.append("다음 ",el("b",null,nx.label)," · "+hd(nx.s-h)+" 뒤");
  else l.append("남은 일정 없음");
  const free=freeToday()||0,done=DID.filter(b=>b.done_at&&ddays(b.done_at)===0).length,fm=todayMs();
  r.textContent="";
  if(WX){const w=WX.hourly&&WX.hourly[Math.floor(h)],k=wxKind(w?w.code:WX.code);
    r.append((k?WXNAME[k]:(day?"맑음":"맑은 밤"))+" "+Math.round(WX.temp)+"° · ")}
  if(done)r.append("✓"+done+" · ");
  if(fm>=6e4)r.append("집중 "+dur(fm)+" · ");
  r.append("빈 ",el("b",null,hd(free)))}

// 시간대 빛 · 지난 마감이면 주황
function mood(){
  const k=K(NOW).getUTCHours(),v=k>=5&&k<11?"--mark":k<17&&k>=11?"--mark2":k>=17&&k<21?"--hot":"--lec";
  document.documentElement.style.setProperty("--tod","var("+v+")");
  document.body.classList.toggle("hot",counts().over.length>0)}

// 매초 한 번. 숨겨져 있으면 쉰다. 분이 바뀌면 머리말·빛, 날이 바뀌면 새 계획을 받으러 다시 연다.
let lastMin=-1,overN=-1,hidAt=0;
const idle=()=>!drag&&!moving&&!fzOpen&&(wide()?sheet.classList.contains("empty-pane"):!sheet.classList.contains("on"));
function refresh(){try{sessionStorage.setItem("erion-scroll",String(scrollY))}catch(e){}location.reload()}
function tick(){
  if(document.hidden)return;
  NOW=new Date(Date.now()+SKEW);
  document.querySelectorAll("[data-left]").forEach(n=>{
    const ms=new Date(n.dataset.left)-NOW;
    if(ms>0)n.textContent=leftTxt(ms);else{n.removeAttribute("data-left");n.textContent="지남"}});
  bandTick();dockTick();focusTick();
  const m=Math.floor(NOW/6e4);
  if(m===lastMin)return;lastMin=m;
  if(day0(NOW)!==day0(T0)&&idle()){refresh();return}
  head();mood();
  const o=counts().over.length;if(overN>=0&&o!==overN){renderTabs();render()}overN=o}
document.addEventListener("visibilitychange",()=>{
  if(document.hidden){hidAt=Date.now();return}
  // 15분 넘게 딴 데 있다 오면 폴러가 새로 가져온 걸 받으러 다시 연다
  if(hidAt&&Date.now()-hidAt>15*6e4&&idle()){refresh();return}
  // 돌아오면 타이머 상태를 서버에서 다시 받는다(다른 기기에서 멈췄을 수 있다). 화면 잠금도 다시 건다.
  focusPull();wakeOn();tick()});

// 알림을 누르고 들어오면 ?tab= / ?open=<id> 가 붙어 온다.
{const q=new URLSearchParams(location.search),t=q.get("tab"),o=q.get("open"),nt=q.get("notice");
 if(q.get("inbox"))setTimeout(openInbox,0);
 if(nt)setTimeout(()=>openNotices(nt),0);
 if(t&&TABS.some(x=>x[0]===t))tab=t;
 if(t||o||nt||q.get("inbox"))history.replaceState(null,"",location.pathname);
 if(o)setTimeout(()=>{const b=BLOCKS.find(x=>x.id===o);if(b)openSheet(b)},0)}
head();renderTabs();render(true);paneIdle();bandBuild();
syncFocus(false);
tick();setInterval(tick,1000);
try{const y=sessionStorage.getItem("erion-scroll");if(y!=null){sessionStorage.removeItem("erion-scroll");scrollTo(0,+y)}}catch(e){}
addEventListener("resize",()=>{if(wide()&&!sheet.classList.contains("on"))paneIdle()});
})();
"""

async def static(request: Request) -> Response:
    """app/static 의 아이콘. 로그인 없이 준다 — 홈화면 추가·탭 아이콘은 쿠키 없이 요청된다."""
    name = request.path_params["name"]
    if name not in _STATIC_OK:
        return Response("not found", status_code=404, media_type="text/plain")
    return FileResponse(_STATIC_DIR / name, media_type="image/png",
                        headers={"cache-control": "public, max-age=604800"})


async def manifest(request: Request) -> Response:
    """홈화면 앱 정의. start_url·scope 를 /erion/ 로 가둔다 — 여백(/yeobaek/)과 안 섞이게."""
    ic = lambda n, s, p=None: {"src": f"{PREFIX}/static/{n}", "sizes": s, "type": "image/png",
                               **({"purpose": p} if p else {})}
    return JSONResponse({
        "name": "erion", "short_name": "erion", "lang": "ko",
        "start_url": f"{PREFIX}/", "scope": f"{PREFIX}/", "display": "standalone",
        "background_color": "#201F1D", "theme_color": "#201F1D",
        "icons": [ic("icon-192.png", "192x192"), ic("icon-512.png", "512x512"),
                  ic("icon-512-maskable.png", "512x512", "maskable")],
    }, media_type="application/manifest+json")


ROUTES = [
    ("/static/{name}", static, ["GET"]),
    ("/manifest.webmanifest", manifest, ["GET"]),
    ("/", root, ["GET"]),
    ("/web", page, ["GET"]),
    ("/web/", page, ["GET"]),
    ("/web/login", login, ["POST"]),
    ("/web/done", done, ["POST"]),
    ("/web/plan", plan_move, ["POST"]),
    ("/web/notice", notice_read, ["POST"]),
    ("/web/est", est, ["POST"]),
    ("/web/focus", focus, ["GET", "POST"]),
    ("/web/push", push_sub, ["POST"]),
    ("/web/push/ack", push_ack, ["POST"]),
    ("/sw.js", sw, ["GET"]),
    ("/web/logout", logout, ["GET"]),
]
