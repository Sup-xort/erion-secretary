#!/usr/bin/env python3
"""`deadline` 소요시간 추정 배치 — est_hours · prep_note.

폴러 **옆의 1회 전처리**다. 추론 루프가 아니다 (decisions.md "est_hours 와 전달 계층").
Canvas 폴러가 끝난 직후에 돈다 (erion-canvas-poll.service 의 ExecStartPost).

하는 일: 과제마다 est_hours · prep_note · label(대시보드에 띄울 정제된 이름) 을 한 번에 받는다.
대상: 미제출 · 마감 전 · 아직 추정 안 한 과제.
      + 설명 없이 제목만 보고 추정했는데(est_basis=title) 그 뒤 설명이 채워진 과제 — 1회 재추정.
      그 외엔 같은 과제를 두 번 부르지 않는다.

모델: Gemini(flash-lite) → 실패하면 NIM(nemotron). 키는 여백과 같은 것 (.env).
NIM 은 모델이 자주 단종된다 — gpt-oss-120b 는 2026-09-03 에 410 이 됐다. 그래서 2순위다.
추정은 틀린다. 챗에서 보정하면(est_adjust) est_feedback 에 쌓이고, 여기서 최근 보정을
프롬프트에 넣어 다음 추정이 따라오게 한다 — 이게 추정의 기억이다.
사용자가 직접 고친 추정(est_basis=user)은 다시 건드리지 않는다.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
VAULT_DB = Path(os.environ.get(
    "ERION_DATA", "/home/ubuntu/projects/erion/data")) / "erion.db"
NIM_KEY = os.environ.get("NIM_API_KEY", "")
NIM_BASE = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = os.environ.get("ERION_EST_NIM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("ERION_EST_GEMINI_MODEL", "gemini-3.1-flash-lite")

# 한 회차 상한. 30분마다 도니까 밀린 건 다음 회차가 잇는다 — 서비스 타임아웃 안에 끝내는 게 우선.
PER_RUN = 12
GAP_SEC = 2.0          # 호출 사이 간격. Gemini 무료 티어 분당 한도에 안 걸리게.
HOURS_MAX = 40.0       # 이보다 크면 모델이 헛소리한 것으로 본다

SYSTEM = """너는 한국 대학생(성균관대, 공대)의 과제 소요시간을 추정한다.
주어진 과제 하나에 대해, 평범하게 성실한 학생이 **착수부터 제출까지 실제로 손을 쓰는 시간**을
시간 단위로 추정하라. 마감까지 남은 기간이 아니라 순수 작업 시간이다.

참고:
- submission_types: online_upload=파일 제출, online_text_entry=글 입력, discussion_topic=게시판 토론,
  online_quiz=퀴즈, external_tool/none=대개 강의영상 시청·출석.
- points 는 중요도 신호다. 소요시간과 비례하지는 않는다.
- 설명이 없으면 제목·과목·같은 과목의 비슷한 과제를 보고 추정하라.
- "사용자 보정 기록"이 있으면 그게 이 학생의 실제 속도다. 비슷한 과제엔 그 경향을 따르라.

label 은 대시보드에 띄울 짧은 이름이다. 과목을 모르는 사람도 뭘 하는 건지 알게,
한국어로 20자 안팎. **과목명·배점·마감일은 넣지 마라** — 대시보드에 옆 칸으로 따로 보인다.
원제목에 있는 번호(주차·회차)는 살리고, **없는 번호를 지어내지 마라.**
예) "Homework Week 3" → "3주차 숙제", "HW1" → "숙제 1",
    "3주 토론-자연 상태에서 발생하는 변이" → "3주차 토론: 자연 상태의 변이"

