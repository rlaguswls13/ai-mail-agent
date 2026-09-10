"""admin_ui 블루프린트들이 공유하는 얇은 커널 - 상수 + 페이지 셸 + 문자열 헬퍼.

admin_app.py(~2000줄)를 기능별 블루프린트(settings/tasks/vault/dashboard)로 쪼개는
중이다. 여러 블루프린트가 함께 쓰는 최소 원시 함수/상수만 여기 둔다. admin_app.py 도
여기서 re-import 하므로 `admin_app.DB_PATH` 같은 기존 참조는 그대로 동작한다.
순환 import 를 막기 위해 이 모듈은 admin_app / 블루프린트를 절대 import 하지 않는다.
"""
import html
from urllib.parse import quote

from mail_app import app_paths
from mail_app.web_style import STYLE_CSS, render_nav

DATA_DIR = app_paths.data_dir()  # 기본: 저장소의 data/. 데스크톱 앱은 MAIL_AGENT_DATA_DIR.
DB_PATH = app_paths.db_path()
JSON_EXPORT_PATH = app_paths.config_dir() / "categories.json"
ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"
RUN_TIMEOUT_SECONDS = 300

ACTIONS = ["keep", "trash", "save", "read"]
PRIORITIES = ["HIGH", "NORMAL", "LOW"]
PROVIDER_LABEL = {"gmail": "Gmail", "naver": "Naver", "outlook": "Outlook"}

# 서버 프로세스가 떠 있는 동안만 유지되는 마지막 실행 결과들(재시작하면 사라짐) - 개인용
# 단일 사용자 로컬 도구라 DB에 영구 기록할 필요까지는 없다. 실제 액션 결과 자체는
# fetch_mail.py 가 app.db 의 action_runs 에 별도로 남긴다.
#   last_run         : /sync·/tasks/run·/tasks/apply 의 fetch->generate 파이프라인 결과
#   last_task_action : /tasks/action(개별 메일 수동 처리) 결과
#   last_vault_action: /vault 되돌리기/영구삭제 결과
# admin_app 과 여러 블루프린트가 함께 읽고 쓰므로 모듈 전역 대신 이 dict 로 모은다.
run_state: dict = {"last_run": None, "last_task_action": None, "last_vault_action": None}

_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22>"
    "<text y=%2213%22 font-size=%2213%22>%F0%9F%93%AC</text></svg>"
)

# 모든 페이지 공용 JS (최소):
#  1. .slow-form 제출 시 버튼을 disabled + "실행 중…" 으로 - /sync, /tasks/run, /tasks/apply
#     처럼 수십 초~2분 걸리는 동기 subprocess 실행에 피드백을 준다.
#  2. 폼 검증 실패로 렌더된 .form-error 배너에 포커스를 준다(스크린리더 안내).
PAGE_SCRIPT = """<script>
(function () {
  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    var f = e.target;
    if (!(f instanceof HTMLFormElement) || !f.classList.contains('slow-form')) return;
    var btn = f.querySelector('button[type=submit], button:not([type])');
    if (btn && !btn.disabled) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spin" aria-hidden="true"></span> 실행 중… (수십 초~2분 소요)';
    }
  });
  var err = document.querySelector('.form-error[tabindex]');
  if (err) err.focus();
})();
</script>"""


def page(title: str, body: str, active: str) -> str:
    return (
        f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="icon" href="{_FAVICON}">'
        f"<title>{esc(title)}</title>"
        f"<style>{STYLE_CSS}</style></head>"
        f'<body>{render_nav(active)}<div class="wrap">{body}</div>{PAGE_SCRIPT}</body></html>'
    )


def esc(s: str | None) -> str:
    return html.escape(s or "")


def kw_to_text(values: list[str]) -> str:
    return "\n".join(values)


def text_to_kw(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def build_qs(**params) -> str:
    """None/빈 문자열 값은 빼고 쿼리스트링을 만든다 - 필터/페이지 링크를 조립할 때 쓴다."""
    parts = [f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, "")]
    return "&".join(parts)
