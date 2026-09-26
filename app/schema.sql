-- erion 볼트 스키마 (SPEC §2). 구조화 레코드만. 문서 본문은 docs/ 파일에 있다.
-- 이 4테이블은 싼 층이 쓰고 Claude 는 읽는다. raw 90일 롤링 대상이 아니다(결과물).

CREATE TABLE IF NOT EXISTS deadline (
  id          TEXT PRIMARY KEY,   -- canvas:<course_id>:<assignment_id>
  title       TEXT NOT NULL,
  course      TEXT,
  due_at      TEXT,               -- ISO8601 +09:00
  due_approx  INTEGER DEFAULT 0,  -- 1 = ICS 종일 근사(23:59). 출력에 ~ 를 붙인다
  points      REAL,               -- Canvas points_possible = 중요도 신호
  submitted   INTEGER DEFAULT 0,
  updated_at  TEXT NOT NULL,
  -- 2026-09-18 추가분. 기존 DB 엔 db.py 의 _migrate 가 ALTER 로 붙인다
  -- (CREATE IF NOT EXISTS 는 이미 있는 테이블의 칼럼을 안 늘린다).
  description TEXT,               -- Canvas 과제 설명, HTML 벗긴 텍스트 (폴러가 채움)
  sub_types   TEXT,               -- Canvas submission_types, 쉼표 (online_upload,online_quiz …)
  url         TEXT,               -- Canvas html_url. 대시보드 블록의 링크
  label       TEXT,               -- 정제된 표시 이름 (estimate.py 가 채움, 챗에서 고칠 수 있음)
  est_hours   REAL,               -- 소요시간 추정 (poller/estimate.py, 과제당 1회)
  prep_note   TEXT,               -- 착수 전 준비물 한 줄
  est_model   TEXT,               -- 추정한 모델. NULL = 아직 추정 안 함
  est_basis   TEXT,               -- desc = 설명 보고 추정 / title = 설명 없어 제목만. title 이면 설명 생길 때 1회 재추정
  est_at      TEXT,
  actual_hours REAL               -- 실제 걸린 시간. 추정과 대조용 — 쓰는 경로는 아직 없다
);

-- 대시보드 블록 중 Canvas 과제가 아닌 것 — 할 일 · 영상. 챗의 Claude 가 item_save 로 만든다.
-- 과제는 deadline 에 있다. 대시보드는 둘을 합쳐 보여준다.
CREATE TABLE IF NOT EXISTS item (
  id          TEXT PRIMARY KEY,   -- item:<hex8>
  kind        TEXT NOT NULL,      -- task | video
  label       TEXT NOT NULL,
  course      TEXT,
  due_at      TEXT,               -- ISO8601 +09:00, 없어도 된다. **밖에서 정해진 진짜 마감만**
  est_hours   REAL,
  prep_note   TEXT,
  description TEXT,               -- Claude 가 정리한 내용
  url         TEXT,
  status      TEXT NOT NULL DEFAULT 'todo',  -- todo | done
  source      TEXT NOT NULL,      -- claude | web
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  -- 2026-09-25 추가 (db.py _migrate). 사용자가 "10/4 까지 볼게" 처럼 스스로 정한 날.
  -- due_at 과 섞으면 대시보드가 자기 목표를 진짜 마감처럼 D-n·지난 마감·푸시로 다룬다.
  -- 계획(planner)은 이 날까지 끝내도록 배치하지만, 넘겨도 "마감 지남" 이 아니다.
  target_at   TEXT
);

-- 사람이 대시보드에서 직접 찍은 완료 표시 (2026-09-20).
-- Canvas 과제(deadline)와 강의영상(LearningX)은 원본이 밖에 있다 — 폴러가 30분마다 덮어쓰고,
-- 강의영상은 아예 DB 에 없다(실시간 조회). 그래서 그 행에 못 적고 여기 따로 적는다.
-- **Canvas 의 submitted 와 무관하다.** 손으로 찍으면 Canvas 가 뭐라 하든 대시보드에서 내려간다.
-- item(할 일·영상)은 자기 status 칸이 원본이라 여기 안 들어온다 — tools.mark_done 이 갈라 보낸다.
CREATE TABLE IF NOT EXISTS done_mark (
  id          TEXT PRIMARY KEY,   -- deadline.id (canvas:*) | lx:<url>
  kind        TEXT NOT NULL,      -- assignment | lecture
  label       TEXT,               -- 찍을 때의 이름. 원본이 목록에서 빠져도 되돌릴 수 있게 남긴다
  course      TEXT,
  due_at      TEXT,
  at          TEXT NOT NULL       -- 찍은 시각 ISO8601 +09:00
);

-- 추정 보정 기록 = 추정의 기억. 챗에서 "이건 3시간 걸려" 하면 한 줄 쌓이고,
-- estimate.py 가 다음 추정 때 최근 기록을 프롬프트에 넣는다.
CREATE TABLE IF NOT EXISTS est_feedback (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  target      TEXT NOT NULL,      -- deadline.id | item.id
  label       TEXT,
  course      TEXT,
  kind        TEXT,               -- assignment | task | video
  sub_types   TEXT,
  old_hours   REAL,
  new_hours   REAL NOT NULL,
  actual      INTEGER NOT NULL DEFAULT 0,  -- 1 = 실제 걸린 시간 / 0 = 추정 수정
  reason      TEXT,
  at          TEXT NOT NULL
);

