"""공개 정적 면 세 개 — erion 안내 · 개인정보처리방침 · 이용약관.

구글 OAuth 동의 화면이 "앱 홈페이지 / 개인정보처리방침 / 서비스 약관" URL 을 요구한다.
민감한 범위(calendar.readonly)를 쓰는 앱은 이 셋이 없으면 **게시 상태를 프로덕션으로
못 올리고**, 테스트에 머물면 refresh token 이 7일마다 죽는다 (memory/verified.md).

nginx 가 `/erion/` 접두어를 벗기므로 여기서는 `/`, `/privacy`, `/terms` 로 보인다.
공개되는 유일한 사람 읽는 면이다 — 내용은 실제 동작과 어긋나면 안 된다.
바뀌면(수집 항목·보관·제3자 제공) 이 파일도 같이 고쳐라.
"""

from starlette.responses import HTMLResponse

ISSUER_HOST = "sqhsxp.duckdns.org"
UPDATED = "2026-09-18"

_CSS = """
:root{color-scheme:dark}
body{margin:0;background:#0d1117;color:#e6edf3;
     font:16px/1.75 -apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif}
main{max-width:46rem;margin:0 auto;padding:3rem 1.25rem 5rem}
h1{font-size:1.6rem;margin:0 0 .4rem}
h2{font-size:1.05rem;margin:2.2rem 0 .6rem;color:#7ee787}
p,li{color:#c9d1d9}
li{margin:.3rem 0}
code{background:#161b22;border:1px solid #30363d;border-radius:5px;padding:.1rem .35rem;
     font-size:.9em;color:#a5d6ff}
a{color:#58a6ff}
.sub{color:#7d8590;font-size:.9rem;margin:0 0 2rem}
nav{margin-top:3rem;padding-top:1.2rem;border-top:1px solid #21262d;font-size:.9rem}
nav a{margin-right:1.2rem}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=ko><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title} · erion</title><style>{_CSS}</style><main>{body}"
        f"<nav><a href='/erion/'>erion</a>"
        f"<a href='/erion/privacy'>개인정보처리방침</a>"
        f"<a href='/erion/terms'>이용약관</a></nav></main></html>")


async def home(request):
    return _page("erion", f"""
<h1>erion</h1>
<p class=sub>개인 한 사람을 위한 일정·마감 볼트</p>
<p>erion 은 운영자 본인이 자기 용도로 만들어 쓰는 비상업 서비스다. 학교 학습관리시스템의
과제 마감과 구글 캘린더의 일정을 한곳에 모아, 남은 시간이 실제로 얼마인지 계산한다.</p>
<h2>구성</h2>
<ul>
  <li>마감·일정을 주기적으로 받아 저장하는 수집기</li>
  <li>저장된 것을 좁은 질의로만 읽게 하는 MCP 서버 <code>/erion/mcp</code></li>
  <li>그 접근을 통제하는 OAuth 인가 서버</li>
</ul>
<p>가입 절차도, 다른 사용자도 없다. 문의는 구글 동의 화면에 표시된 지원 이메일로.</p>
<p><a href="/erion/web">운영자 대시보드</a></p>""")


async def privacy(request):
    return _page("개인정보처리방침", f"""
<h1>개인정보처리방침 · Privacy Policy</h1>
<p class=sub>앱 이름 erion · 운영 주소 https://{ISSUER_HOST}/erion/ · 최종 수정 {UPDATED}</p>
<p>이 방침은 <b>erion</b>(이하 "서비스")이 구글 사용자 데이터를 포함한 정보를 어떻게
접근·이용·저장·공유·파기하는지 설명합니다. erion 은 운영자 본인 1인이 자신의 일정 관리를
위해 운영하는 비상업 서비스입니다. 다른 이용자를 받지 않으며, 회원가입 기능이 없습니다.</p>

<h2>1. 접근하는 구글 사용자 데이터</h2>
<ul>
  <li>권한 범위는 구글 캘린더 읽기 전용 <code>https://www.googleapis.com/auth/calendar.readonly</code>
      하나입니다. 캘린더에 쓰거나 일정을 수정·삭제하지 않습니다.</li>
  <li>일정마다 가져오는 항목: <b>제목, 시작·종료 시각, 장소, 일정 ID, 취소 여부</b>.
      참석자 명단, 설명(본문), 첨부파일, 회의 링크는 저장하지 않습니다.</li>
  <li>범위는 <b>지난 1일부터 앞으로 30일</b>까지이며, 20분 간격으로 갱신합니다.</li>
  <li>구글 계정 인증을 위해 구글이 발급한 액세스 토큰과 갱신(refresh) 토큰을 보관합니다.</li>
  <li>Gmail, 드라이브, 연락처, 사진 등 다른 구글 데이터는 <b>요청하지도 접근하지도 않습니다.</b></li>
