"""메일 대시보드(기간별 탭) + 카테고리/계정 설정 + 작업 실행(액션 태스크) 로컬 웹 UI.

로컬(127.0.0.1)에서만 실행하는 걸 전제로 인증을 넣지 않았다 - 외부에 노출하지 말 것.
`python -m admin_ui`로 실행하면 http://127.0.0.1:5000 에서 다음 세 화면을 쓸 수 있다:

- `/` - 메인 대시보드. 일일/주간/월별/연도별/전체 탭(기간만 다르고 같은 화면 -
  generate_html.py의 build_report()/render_report_*() 조각 함수들을 재사용해 그때그때
  app.db를 쿼리해서 렌더링한다. IMAP 재조회 없음). 일일/주간/월간은 ◀이전/다음▶
  화살표로 과거 기간을 오갈 수 있다(기본값: 오늘/이번 주/이번 달, `?date=YYYY-MM-DD`로
  기준일 지정 - 미래로는 못 감). 연도별은 이동 기능 없이 항상 "올해"만 보여준다(요청
  범위 밖). "전체" 탭은 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 통계를 보여준다.
  화면 오른쪽 위 "동기화" 버튼은 fetch_mail.py를 --since 없이(계정별 마지막 저장
  시점부터, dry-run) 실행하고 돌아온다 - 실제 메일함 변경(--apply)은 여전히 /tasks
  전용. 카테고리 통계 타일/탭과 계정 태그/계정 카드 내부 카테고리는 전부 건수
  상위 N개만 기본 노출하고 나머지는 "···"로 접는다(JS 없이 CSS 체크박스 토글).
  계정 태그(포털·이메일·총건수)를 클릭하면 아래 "계정별 상세"의 그 계정 카드로
  포커스가 이동하며 펼쳐진다 - 계정 카드를 클릭해도 같은 방식으로 펼쳐지고, 그 안에서
  카테고리 필터 칩 + 실제 페이지네이션 목록을 볼 수 있다(한 번에 한 계정만 열림,
  `?acct=`/`acct_cat=`/`acct_page=` 쿼리로 서버가 상태를 관리 - JS 없음).
- `/list` - 일일/주간/월간/전체 각 기간의 메일을 계정/카테고리 드롭다운 필터 +
  페이지네이션으로 보여주는 별도 화면(`/`의 리포트 화면 안 "이 기간 목록 보기" 링크로
  진입, "전체"는 최근 2년 고정 기간). "이 조건으로 작업 실행" 버튼으로 /tasks와
  연결되는 대량 필터링용 화면이라 그대로 남겨뒀다("/"의 계정 카드 인라인 목록과는
  별개 - 계정 카드는 한 계정만, 이 화면은 여러 계정을 한 번에 필터링한다).
- `/settings` - 카테고리 관리 + 메일 계정 관리(⚙️ 아이콘으로 진입).
- `/tasks` - fetch_mail.py를 버튼으로 실행(새로고침/실제 처리)하고, 페이지네이션 목록에서
  메일을 체크박스로 선택(페이지를 넘겨도 localStorage로 유지)해 "선택 실행" → 요약 확인
  모달 → 휴지통/보관/읽음 처리한다.
- `/vault` - 정리함. 처리된 메일을 `보관함`(archived)/`휴지통`(trashed) 탭으로 나눠 보고,
  보관함은 원래 받은편지함으로 "되돌리기", 휴지통은 서버에서 "영구 삭제"한다. 메일이
  옮겨지면 UID가 바뀌므로 messages.message_id로 대상 폴더에서 다시 찾는다.

여기서 저장/실행한 내용은 바로 다음 fetch_mail.py 실행에 반영된다(같은 data/app.db를
config_store.load_categories()로 읽으므로 - app.db는 categories 테이블과 원본 메일 로그
messages/action_runs 테이블을 함께 담고 있는 단일 SQLite 파일).
"""
from urllib.parse import urlparse

from flask import Flask, request

from admin_ui._shared import DB_PATH
from admin_ui.dashboard import bp as _dashboard_bp
from admin_ui.settings import bp as _settings_bp
from admin_ui.tasks import bp as _tasks_bp
from admin_ui.vault import bp as _vault_bp