-- 이번 주 계획의 손질 (2026-09-23). 계획 자체는 저장하지 않는다 — 열 때마다 planner.py 가
-- 새로 짠다. 여기엔 사람이(챗의 Claude 를 통해) 고친 것만 쌓인다. 짜는 쪽이 이걸 먼저 따른다.
-- plan_pin: "물리 숙제는 목요일에 할게" → 그 날에 박는다. hours NULL = 남은 양 전부.
CREATE TABLE IF NOT EXISTS plan_pin (
  target      TEXT NOT NULL,      -- deadline.id | item.id | lx:<url>
  day         TEXT NOT NULL,      -- YYYY-MM-DD (KST)
  hours       REAL,
  label       TEXT,               -- 박을 때의 이름. 대조용
  note        TEXT,               -- 사용자가 말한 이유
  at          TEXT NOT NULL,
  PRIMARY KEY (target, day)
);
-- plan_day: "금요일엔 본가 가서 못 해" → 그 날 가용시간을 직접 정한다 (0 = 쉰다).
CREATE TABLE IF NOT EXISTS plan_day (
  day         TEXT PRIMARY KEY,   -- YYYY-MM-DD (KST)
  hours       REAL NOT NULL,
  note        TEXT,
  at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block (
  id          TEXT PRIMARY KEY,   -- gcal:<event_id> | phone:<uuid>
  title       TEXT NOT NULL,
  start_at    TEXT NOT NULL,
  end_at      TEXT,
  location    TEXT,
  source      TEXT NOT NULL,      -- gcal | phone_shot | phone_notif
  confidence  REAL,               -- 폰 추출이면 분류기 점수. gcal 이면 NULL
  status      TEXT NOT NULL,      -- confirmed | candidate | cancelled
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_agg (
  month       TEXT NOT NULL,      -- YYYY-MM
  category    TEXT NOT NULL,
  amount      INTEGER NOT NULL,
  txn_count   INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (month, category)
);

CREATE TABLE IF NOT EXISTS doc (
  id           TEXT PRIMARY KEY,  -- docs/ 아래 경로 슬러그
  title        TEXT NOT NULL,
  kind         TEXT NOT NULL,     -- material | output | note
  rel_deadline TEXT,              -- deadline.id
  bytes        INTEGER NOT NULL,
  updated_at   TEXT NOT NULL
);

-- docs_search 용 인덱스 (제목 검색)
CREATE INDEX IF NOT EXISTS idx_doc_kind ON doc(kind);
CREATE INDEX IF NOT EXISTS idx_deadline_due ON deadline(due_at);
CREATE INDEX IF NOT EXISTS idx_block_start ON block(start_at);
CREATE INDEX IF NOT EXISTS idx_item_due ON item(due_at);
CREATE INDEX IF NOT EXISTS idx_done_mark_at ON done_mark(at);

-- 웹 푸시 (2026-09-23). push_sub = 알림을 받겠다고 등록한 기기(브라우저) 하나당 한 줄.
-- 푸시 서비스가 404/410 을 주면(앱 삭제·권한 철회) 그 줄을 지운다.
CREATE TABLE IF NOT EXISTS push_sub (
  endpoint    TEXT PRIMARY KEY,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  ua          TEXT,
  at          TEXT NOT NULL,
  last_ok     TEXT
);
-- push_log = 보낸 알림. key 로 같은 알림을 두 번 안 보낸다 (예: due24:<id>:<due_at>).
CREATE TABLE IF NOT EXISTS push_log (
  key         TEXT PRIMARY KEY,
  at          TEXT NOT NULL,
  title       TEXT,
  body        TEXT,
  sent        INTEGER,            -- 받은 기기 수
  url         TEXT                -- 누르면 갈 곳 (2026-09-26, 알림함)
);

-- 집중 타이머 (2026-09-26). 예전엔 브라우저 localStorage 에만 있었다 — 앱을 뒤로 보내거나 기기를 바꾸면
-- 날아갈 수 있어서 서버로 옮겼다. focus_now = 지금 켜진 것 하나(없으면 행 없음).
-- start_ms 가 NULL 이면 멈춤 상태, acc_ms 는 멈추기 전까지 모인 시간. 시각은 서버 epoch ms.
CREATE TABLE IF NOT EXISTS focus_now (
  k           INTEGER PRIMARY KEY CHECK (k = 1),
  id          TEXT NOT NULL,
  label       TEXT,
  est         REAL,
  start_ms    INTEGER,
  acc_ms      INTEGER NOT NULL DEFAULT 0,
  began_ms    INTEGER NOT NULL
);
-- focus_log = 끝낸 집중을 (블록, KST 날짜) 로 합산. 취소한 건 안 들어온다.
CREATE TABLE IF NOT EXISTS focus_log (
  id          TEXT NOT NULL,
  day         TEXT NOT NULL,
  ms          INTEGER NOT NULL,
  PRIMARY KEY (id, day)
);