JSON 한 개만 출력하라. 다른 말은 쓰지 마라.
{"label": "…", "est_hours": 숫자(0.25 단위), "prep_note": "착수 전에 준비할 것·주의할 점 한 줄, 한국어 60자 이내"}"""
FEEDBACK_N = 15        # 프롬프트에 넣을 최근 보정 기록 수


def _now() -> datetime:
    return datetime.now(KST)


def _feedback(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT label, course, sub_types, old_hours, new_hours, actual, reason FROM est_feedback "
        "ORDER BY seq DESC LIMIT ?", [FEEDBACK_N]).fetchall()
    if not rows:
        return ""
    out = ["\n사용자 보정 기록 (최근순):"]
    for f in rows:
        old = "?" if f["old_hours"] is None else f"{f['old_hours']:g}h"
        what = (f"추정 {old}, 실제 {f['new_hours']:g}h 걸림" if f["actual"]
                else f"추정 {old} → 사용자가 {f['new_hours']:g}h 로 고침")
        out.append(f"- [{f['course']}] {f['label']} ({f['sub_types'] or '-'}): {what}"
                   + (f" — {f['reason']}" if f["reason"] else ""))
    return "\n".join(out)


def _prompt(r: sqlite3.Row, siblings: list[sqlite3.Row], feedback: str = "") -> str:
    lines = [
        f"과목: {r['course']}",
        f"제목: {r['title']}",
        f"배점: {r['points']}",
        f"제출 형식: {r['sub_types'] or '(모름)'}",
        f"마감: {r['due_at']}",
        "설명:",
        r["description"] or "(없음)",
    ]
    if not r["description"] and siblings:
        lines.append("\n같은 과목의 설명 있는 다른 과제 (참고용):")
        for s in siblings:
            lines.append(f"- {s['title']} [{s['sub_types']}]: {(s['description'] or '')[:300]}")
    if feedback:
        lines.append(feedback)
    return "\n".join(lines)


def _post(url: str, body: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def _nim(user: str) -> str:
    if not NIM_KEY:
        raise RuntimeError("NIM 키 없음")
    d = _post(f"{NIM_BASE}/chat/completions", {
        "model": NIM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        # 추론형 모델은 추론 토큰을 먼저 쓴다. 너무 조이면 content 가 비어서 온다.
        "max_tokens": 2000,
        "temperature": 0.2,
    }, {"Authorization": f"Bearer {NIM_KEY}"})
    return d["choices"][0]["message"].get("content") or ""


def _gemini(user: str) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini 키 없음")
    d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent", {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400,
                             "responseMimeType": "application/json"},
    }, {"x-goog-api-key": GEMINI_KEY})
    return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])


def _parse(text: str) -> tuple[float, str, str]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 없음: {text[:120]!r}")
    obj = json.loads(m.group(0))
    h = float(obj["est_hours"])
    if not (0 < h <= HOURS_MAX):
        raise ValueError(f"est_hours 범위 밖: {h}")
    h = max(0.25, round(h * 4) / 4)
    note = str(obj.get("prep_note") or "").strip().replace("\n", " ")[:120]
    label = str(obj.get("label") or "").strip().replace("\n", " ")[:40]
    return h, note, label


def estimate(user: str) -> tuple[float, str, str, str]:
    """(est_hours, prep_note, label, model). 둘 다 실패하면 RuntimeError."""
    errs = []
    for name, fn in ((GEMINI_MODEL, _gemini), (NIM_MODEL, _nim)):
        try:
            h, note, label = _parse(fn(user))
            return h, note, label, name
        except Exception as e:  # 모델 하나가 죽어도 다음으로 넘어간다
            errs.append(f"{name}: {e}")
    raise RuntimeError(" / ".join(errs))


def main() -> int:
    ap = argparse.ArgumentParser(description="deadline 소요시간 추정 배치")
    ap.add_argument("--limit", type=int, default=PER_RUN, help=f"이번 회차 최대 건수 (기본 {PER_RUN})")
    ap.add_argument("--dry", action="store_true", help="모델은 부르되 DB 에 안 쓴다")
    args = ap.parse_args()

    conn = sqlite3.connect(VAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        todo = conn.execute(
            "SELECT * FROM deadline WHERE submitted = 0 AND due_at >= ? "
            # 사용자가 대시보드에서 완료로 찍은 건 추정할 이유가 없다 (2026-09-20)
            "AND id NOT IN (SELECT id FROM done_mark) "
            "AND (est_model IS NULL OR (est_basis = 'title' AND description IS NOT NULL)) "
            "ORDER BY due_at ASC LIMIT ?",
            [_now().isoformat(timespec="seconds"), args.limit]).fetchall()
        if not todo:
            print("[est] 추정할 과제 없음")
            return 0

        fb = _feedback(conn)
        ok = fail = 0
        for i, r in enumerate(todo):
            if i:
                time.sleep(GAP_SEC)
            sibs = [] if r["description"] else conn.execute(
                "SELECT title, sub_types, description FROM deadline "
                "WHERE course = ? AND id != ? AND description IS NOT NULL "
                "ORDER BY due_at DESC LIMIT 2", [r["course"], r["id"]]).fetchall()
            try:
                h, note, label, model = estimate(_prompt(r, sibs, fb))
            except RuntimeError as e:
                fail += 1
                print(f"[est] 실패 {r['id']} {r['title'][:30]}: {e}", file=sys.stderr)
                continue
            basis = "desc" if r["description"] else "title"
            # 근거 표시는 코드가 붙인다. 프롬프트로 시키면 설명이 있어도 붙여버린다 (09-18 실측).
            note = re.sub(r"^설명\s*없음\s*[—-]\s*", "", note)
            if basis == "title":
                note = "설명 없음 — " + note
            ok += 1
            print(f"  {h:5.2f}h [{basis:5}] {r['course'][:12]:12} {r['title'][:24]:24} → {label[:20]:20} "
                  f"({model.split('/')[-1]}) | {note}")
            if not args.dry:
                conn.execute(
                    # label 은 비어 있을 때만 — 챗에서 고친 이름을 재추정이 덮으면 안 된다.
                    "UPDATE deadline SET est_hours=?, prep_note=?, est_model=?, est_basis=?, est_at=?, "
                    "label=COALESCE(label, NULLIF(?, '')) WHERE id=?",
                    [h, note, model, basis, _now().isoformat(timespec="seconds"), label, r["id"]])
                conn.commit()  # 한 건씩 — 중간에 죽어도 앞의 호출이 버려지지 않게
        print(f"[est] 추정 {ok} · 실패 {fail}" + (" (--dry 라 안 썼다)" if args.dry else ""))
        return 1 if ok == 0 else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