# 이 모듈은 이제 app 조립 + 전역 요청 가드만 담당한다. 실제 화면 로직은
# 기능별 블루프린트로 나뉘어 있다 (admin_ui/{dashboard,settings,tasks,vault}.py).
# 공통 커널: _shared.py(상수 + page()/esc()/run_state), _render.py(메일목록 조회/렌더).
#
# ⚠️ 보안: 블루프린트들은 CSRF/Host 방어 코드를 자체적으로 갖고 있지 않다.
# 아래 _block_cross_origin_writes(@app.before_request)가 이 app 에 등록된 **모든**
# 블루프린트 라우트를 한 곳에서 막는다(/vault/purge 의 EXPUNGE 포함). 블루프린트를
# 다른 Flask() 인스턴스에 등록하려면 이 가드도 같이 옮겨야 한다.
app = Flask(__name__)
app.register_blueprint(_dashboard_bp)  # /, /list, /sync (admin_ui/dashboard.py)
app.register_blueprint(_settings_bp)   # /settings, 카테고리·계정 CRUD (admin_ui/settings.py)
app.register_blueprint(_tasks_bp)      # /tasks, 파이프라인 실행 + 개별 메일 액션 (admin_ui/tasks.py)
app.register_blueprint(_vault_bp)      # /vault, 보관함/휴지통 되돌리기·영구삭제 (admin_ui/vault.py)


# 이 앱이 바인드되는 호스트(데스크톱 앱이 포트를 바꿔도 호스트명은 이 셋 중 하나).
# request.host 는 Host 헤더라 공격자가 조종할 수 있어서(DNS rebinding), 로컬 호스트가
# 아니면 상태 변경을 아예 거부한다. Origin/Host 가 둘 다 attacker 도메인이면 예전엔
# 통과했다.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def _host_name(host: str) -> str:
    """"127.0.0.1:5000" → "127.0.0.1", "[::1]:5000" → "::1"."""
    h = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") else host
    return h.strip("[]")


def _is_cross_origin_post() -> bool:
    """상태 변경 POST가 로컬 앱 자신이 아닌 다른 출처에서 왔는지 판정한다.

    이 앱은 127.0.0.1 전용이고 세션 쿠키가 없어 SameSite 보호가 없다 - 브라우저의
    다른 탭이 cross-origin 폼 POST로 /vault/purge(영구삭제) 등을 때리는 CSRF를 막는다.
    포트는 request.host(실제 바인드) 기준으로 비교해 데스크톱 앱이 포트를 바꿔도 동작한다.

    Origin/Referer 헤더가 아예 없으면(curl/스크립트, 그리고 **Electron 스케줄러가
    raw http 로 때리는 /sync**. desktop/scheduler.js 참고) 통과시킨다. 이 폴백을
    없애면 백그라운드 동기화가 깨지므로 주의.
    """
    host = request.host  # 예: "127.0.0.1:5000"
    origin = request.headers.get("Origin")
    if origin is not None:
        return urlparse(origin).netloc != host
    referer = request.headers.get("Referer")
    if referer:
        return urlparse(referer).netloc != host
    return False


@app.before_request
def _block_cross_origin_writes():
    """모든 상태 변경 POST(설정 CRUD·/sync·/tasks/apply·/vault/* 포함)를 한 곳에서
    cross-origin CSRF + DNS rebinding 으로부터 막는다. 예전엔 /tasks/action·
    /vault/restore·/vault/purge 세 곳에만 개별로 걸려 있어서, 다른 탭의 악성 페이지가
    /tasks/apply(실제 메일함 정리)나 /settings/accounts/<user>/delete 를 POST 로
    때릴 수 있었다.

    Host 검증은 GET 을 포함한 **모든** 메서드에 건다: 공격자가 자기 도메인을
    127.0.0.1 로 rebind 하면 브라우저 입장에선 same-origin 이 되어 GET 응답
    (메일 발신인·제목 등 메타데이터)까지 스크립트로 읽어갈 수 있기 때문이다.
    이 앱은 127.0.0.1 에만 바인드하므로 정상 요청의 Host 는 늘 로컬이다."""
    if _host_name(request.host) not in _LOCAL_HOSTS:
        return "로컬 호스트가 아닌 요청 거부", 403
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if _is_cross_origin_post():
        return "cross-origin 요청 거부", 403
    return None


def main() -> None:
    """`python -m admin_ui` / `mail-admin` 진입점."""
    print(f"메일 대시보드: http://127.0.0.1:5000  (DB: {DB_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