</ul>
<p>구글 외에 학교 학습관리시스템(Canvas)의 과제 이름·설명·제출 형식·링크·마감 시각·제출 여부·배점도
수집합니다. 운영자가 AI 비서에게 요청할 때에 한해 Canvas 의 강의 모듈 항목(제목·링크·페이지 본문)을
그 자리에서 조회합니다.</p>

<h2>2. 이용 목적</h2>
<p>과제 마감과 일정을 겹쳐 놓고 운영자에게 남은 가용 시간과 과제별 예상 소요시간을 보여 주는 것,
그 하나뿐입니다.
구글 사용자 데이터를 다음 용도로는 <b>사용하지 않습니다</b>:</p>
<ul>
  <li>광고(맞춤형·리타게팅 포함)</li>
  <li>판매, 데이터 브로커·정보 재판매자에게 제공</li>
  <li>신용도 판단이나 대출 목적</li>
  <li>일반(비개인화) 인공지능·머신러닝 모델의 학습 또는 개선</li>
</ul>

<h2>3. 공유 및 전달</h2>
<p>데이터를 판매·대여하지 않으며, 광고·분석 업체와 공유하지 않습니다. 분석 도구나 추적
스크립트도 없습니다. 서비스 밖으로 나가는 경로는 아래 둘뿐입니다.</p>
<ul>
  <li><b>운영자 본인의 AI 비서(Anthropic 의 Claude).</b> 운영자가 자기 Claude 계정에서
      이 서비스를 커넥터(MCP)로 연결해 두었고, 운영자가 대화에서 일정을 물으면 서비스는
      요청된 기간의 일정(제목·시각·장소·상태, 최대 50건)만 그 대화로 돌려줍니다. 이는
      운영자 본인에게 답하기 위한 전달이며, 해당 데이터의 처리에는
      <a href="https://www.anthropic.com/legal/privacy">Anthropic 개인정보처리방침</a>이
      적용됩니다. 이 서비스는 그 데이터를 AI 학습용으로 제공하지 않습니다.</li>
  <li><b>과제 소요시간 추정용 AI API (Google Gemini API, 실패 시 NVIDIA NIM API).</b>
      새 Canvas 과제가 들어오면 그 과제의 이름·설명·제출 형식·배점·마감과 운영자가 남긴
      소요시간 보정 기록을 과제당 한두 번 보내 예상 소요시간과 표시 이름을 받습니다.
      <b>구글 캘린더 데이터는 이 경로로 보내지 않습니다.</b> 전송된 내용의 처리에는 각 제공자의
      약관이 적용되며, 무료 등급에서는 제공자가 입력을 자기 서비스 개선에 쓸 수 있습니다.</li>
  <li>법령에 따라 제출 의무가 생긴 경우에는 그 범위 안에서 제공할 수 있습니다.</li>
</ul>

<h2>4. 저장과 보호</h2>
<ul>
  <li>운영자가 직접 관리하는 서버(Oracle Cloud)의 SQLite 데이터베이스에 저장합니다.
      제3자 데이터베이스·백업 서비스로 복제하지 않습니다.</li>
  <li>구글 토큰은 운영자 계정만 읽을 수 있는 권한(600)의 별도 파일에 두며, 로그나
      화면에 출력하지 않습니다.</li>
  <li>모든 전송은 HTTPS(TLS)로 암호화됩니다.</li>
  <li>서비스의 데이터 조회 면(MCP)은 OAuth 2.1(PKCE) 인가와 운영자 비밀번호로 잠겨
      있습니다. 발급 토큰은 1시간(갱신 토큰 30일) 후 만료됩니다.</li>
  <li>운영자용 대시보드(<code>/erion/web</code>)는 운영자 비밀번호로 잠겨 있으며, 로그인
      쿠키는 30일 뒤 만료됩니다.</li>
  <li>조회 응답은 행 수 상한이 있어 한 번에 전체 데이터를 내보내지 않습니다.</li>
</ul>

<h2>5. 보관 기간과 파기</h2>
<ul>
  <li>일정 데이터는 서비스를 운영하는 동안 보관하며, 구글 캘린더에서 취소된 일정은
      다음 갱신 때 취소 상태로 반영됩니다.</li>
  <li>구글 접근 권한이 철회되거나 서비스 운영을 끝내면, 운영자는 저장된 구글 토큰과
      캘린더 데이터를 <b>30일 이내에 삭제</b>합니다.</li>
  <li>삭제를 원하면 아래 문의처로 요청할 수 있으며, 요청을 받은 날부터 30일 이내에
      삭제합니다.</li>
