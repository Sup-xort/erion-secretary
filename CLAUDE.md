# erion 볼트 (서버)

개인맞춤 AI 를 위한 **원격 MCP 서버 + OAuth 인가 서버**. claude.ai 가 커스텀 커넥터로
붙어서 마감·일정·문서를 좁은 질의로 읽는다.

- 공개 주소: `https://sqhsxp.duckdns.org/erion/mcp`
- 코드 `app/` · 데이터 `data/` · 설정 `.env`(chmod 600) · venv `.venv`
- systemd `erion-vault` → `127.0.0.1:8788`

## 문서는 여기 없다 → `memory/`

**`memory/` 가 이 프로젝트의 기억소다.** 2026-09-26 부터 private 저장소
`Sup-xort/vault-for-erion` 의 clone 이다 (DB 텍스트 덤프 `db/erion.sql` 도 같이 산다).
이 코드 저장소는 `memory/` 를 무시한다 — 두 저장소는 따로 커밋한다.

진입점은 **`memory/CLAUDE.md`** — 어떤 질문에 어느 파일을 열면 되는지가 거기 있다.
자주 쓰는 것만 적으면:

- 서버에 뭐가 돌고 있나 · 경로 검증 매트릭스 · 되돌리는 법 → `memory/deploy.md`
- 지금 상태와 다음 할 일 → `memory/status.md`
- 이미 버린 것 (다시 제안하지 않도록) → `memory/dead-ends.md`
- 왜 이렇게 설계했나 → `memory/spec-vault.md`

**같은 내용을 이 파일에 옮겨 적지 마라.** 예전에 그렇게 해서 문서가 서로 어긋났다.

`scripts/vault-backup.sh` 가 DB 를 덤프하고 `memory/` 를 커밋·푸시한다. 문서를 고쳤으면
그걸 한 번 돌리면 된다.

## 코드를 고치기 전에

**서버에서 직접 고치고 재기동한다. 여기 `app/` 이 유일한 원본이다.**
예전엔 노트북 `vault/` 가 원본이고 `app/` 은 그 배포 산출물이었지만, 2026-09-11 에
노트북을 은퇴시켰다. 노트북에 반영하는 절차는 이제 없다.

**이 디렉터리는 git 이다** (2026-09-26, `Sup-xort/erion-secretary`, **public**).
그래서 **비밀값·개인 데이터는 절대 커밋하지 마라** — `.env`·`data/`·`memory/` 는
`.gitignore` 로 빠져 있다. 되돌리기는 git 으로 한다. `*.bak-*` 은 더 뜨지 않아도 된다.

경로·라우팅을 건드릴 거면 `memory/deploy.md` 의 검증 매트릭스를 먼저 보고 나서 손대라.
