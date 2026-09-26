#!/usr/bin/env bash
# memory/ (+ DB 텍스트 덤프) 를 private 저장소 vault-for-erion 에 올린다.
# erion-vault-backup.timer 가 하루 한 번 부른다. 손으로 불러도 된다.
# 바뀐 게 없으면 커밋하지 않는다.
set -euo pipefail
ROOT=/home/ubuntu/projects/erion
MEM=$ROOT/memory

mkdir -p "$MEM/db"
# 돌고 있는 DB 를 backup API 로 떠서 텍스트로 덤프 — 파일 복사는 쓰는 중이면 깨질 수 있다
"$ROOT/.venv/bin/python" - "$ROOT/data/erion.db" "$MEM/db/erion.sql" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
mem = sqlite3.connect(":memory:")
src.backup(mem)
with open(sys.argv[2], "w") as f:
    for line in mem.iterdump():
        f.write(line + "\n")
PY
cp "$ROOT/data/timetable.json" "$MEM/db/timetable.json"

cd "$MEM"
git add -A
if git diff --cached --quiet; then
  echo "vault-backup: 바뀐 것 없음"
  exit 0
fi
git commit -q -m "snapshot $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M KST')"
git push -q origin main
echo "vault-backup: $(git log -1 --format='%h %s')"