</ul>

<h2>6. 권한 철회</h2>
<p><a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>
에서 erion 의 접근 권한을 해제하면 그 즉시 새로운 수집이 중단됩니다. 이미 저장된 데이터는
5항에 따라 삭제됩니다.</p>

<h2>7. 구글 API 서비스 사용자 데이터 정책 준수</h2>
<p>erion 이 구글 API 로부터 받은 정보의 이용과 다른 앱으로의 이전은
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
Services User Data Policy</a>의 제한된 사용(Limited Use) 요건을 포함한 정책을 따릅니다.</p>

<h2>8. 방침의 변경</h2>
<p>구글 사용자 데이터의 이용 방식이 바뀌면 이 페이지를 먼저 고치고 상단의 최종 수정일을
갱신합니다. 수집 범위가 넓어지는 변경은 구글 동의 화면에서 다시 동의를 받습니다.</p>

<h2>9. 문의</h2>
<p>운영자: erion 운영자(개인). 구글 동의 화면에 표시된 지원 이메일로 연락하시면 됩니다.</p>

<h2 id=en>English summary</h2>
<p><b>erion</b> is a private, non-commercial tool run by a single person for their own
schedule. It has no other users and no sign-up.</p>
<ul>
  <li><b>Data accessed:</b> Google Calendar via the read-only scope
      <code>calendar.readonly</code> only — event title, start/end time, location, event ID and
      cancellation status, for the window from 1 day ago to 30 days ahead, refreshed every
      20 minutes. OAuth access/refresh tokens are stored. No other Google data is accessed.</li>
  <li><b>Use:</b> solely to show the operator how much free time remains around their
      coursework deadlines. Google user data is never used for advertising, sold, given to
      data brokers, used for credit decisions, or used to train generalized AI/ML models.</li>
  <li><b>Sharing:</b> not sold or shared with advertisers or analytics providers. The only
      transfer is to the operator's own Claude assistant (Anthropic), connected by the
      operator via MCP: when the operator asks about their schedule, the requested events
      (max 50) are returned into that conversation, governed by
      <a href="https://www.anthropic.com/legal/privacy">Anthropic's privacy policy</a>.
      Data may also be disclosed if required by law.</li>
  <li><b>Protection:</b> stored in SQLite on an operator-managed server; tokens in a
      file readable only by the operator account (mode 600); HTTPS everywhere; the data API is
      protected by OAuth 2.1 with PKCE plus the operator's password.</li>
  <li><b>Retention &amp; deletion:</b> kept while the service runs. If access is revoked, the
      service is shut down, or deletion is requested, stored tokens and calendar data are
      deleted within 30 days. Access can be revoked at
      <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</li>
  <li><b>Limited Use:</b> erion's use and transfer of information received from Google APIs
      adheres to the
      <a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
      Services User Data Policy</a>, including the Limited Use requirements.</li>
  <li><b>Changes:</b> this page is updated before any change in how Google user data is used.</li>
  <li><b>Contact:</b> the support email shown on the Google consent screen.</li>
</ul>""")


async def terms(request):
    return _page("이용약관", f"""
<h1>이용약관</h1>
<p class=sub>최종 수정 {UPDATED}</p>

<h2>1. 적용 범위</h2>
<p>erion 은 운영자 본인 1인이 사용하는 개인용 비상업 서비스입니다. 일반에 제공되는
서비스가 아니며, 제3자의 가입을 받지 않습니다.</p>

<h2>2. 이용 조건</h2>
<p>접근은 운영자 계정으로 한정됩니다. 허가 없이 접근을 시도하거나 자동화된 수단으로
자원을 소모시키는 행위를 금지합니다.</p>

<h2>3. 무보증</h2>
<p>서비스는 <b>있는 그대로</b> 제공됩니다. 마감 시각, 남은 시간, 일정 정보의 정확성과
완전성을 보증하지 않습니다. 표시된 내용은 참고용이며, 원본은 학습관리시스템과 구글
캘린더입니다. 이에 근거한 판단의 책임은 이용자 본인에게 있습니다.</p>

<h2>4. 중단과 변경</h2>
<p>운영자는 사전 통지 없이 서비스의 전부 또는 일부를 변경하거나 중단할 수 있습니다.</p>

<h2>5. 준거법</h2>
<p>이 약관은 대한민국 법률에 따릅니다.</p>""")
