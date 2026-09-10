"""메일 대시보드(기간별 탭) + 카테고리/계정 설정 + 작업 실행(액션 태스크) 로컬 웹 UI.

로컬(127.0.0.1)에서만 실행하는 걸 전제로 인증을 넣지 않았다 — 외부에 노출하지 말 것.
`python -m admin_ui`로 실행하면 http://127.0.0.1:5000 에서 다음 세 화면을 쓸 수 있다:

- `/` — 메인 대시보드. 일일/주간/월별/연도별/전체 탭(기간만 다르고 같은 화면 —
  generate_html.py의 build_report()/render_report_*() 조각 함수들을 재사용해 그때그때
  app.db를 쿼리해서 렌더링한다. IMAP 재조회 없음). 일일/주간/월간은 ◀이전/다음▶
  화살표로 과거 기간을 오갈 수 있다(기본값: 오늘/이번 주/이번 달, `?date=YYYY-MM-DD`로
  기준일 지정 — 미래로는 못 감). 연도별은 이동 기능 없이 항상 "올해"만 보여준다(요청
  범위 밖). "전체" 탭은 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 통계를 보여준다.
  화면 오른쪽 위 "동기화" 버튼은 fetch_mail.py를 --since 없이(계정별 마지막 저장
  시점부터, dry-run) 실행하고 돌아온다 — 실제 메일함 변경(--apply)은 여전히 /tasks
  전용. 카테고리 통계 타일/탭과 계정 태그/계정 카드 내부 카테고리는 전부 건수
  상위 N개만 기본 노출하고 나머지는 "···"로 접는다(JS 없이 CSS 체크박스 토글).
  계정 태그(포털·이메일·총건수)를 클릭하면 아래 "계정별 상세"의 그 계정 카드로
  포커스가 이동하며 펼쳐진다 — 계정 카드를 클릭해도 같은 방식으로 펼쳐지고, 그 안에서
  카테고리 필터 칩 + 실제 페이지네이션 목록을 볼 수 있다(한 번에 한 계정만 열림,
  `?acct=`/`acct_cat=`/`acct_page=` 쿼리로 서버가 상태를 관리 — JS 없음).
- `/list` — 일일/주간/월간/전체 각 기간의 메일을 계정/카테고리 드롭다운 필터 +
  페이지네이션으로 보여주는 별도 화면(`/`의 리포트 화면 안 "이 기간 목록 보기" 링크로
  진입, "전체"는 최근 2년 고정 기간). "이 조건으로 작업 실행" 버튼으로 /tasks와
  연결되는 대량 필터링용 화면이라 그대로 남겨뒀다("/"의 계정 카드 인라인 목록과는
  별개 — 계정 카드는 한 계정만, 이 화면은 여러 계정을 한 번에 필터링한다).
- `/settings` — 카테고리 관리 + 메일 계정 관리(⚙️ 아이콘으로 진입).
- `/tasks` — fetch_mail.py를 버튼으로 실행(새로고침/실제 처리)하고, 페이지네이션 목록에서
  메일을 체크박스로 선택(페이지를 넘겨도 localStorage로 유지)해 "선택 실행" → 요약 확인
  모달 → 휴지통/보관/읽음 처리한다.
- `/vault` — 정리함. 처리된 메일을 `보관함`(archived)/`휴지통`(trashed) 탭으로 나눠 보고,
  보관함은 원래 받은편지함으로 "되돌리기", 휴지통은 서버에서 "영구 삭제"한다. 메일이
  옮겨지면 UID가 바뀌므로 messages.message_id로 대상 폴더에서 다시 찾는다.

여기서 저장/실행한 내용은 바로 다음 fetch_mail.py 실행에 반영된다(같은 data/app.db를
config_store.load_categories()로 읽으므로 — app.db는 categories 테이블과 원본 메일 로그
messages/action_runs 테이블을 함께 담고 있는 단일 SQLite 파일).
"""
import html
import imaplib
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, request, url_for

from mail_core.actions import (
    decode_mailbox_name,
    find_archive_folder,
    find_message_uid_by_id,
    find_trash_folder,
    mark_as_read,
    move_to_folder,
    permanent_delete,
    select_folder,
)
from mail_core.classify import classify

from mail_core.accounts import (
    IMAP_SERVERS,
    add_account,
    delete_account,
    env_mode,
    load_accounts,
    update_account,
)

from mail_app import app_paths, config_store, generate_html
from mail_app.mail_log_store import (
    account_type_for,
    delete_messages,
    distinct_accounts,
    log_action_run,
    mark_message_status,
    query_messages,
)
from mail_app.web_style import STYLE_CSS, render_nav

DATA_DIR = app_paths.data_dir()  # 기본: 저장소의 data/. 데스크톱 앱은 MAIL_AGENT_DATA_DIR.
DB_PATH = app_paths.db_path()
# "내보내기" 백업(사람이 읽는 용도) — app_paths.config_dir()이 dev/번들을 알아서 가른다.
JSON_EXPORT_PATH = app_paths.config_dir() / "categories.json"
ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"
RUN_TIMEOUT_SECONDS = 300

ACTIONS = ["keep", "trash", "save", "read"]
PRIORITIES = ["HIGH", "NORMAL", "LOW"]
PROVIDER_LABEL = {"gmail": "Gmail", "naver": "Naver", "outlook": "Outlook"}

MSG_PAGE_SIZE_DEFAULT = 50
MSG_PAGE_SIZE_MIN = 10
MSG_PAGE_SIZE_MAX = 100
MSG_DEFAULT_SINCE_DAYS = 730  # /list의 "전체"·작업 실행 화면이 쓰는 고정 기간(최근 2년).
EPOCH_START = datetime(2000, 1, 1)  # "/"의 전체 탭 통계 — app.db에 있는 진짜 전체 기간.
ACCOUNT_LIST_PAGE_SIZE = 20  # 계정 카드를 펼쳤을 때 그 안 페이지네이션 목록의 페이지당 건수.

RANGE_TABS = [
    ("daily", "일일"),
    ("weekly", "주간"),
    ("monthly", "월별"),
    ("yearly", "연도별"),
    ("all", "전체"),
]

# action 이름 -> 대상 폴더를 찾는 함수. fetch_mail.py의 ACTION_FOLDER_FINDERS와 동일한 개념을
# "선택한 메일만" 처리하는 수동 액션에도 재사용한다. "read"는 폴더 이동이 아니라서 여기 없음.
MSG_ACTION_FOLDER_FINDERS = {"trash": find_trash_folder, "save": find_archive_folder}
# action 이름 -> messages.status에 기록할 값 (fetch_mail.py의 ACTION_STATUS와 동일한 개념).
MSG_ACTION_STATUS = {"trash": "trashed", "save": "archived"}
MSG_ACTION_PILL_CLASS = {"trash": "trash", "save": "save", "read": "read"}

app = Flask(__name__)

# 서버 프로세스가 떠 있는 동안만 유지되는 마지막 실행 결과(재시작하면 사라짐) — 개인용
# 단일 사용자 로컬 도구라 DB에 영구 기록할 필요까지는 없다고 판단했다. 실제 액션 결과
# 자체는 fetch_mail.py가 app.db의 action_runs에 별도로 남기니 여기서 날아가도 안전하다.
LAST_RUN: dict | None = None
# /tasks에서 처리한 마지막 결과(재시작하면 사라짐).
LAST_TASK_ACTION: dict | None = None
# /vault(보관함/휴지통)에서 되돌리기/영구삭제한 마지막 결과(재시작하면 사라짐).
LAST_VAULT_ACTION: dict | None = None

# /vault 탭: (탭 키, 라벨, messages.status 값, 대상 폴더 finder, 되돌리기/삭제 동사).
VAULT_TABS = [
    ("archive", "보관함", "archived", find_archive_folder, "restore"),
    ("trash", "휴지통", "trashed", find_trash_folder, "purge"),
]
VAULT_TAB_BY_KEY = {t[0]: t for t in VAULT_TABS}


_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22>"
    "<text y=%2213%22 font-size=%2213%22>%F0%9F%93%AC</text></svg>"
)

# 모든 페이지 공용 JS (최소):
#  1. .slow-form 제출 시 버튼을 disabled + "실행 중…" 으로 — /sync, /tasks/run, /tasks/apply
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


# 대시보드 "계정별 상세" 캐러셀 — ‹/› 버튼으로 트랙을 좌우 스크롤하고, 양 끝에 닿으면
# 해당 버튼을 숨긴다. 펼쳐진 계정 카드(data-open-acct)가 있으면 로드 시 거기로 스크롤한다.
# 접기/펼치기 자체는 여전히 <a href="/?acct=…"> 링크(서버 왕복)라 JS가 필요 없다.
CAROUSEL_SCRIPT = """<script>
(function () {
  var box = document.querySelector('.account-carousel');
  if (!box) return;
  var track = box.querySelector('.account-track');
  var prev = box.querySelector('.carousel-nav.prev');
  var next = box.querySelector('.carousel-nav.next');
  if (!track || !prev || !next) return;

  function step() {
    var card = track.querySelector('.account-detail');
    var gap = parseFloat(getComputedStyle(track).columnGap || getComputedStyle(track).gap) || 12;
    return card ? card.getBoundingClientRect().width + gap : track.clientWidth * 0.8;
  }
  function sync() {
    var max = track.scrollWidth - track.clientWidth - 1;
    var overflowing = max > 0;
    prev.disabled = !overflowing || track.scrollLeft <= 0;
    next.disabled = !overflowing || track.scrollLeft >= max;
  }
  prev.addEventListener('click', function () { track.scrollBy({ left: -step(), behavior: 'smooth' }); });
  next.addEventListener('click', function () { track.scrollBy({ left: step(), behavior: 'smooth' }); });
  track.addEventListener('scroll', sync, { passive: true });
  window.addEventListener('resize', sync);

  var openId = box.dataset.openAcct;
  if (openId) {
    var openCard = document.getElementById(openId);
    if (openCard) {
      var left = openCard.offsetLeft - track.offsetLeft;
      track.scrollTo({ left: left, behavior: 'auto' });
    }
  }
  sync();
})();
</script>"""


# /tasks 목록의 인라인 대량 선택 — 체크 상태를 localStorage에 저장해서 페이지를 넘기거나
# 필터를 바꿔도 선택이 유지된다(7일 후 자동 만료). "선택 실행" → /tasks/action/preview(JSON)
# 요약을 <dialog>에 채우고 → 액션 버튼 → confirm() → 숨은 #task-form 제출(/tasks/action).
# 처리 후 서버는 ?done=1 + <script id="task-result">(실패 key)로 되돌려보내고, 그때
# 성공분만 선택에서 지운다(실패분은 재시도할 수 있게 남긴다).
TASKS_SCRIPT = """<script>
(function () {
  var KEY = 'mailagent.tasks.selection';
  var MAX_AGE = 7 * 24 * 3600 * 1000;
  function load() {
    try {
      var raw = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (!raw || !raw.keys) return {};
      if (raw.ts && Date.now() - raw.ts > MAX_AGE) return {};
      return raw.keys;
    } catch (e) { return {}; }
  }
  function save(s) {
    try { localStorage.setItem(KEY, JSON.stringify({ v: 1, ts: Date.now(), keys: s })); }
    catch (e) { if (warnEl) warnEl.hidden = false; }
  }
  var warnEl = document.getElementById('sel-warn');
  var store = load();

  var params = new URLSearchParams(location.search);
  if (params.get('done') === '1') {
    var failed = {};
    try {
      var res = JSON.parse((document.getElementById('task-result') || {}).textContent || '{}');
      (res.failed_keys || []).forEach(function (k) { failed[k] = true; });
    } catch (e) {}
    store = failed; save(store);
    params.delete('done');
    var qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  var countEl = document.getElementById('sel-count');
  var runBtn = document.getElementById('sel-run');
  function refresh() {
    var n = Object.keys(store).length;
    if (countEl) countEl.textContent = n + '건 선택됨';
    if (runBtn) runBtn.disabled = n === 0;
  }

  document.querySelectorAll('.sel-box').forEach(function (cb) {
    if (store[cb.dataset.key]) cb.checked = true;
    cb.addEventListener('change', function () {
      if (cb.checked) store[cb.dataset.key] = true; else delete store[cb.dataset.key];
      save(store); refresh();
    });
  });

  var clearBtn = document.getElementById('sel-clear');
  if (clearBtn) clearBtn.addEventListener('click', function () {
    store = {}; save(store);
    document.querySelectorAll('.sel-box').forEach(function (cb) { cb.checked = false; });
    refresh();
  });

  var modal = document.getElementById('confirm-modal');
  var bodyEl = document.getElementById('confirm-body');
  var form = document.getElementById('task-form');
  var actionBtns = document.querySelectorAll('#confirm-modal .confirm-actions button');
  var submittable = null;   // 프리뷰가 확정한 "실제로 처리 가능한" key 목록
  function keys() { return Object.keys(store); }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function setActionsEnabled(on) { actionBtns.forEach(function (b) { b.disabled = !on; }); }

  function renderSummary(d) {
    var h = '<p class="confirm-total"><strong>' + d.total + '건</strong> 처리 대상';
    if (d.missing) h += ' <span class="confirm-warn">· ' + d.missing + '건은 제외(이미 처리/삭제됨)</span>';
    h += '</p>';
    function group(title, items, keyName) {
      if (!items || !items.length) return '';
      var s = '<div class="confirm-group"><span class="confirm-group-title">' + title + '</span><ul>';
      items.forEach(function (it) { s += '<li>' + esc(it[keyName]) + ' <b>' + it.count + '</b></li>'; });
      return s + '</ul></div>';
    }
    h += group('카테고리별', d.by_category, 'name');
    h += group('계정별', d.by_account, 'account');
    return h;
  }

  if (runBtn) runBtn.addEventListener('click', function () {
    var ks = keys();
    if (!ks.length || !modal) return;
    submittable = null;
    setActionsEnabled(false);
    bodyEl.textContent = '불러오는 중…';
    modal.showModal();
    var fd = new FormData();
    ks.forEach(function (k) { fd.append('sel', k); });
    fetch('/tasks/action/preview', { method: 'POST', body: fd })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (d) {
        submittable = d.keys || ks;
        bodyEl.innerHTML = renderSummary(d);
        setActionsEnabled(submittable.length > 0);
      })
      .catch(function () {
        bodyEl.innerHTML = '<p class="confirm-warn">요약을 불러오지 못했습니다. 선택한 '
          + ks.length + '건 전체가 대상이 됩니다.</p>';
        submittable = ks;
        setActionsEnabled(true);
      });
  });

  actionBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var action = btn.dataset.action;
      var ks = submittable || keys();
      if (!ks.length) return;
      var labels = { trash: '휴지통 이동', save: '보관', read: '읽음 표시' };
      if (!confirm(ks.length + '건을 ' + (labels[action] || action) + ' 처리합니다. 실제로 메일함이 바뀝니다. 계속할까요?')) return;
      form.querySelectorAll('input[name="sel"], input[name="action"]').forEach(function (el) { el.remove(); });
      ks.forEach(function (k) {
        var i = document.createElement('input');
        i.type = 'hidden'; i.name = 'sel'; i.value = k; form.appendChild(i);
      });
      var a = document.createElement('input');
      a.type = 'hidden'; a.name = 'action'; a.value = action; form.appendChild(a);
      form.submit();
    });
  });

  var cancelBtn = document.getElementById('confirm-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', function () { modal.close(); });

  refresh();
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
    """None/빈 문자열 값은 빼고 쿼리스트링을 만든다 — 필터/페이지 링크를 조립할 때 쓴다."""
    import urllib.parse

    parts = [f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v not in (None, "")]
    return "&".join(parts)


def run_pipeline(apply: bool) -> dict:
    """`python -m mail_app.fetch_mail`(dry-run 또는 --apply) -> `-m mail_app.generate_html`
    순서로 동기 실행한다.

    자식 프로세스로 띄우는 이유는 이 두 CLI가 자기 완결적인 진입점으로 설계돼 있어서 —
    여기서 함수를 직접 import해서 부르는 것보다 실제 명령줄 실행과 동일한 경로를 타는 게
    더 안전하고, 스케줄러(run_daily.bat)가 실행하는 것과도 같은 코드 경로가 된다.
    generate_html 실행은 data/dashboard.html(Artifact 게시용 정적 파일)을 최신 상태로
    유지하기 위한 것 — 화면 자체는 build_report()를 직접 호출해서 그리므로 이 결과를
    기다릴 필요는 없지만, 부수효과로 계속 최신화해둔다.

    같은 인터프리터(sys.executable)로 실행하고 env(PYTHONPATH 등)를 그대로 물려주므로,
    이 프로세스에서 mail_app 을 import 할 수 있으면 자식도 할 수 있다. 데이터 경로는
    app_paths(MAIL_AGENT_DATA_DIR / 패키지 위치 기준)로 해석되어 cwd 와 무관하다.
    """
    python_exe = sys.executable
    fetch_args = [python_exe, "-m", "mail_app.fetch_mail"]
    if apply:
        fetch_args.append("--apply")

    result = {"ran_at": datetime.now().isoformat(timespec="seconds"), "apply": apply}
    try:
        fetch_proc = subprocess.run(
            fetch_args, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS
        )
        result["fetch_ok"] = fetch_proc.returncode == 0
        result["fetch_output"] = (fetch_proc.stdout + fetch_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        result["fetch_ok"] = False
        result["fetch_output"] = f"{RUN_TIMEOUT_SECONDS}초 넘게 걸려서 중단했습니다."
        result["generate_ok"] = False
        result["generate_output"] = "fetch가 실패해서 건너뜀."
        return result

    try:
        gen_proc = subprocess.run(
            [python_exe, "-m", "mail_app.generate_html"],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )
        result["generate_ok"] = gen_proc.returncode == 0
        result["generate_output"] = (gen_proc.stdout + gen_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        result["generate_ok"] = False
        result["generate_output"] = f"{RUN_TIMEOUT_SECONDS}초 넘게 걸려서 중단했습니다."
    return result


def run_status_html(run: dict | None) -> str:
    if run is None:
        return ""
    badge = lambda ok: f'<span class="badge {"ok" if ok else "fail"}">{"성공" if ok else "실패"}</span>'
    mode = "--apply (실제 처리)" if run["apply"] else "dry-run (미리보기만)"
    return f"""
    <div class="run-status">
      <strong>마지막 실행</strong> · {run['ran_at']} · {mode}<br>
      fetch_mail.py {badge(run['fetch_ok'])}
      <pre>{html.escape(run['fetch_output']) or '(출력 없음)'}</pre>
      generate_html.py {badge(run['generate_ok'])}
      <pre>{html.escape(run['generate_output']) or '(출력 없음)'}</pre>
    </div>
    """


def status_badges_html(m: dict) -> str:
    return generate_html.render_status_badge(m)


def msg_date_cell(iso: str) -> str:
    """message_date(ISO)를 날짜/시각 2줄로. 값이 없으면 빈 셀."""
    raw = (iso or "")[:16].replace("T", " ")
    if not raw:
        return '<td class="msg-date"></td>'
    date_part, _, time_part = raw.partition(" ")
    return (
        f'<td class="msg-date"><span class="d-date">{esc(date_part)}</span>'
        f'<span class="d-time">{esc(time_part)}</span></td>'
    )


def msg_table_row(m: dict, categories: dict, *, with_account: bool, with_select: bool = False) -> str:
    """메일 목록 테이블 한 행. 제목은 링크가 있으면 굵게(클릭 가능 표시), 상태는 별도 칸.

    with_select=True면 맨 앞에 선택 체크박스 칸을 붙인다(/tasks·/vault 대량 선택).
    체크박스 값(data-key)은 `account::uid` — 폼 제출/localStorage 키로 함께 쓴다.
    """
    cat_name = m.get("_category")
    cat_action = categories.get(cat_name, {}).get("action", "keep") if cat_name else "keep"
    pill_class = MSG_ACTION_PILL_CLASS.get(cat_action, "")
    subject = esc(m["subject"])[:120]
    web_link = m.get("web_link")
    if web_link:
        subject = f'<a href="{esc(web_link)}" target="_blank" rel="noopener">{subject}</a>'
    select_cell = ""
    if with_select:
        key = f'{m["account"]}::{m["uid"]}'
        # name/value/form="vault-form" 은 /vault의 폼 제출용(선택→되돌리기/영구삭제).
        # /tasks 에는 vault-form 이 없어 무해하게 무시되고, 그쪽은 JS가 data-key +
        # localStorage 로 페이지 간 선택을 관리한다.
        select_cell = (
            f'<td class="msg-select">'
            f'<input type="checkbox" class="sel-box" data-key="{esc(key)}" '
            f'name="sel" value="{esc(key)}" form="vault-form" aria-label="이 메일 선택"></td>'
        )
    account_cell = ""
    if with_account:
        provider = PROVIDER_LABEL.get(m.get("account_type"), m.get("account_type") or "")
        account_cell = f'<td class="msg-account">{esc(provider)} · {esc(m["account"])}</td>'
    status_html = status_badges_html(m) or '<span class="st-none">—</span>'
    cat_label = esc(cat_name or "미분류")
    return (
        f"<tr>"
        f"{select_cell}"
        f"{msg_date_cell(m.get('message_date'))}"
        f"{account_cell}"
        f'<td class="msg-cat"><span class="pill {pill_class}">{cat_label}</span></td>'
        f'<td class="msg-subj"><span class="subj">{subject}</span></td>'
        f'<td class="msg-status">{status_html}</td>'
        f'<td class="msg-sender">{esc(m["sender"])}</td>'
        f"</tr>"
    )


def msg_table(
    page_items: list[dict], categories: dict, *, with_account: bool, with_select: bool = False
) -> str:
    """<table class="msg-table"> 전체. 열 너비는 colgroup으로 고정(table-layout: fixed)해서
    데스크톱에선 제목/발신인/계정이 말줄임(…)으로 잘린다. 좁은 화면(테이블 min-width 미만)
    에선 .table-scroll 래퍼 안에서 테이블만 가로 스크롤 — 페이지 본문은 안 넘친다.

    with_select=True면 맨 앞에 체크박스 열을 추가한다(/tasks·/vault 대량 선택)."""
    if not page_items:
        return '<p class="empty">해당 조건의 메일이 없습니다. 검색어나 계정·카테고리 필터를 바꿔보세요.</p>'
    sel_col = '<col class="c-select">' if with_select else ""
    sel_head = '<th aria-label="선택"></th>' if with_select else ""
    if with_account:
        cols = (
            f'{sel_col}<col class="c-date"><col class="c-account"><col class="c-cat">'
            '<col class="c-subj"><col class="c-status"><col class="c-sender">'
        )
        head = f"{sel_head}<th>날짜</th><th>계정</th><th>카테고리</th><th>제목</th><th>상태</th><th>발신인</th>"
        table_cls = "msg-table msg-table--wide"
    else:
        cols = f'{sel_col}<col class="c-date"><col class="c-cat"><col class="c-subj"><col class="c-status"><col class="c-sender">'
        head = f"{sel_head}<th>날짜</th><th>카테고리</th><th>제목</th><th>상태</th><th>발신인</th>"
        table_cls = "msg-table"
    if with_select:
        table_cls += " msg-table--select"
    rows = "".join(
        msg_table_row(m, categories, with_account=with_account, with_select=with_select)
        for m in page_items
    )
    return (
        f'<div class="table-scroll"><table class="{table_cls}"><colgroup>{cols}</colgroup>'
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def render_sync_button(base_qs_params: dict) -> str:
    """"동기화" 버튼 — fetch_mail.py를 --since 없이(=계정별 app.db 마지막 저장 시점부터
    이어서, dry-run) 실행하고 지금 보던 화면(range/date)으로 돌아온다. 실제 메일함을
    바꾸는 --apply는 여기서 쓰지 않는다(그건 여전히 /tasks의 "실제 처리" 버튼 전용)."""
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in base_qs_params.items()
    )
    return f"""
    <form class="slow-form" method="post" action="/sync" style="margin:0">
      {hidden_fields}
      <button class="btn secondary" type="submit">동기화</button>
    </form>
    """


@app.route("/sync", methods=["POST"])
def sync_now():
    global LAST_RUN
    LAST_RUN = run_pipeline(apply=False)
    range_key = request.form.get("range", "daily")
    qs_params = {"range": range_key}
    if range_key in {"daily", "weekly", "monthly"}:
        qs_params["date"] = request.form.get("date", "")
    return redirect(f"/?{build_qs(**qs_params)}")


# ---------------------------------------------------------------------------
# 메인 대시보드 (/) — 일일/주간/월별/연도별/전체 탭
# ---------------------------------------------------------------------------

def parse_anchor_date(raw: str | None) -> datetime | None:
    """?date=YYYY-MM-DD 쿼리 파라미터를 파싱한다. 없거나 형식이 잘못되면 None
    (호출부가 오늘 날짜로 폴백한다)."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def normalize_anchor(range_key: str, raw: datetime) -> datetime:
    """range_key에 맞게 기준일을 정규화한다 — daily는 그날 0시, weekly는 그 주의
    월요일 0시, monthly는 그 달 1일 0시. 정규화해두면 shift_anchor()로 ±1칸씩 이동해도
    (월말 day-overflow 등) 항상 같은 종류의 기준일을 유지하고, "다음" 버튼을 막을
    시점(오늘 기준 기본 구간과 비교)도 정확히 계산할 수 있다."""
    day0 = datetime(raw.year, raw.month, raw.day)
    if range_key == "weekly":
        return day0 - timedelta(days=day0.weekday())
    if range_key == "monthly":
        return datetime(day0.year, day0.month, 1)
    return day0


def shift_anchor(range_key: str, anchor: datetime, steps: int) -> datetime:
    """정규화된 anchor를 이전/다음 한 칸(daily=1일, weekly=7일, monthly=1개월) 이동한다."""
    if range_key == "weekly":
        return anchor + timedelta(days=7 * steps)
    if range_key == "monthly":
        month_index = anchor.month - 1 + steps
        year = anchor.year + month_index // 12
        month = month_index % 12 + 1
        return datetime(year, month, 1)
    return anchor + timedelta(days=steps)


def range_bounds(range_key: str, anchor: datetime) -> tuple[datetime, datetime | None, str]:
    """range_key 탭의 [since, until) 구간과 화면에 보여줄 라벨을 만든다.

    daily/weekly/monthly는 anchor(normalize_anchor()로 정규화된 기준일)를 기준으로
    구간을 계산해서 화살표 네비게이션(render_period_nav)으로 과거 기간을 오갈 수 있게
    한다. yearly는 사용자가 이번 변경 범위에서 제외하기로 확정해서 기존과 동일하게
    항상 "올해"만 보여준다(anchor 무시, 이동 기능 없음)."""
    if range_key == "weekly":
        until = anchor + timedelta(days=7)
        label = f"{anchor.strftime('%Y-%m-%d')} ~ {(until - timedelta(days=1)).strftime('%Y-%m-%d')}"
        return anchor, until, label
    if range_key == "monthly":
        if anchor.month == 12:
            until = datetime(anchor.year + 1, 1, 1)
        else:
            until = datetime(anchor.year, anchor.month + 1, 1)
        return anchor, until, anchor.strftime("%Y-%m")
    if range_key == "yearly":
        now = datetime.now()
        return datetime(now.year, 1, 1), None, "올해"
    until = anchor + timedelta(days=1)
    return anchor, until, anchor.strftime("%Y-%m-%d")


def render_period_nav(range_key: str, anchor: datetime) -> str:
    """일일/주간/월간 탭에서 이전/다음 기간으로 이동하는 화살표 네비게이션.
    "다음"은 오늘 기준 기본 구간(현재)보다 미래로는 못 가게 막는다 — 그 이후엔 어차피
    저장된 메일이 없다."""
    now = datetime.now()
    today0 = datetime(now.year, now.month, now.day)
    current_default = normalize_anchor(range_key, today0)
    prev_anchor = shift_anchor(range_key, anchor, -1)
    _, _, label = range_bounds(range_key, anchor)

    prev_qs = build_qs(range=range_key, date=prev_anchor.strftime("%Y-%m-%d"))
    prev_link = f'<a href="/?{prev_qs}">← 이전</a>'
    if anchor >= current_default:
        next_link = '<span class="disabled">다음 →</span>'
    else:
        next_anchor = shift_anchor(range_key, anchor, 1)
        next_qs = build_qs(range=range_key, date=next_anchor.strftime("%Y-%m-%d"))
        next_link = f'<a href="/?{next_qs}">다음 →</a>'
    return f'<div class="period-nav">{prev_link}<span class="period-label">{esc(label)}</span>{next_link}</div>'


def render_range_tabs(active_range: str) -> str:
    links = []
    for key, label in RANGE_TABS:
        cls = ' class="active"' if key == active_range else ""
        links.append(f'<a href="/?range={key}"{cls}>{label}</a>')
    return f'<div class="range-tabs">{"".join(links)}</div>'


def load_all_messages(
    since: datetime,
    until: datetime | None,
    account_filter: str | None,
    status: str | None = None,
):
    """계정 필터를 적용해 메시지를 모으고 카테고리 분류를 붙여서 반환한다.
    (/의 전체 탭과 /tasks, /vault가 공유하는 로직.)

    status를 넘기면 그 status인 행만 조회한다 — /vault(보관함=archived / 휴지통=trashed).
    """
    accounts_cfg = load_accounts(ACCOUNTS_PATH)
    account_type_by_user = {a["user"]: a["type"] for a in accounts_cfg}
    all_users = sorted(account_type_by_user) or sorted(distinct_accounts(DB_PATH, since, until))
    target_users = [account_filter] if account_filter else all_users

    all_messages: list[dict] = []
    for user in target_users:
        msgs = query_messages(DB_PATH, since, until, account=user, status=status)
        atype = account_type_by_user.get(user) or account_type_for(DB_PATH, user)
        for m in msgs:
            m["account_type"] = atype
        all_messages.extend(msgs)

    categories = config_store.load_categories(DB_PATH)
    classified = classify(all_messages, categories, sample_cap=None)
    category_of: dict[tuple[str, str], str] = {}
    for name, matches in classified["category_matches"].items():
        for m in matches:
            category_of[(m["account"], m["uid"])] = name
    for m in all_messages:
        m["_category"] = category_of.get((m["account"], m["uid"]))

    return all_messages, all_users, account_type_by_user, categories


def render_pagination(prev_link: str, next_link: str, page: int, total_pages: int, total: int) -> str:
    """목록 하단 페이지네이션 한 줄. prev_link/next_link 는 이미 완성된 <a>/<span> 문자열.

    목록 위·아래에 두 번 찍던 걸 아래 한 번으로 줄였다(사용자 요청) — 위쪽 건 필터 폼
    바로 아래라 오히려 시선을 흐렸다."""
    return (
        f'<div class="pagination">{prev_link}'
        f'<span class="page-label">{page} / {total_pages} 페이지 · 총 {total}건</span>'
        f'{next_link}</div>'
    )


def render_message_list(
    since: datetime,
    until: datetime | None,
    list_route: str,
    base_params: dict,
    note: str,
) -> str:
    """계정/카테고리 필터 + 페이지네이션으로 [since, until) 기간의 메일 목록을 보여준다.

    "전체" 탭(`/`)과 일일/주간/월간 목록 페이지(`/list`)가 이 함수 하나를 공유한다 —
    since/until만 다르고 필터·페이지네이션 로직은 완전히 동일하기 때문. list_route/
    base_params는 필터폼 action과 페이지 링크에 실어야 하는 고정 쿼리(예: range=all,
    또는 range=daily&date=2026-08-24)를 결정한다."""
    try:
        page_num = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page_num = 1
    try:
        page_size = int(request.args.get("page_size", str(MSG_PAGE_SIZE_DEFAULT)))
    except ValueError:
        page_size = MSG_PAGE_SIZE_DEFAULT
    page_size = max(MSG_PAGE_SIZE_MIN, min(MSG_PAGE_SIZE_MAX, page_size))

    account_filter = request.args.get("account", "").strip() or None
    category_filter = request.args.get("category", "").strip() or None
    query = request.args.get("q", "").strip()

    all_messages, all_users, account_type_by_user, categories = load_all_messages(since, until, account_filter)

    if category_filter == "__uncategorized__":
        pool = [m for m in all_messages if m["_category"] is None]
    elif category_filter:
        pool = [m for m in all_messages if m["_category"] == category_filter]
    else:
        pool = all_messages
    if query:
        q = query.lower()
        pool = [m for m in pool if q in (m.get("subject") or "").lower() or q in (m.get("sender") or "").lower()]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    page_num = min(page_num, total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    filter_state = {
        "account": account_filter or "",
        "category": category_filter or "",
        "q": query,
        "page_size": page_size,
    }

    account_options = '<option value="">전체 계정</option>' + "".join(
        f'<option value="{esc(u)}"{" selected" if u == account_filter else ""}>'
        f'{esc(PROVIDER_LABEL.get(account_type_by_user.get(u, ""), ""))} · {esc(u)}</option>'
        for u in all_users
    )
    category_options = (
        '<option value="">전체 카테고리</option>'
        '<option value="__uncategorized__"' + (' selected' if category_filter == "__uncategorized__" else "") + '>미분류</option>'
        + "".join(
            f'<option value="{esc(name)}"{" selected" if name == category_filter else ""}>{esc(name)}</option>'
            for name in categories
        )
    )
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in base_params.items()
    )
    filter_form = f"""
    <form class="filter-form" method="get" action="{list_route}">
      {hidden_fields}
      <label>검색<input type="search" name="q" value="{esc(query)}" placeholder="발신인 · 제목 키워드"></label>
      <label>계정<select name="account">{account_options}</select></label>
      <label>카테고리<select name="category">{category_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
      <a class="btn secondary" href="/tasks?{build_qs(**filter_state)}">이 조건으로 작업 실행 →</a>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True)

    prev_qs = build_qs(**filter_state, **base_params, page=page_num - 1)
    next_qs = build_qs(**filter_state, **base_params, page=page_num + 1)
    prev_link = f'<a href="{list_route}?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="{list_route}?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    return f"""
    <p class="sub">{esc(note)}</p>
    {filter_form}
    {table}
    {pagination}
    """


def render_account_inline_list(
    user: str,
    account_type: str,
    since: datetime,
    until: datetime | None,
    categories: dict,
    selected_cat: str,
    page: int,
    base_qs_params: dict,
    anchor: str,
) -> str:
    """펼쳐진 계정 카드 안쪽 — 카테고리 필터 칩("전체" + 건수 상위 몇 개 + "···") +
    실제 페이지네이션 목록. sample_cap=None으로 이 계정만 다시 분류한다 — build_report()의
    전체 리포트는 미분류 샘플을 20건으로 캡해두는데(대시보드 요약용), 여기서는 미분류를
    선택해도 페이지네이션이 20건에서 끊기면 안 되기 때문이다."""
    msgs = query_messages(DB_PATH, since, until, account=user)
    for m in msgs:
        m["account_type"] = account_type
    classified = classify(msgs, categories, sample_cap=None)

    category_of: dict[str, str] = {}
    for name, matches in classified["category_matches"].items():
        for m in matches:
            category_of[m["uid"]] = name
    for m in msgs:
        m["_category"] = category_of.get(m["uid"])

    if selected_cat == "__uncategorized__":
        pool = [m for m in msgs if m["_category"] is None]
    elif selected_cat:
        pool = [m for m in msgs if m["_category"] == selected_cat]
    else:
        pool = msgs
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / ACCOUNT_LIST_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * ACCOUNT_LIST_PAGE_SIZE
    page_items = pool[start : start + ACCOUNT_LIST_PAGE_SIZE]

    def filter_link(label_text: str, value: str, count: int, active: bool) -> str:
        qs = build_qs(**base_qs_params, acct=user, acct_cat=value, acct_page=1)
        cls = "acct-filter-chip active" if active else "acct-filter-chip"
        return f'<a class="{cls}" href="/?{qs}#{anchor}">{esc(label_text)} <span class="chip-value">{count}</span></a>'

    cat_counts = [(name, classified["categories"][name]["count"]) for name in classified["categories"]]
    if classified["uncategorized_count"]:
        cat_counts.append(("미분류", classified["uncategorized_count"]))
    cat_counts.sort(key=lambda t: (-t[1], t[0]))

    filter_items = [
        filter_link(
            name,
            "__uncategorized__" if name == "미분류" else name,
            count,
            selected_cat == ("__uncategorized__" if name == "미분류" else name),
        )
        for name, count in cat_counts
        if count
    ]
    capped_filters = generate_html.render_capped(
        filter_items, generate_html.ACCOUNT_CHIP_CAP, f"{anchor}-filter-more",
        "acct-filter-chip-extra", "acct-filter-chip acct-filter-more",
    )
    filter_row = (
        f'<div class="acct-filter-row">'
        f'{filter_link("전체", "", len(msgs), not selected_cat)}'
        f'{capped_filters}'
        f'</div>'
    )

    table = msg_table(page_items, categories, with_account=False)

    def page_qs(p: int) -> str:
        return build_qs(**base_qs_params, acct=user, acct_cat=selected_cat or "", acct_page=p)

    prev_link = f'<a href="/?{page_qs(page - 1)}#{anchor}">← 이전</a>' if page > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/?{page_qs(page + 1)}#{anchor}">다음 →</a>' if page < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page, total_pages, total)

    return f'{filter_row}{table}{pagination}'


def render_live_account_card(
    user: str,
    account_data: dict,
    account_type: str,
    since: datetime,
    until: datetime | None,
    categories: dict,
    is_open: bool,
    base_qs_params: dict,
    selected_cat: str,
    page: int,
) -> str:
    """계정 카드 하나. 접혀 있으면 상위 4개 카테고리 미니칩 요약만(정적 Artifact와 같은
    모양), 펼쳐져 있으면(?acct= 쿼리파라미터가 이 계정과 일치) 그 아래 실제 페이지네이션
    목록까지 보여준다. 한 번에 하나의 계정만 열 수 있다 — 다른 계정 카드를 클릭하면
    URL의 acct 값이 바뀌면서 이전 계정은 자동으로 닫힌다(서버 렌더링만으로 동작, JS 없음)."""
    anchor = generate_html.account_anchor_id(user)
    label = PROVIDER_LABEL.get(account_type, account_type or "")
    total = account_data["total"]

    cat_counts = [(name, account_data["categories"][name]["count"]) for name in account_data["categories"]]
    uncategorized_count = account_data.get("uncategorized_count", 0)
    if uncategorized_count:
        cat_counts.append(("미분류", uncategorized_count))
    cat_counts.sort(key=lambda t: (-t[1], t[0]))

    mini_items = []
    for name, count in cat_counts:
        color_class = (
            "muted" if name == "미분류"
            else generate_html.ACTION_COLOR_CLASS.get(account_data["categories"][name]["action"], "muted")
        )
        mini_items.append(generate_html.render_mini_stat(name, count, color_class))
    mini_stats = generate_html.render_mini_stat("총", total) + generate_html.render_capped(
        mini_items, generate_html.ACCOUNT_CHIP_CAP, f"{anchor}-mini-more", "mini-stat-extra", "mini-stat msw3 mini-stat-more"
    )

    if not is_open:
        open_qs = build_qs(**base_qs_params, acct=user)
        return f"""
        <div class="account-detail" id="{anchor}">
          <a class="account-summary-link" href="/?{open_qs}#{anchor}">
            <span class="account-name">{esc(label)} · {esc(user)}</span>
            <span class="account-mini-stats">{mini_stats}</span>
            <span class="chevron">▸</span>
          </a>
        </div>
        """

    close_qs = build_qs(**base_qs_params)
    body = render_account_inline_list(
        user, account_type, since, until, categories, selected_cat, page, base_qs_params, anchor
    )
    return f"""
    <div class="account-detail open" id="{anchor}">
      <a class="account-summary-link" href="/?{close_qs}#{anchor}">
        <span class="account-name">{esc(label)} · {esc(user)}</span>
        <span class="account-mini-stats">{mini_stats}</span>
        <span class="chevron">▸</span>
      </a>
      <div class="account-detail-body">{body}</div>
    </div>
    """


def render_dashboard_report(range_key: str, since: datetime, until: datetime | None, label: str, base_qs_params: dict) -> str:
    """리포트 화면(헤더+동기화 버튼/통계/액션/카테고리별 상세)은 generate_html.py의
    조각 함수들을 그대로 재사용하고, "계정별 상세"만 실시간 페이지네이션이 가능한
    이 파일만의 버전(render_live_account_card)으로 바꿔치기한다."""
    try:
        report = generate_html.build_report(since, until)
    except SystemExit:
        return (
            f'<p class="empty">{esc(label)} 기간에 조회된 메일이 없습니다. '
            f'"작업 실행"에서 새로고침을 먼저 실행해보세요.</p>'
        )

    header = generate_html.render_report_header(report, render_sync_button(base_qs_params))
    stats = generate_html.render_report_stats(report)
    actions = generate_html.render_report_actions(report)
    cats = generate_html.render_report_categories(report)

    accounts_cfg = {a["user"]: a["type"] for a in load_accounts(ACCOUNTS_PATH)}
    per_account = report.get("per_account", {})
    accounts = generate_html.sorted_accounts(report)
    open_acct = request.args.get("acct", "").strip() or None
    selected_cat = request.args.get("acct_cat", "").strip()
    try:
        acct_page = max(1, int(request.args.get("acct_page", "1")))
    except ValueError:
        acct_page = 1
    categories = config_store.load_categories(DB_PATH)

    account_cards = "".join(
        render_live_account_card(
            p, per_account[p], accounts_cfg.get(p) or per_account[p].get("type"),
            since, until, categories,
            p == open_acct, base_qs_params, selected_cat, acct_page,
        )
        for p in accounts
    )
    # 계정 카드를 세로로 쌓지 않고 좌우 캐러셀(가로 스크롤 + ‹/› 버튼)로 보여준다.
    # 접고/펼치기(?acct=)는 그대로 서버가 관리 — 펼쳐진 카드로는 로드 시 JS가 스크롤한다.
    open_attr = f' data-open-acct="{esc(generate_html.account_anchor_id(open_acct))}"' if open_acct else ""
    accounts_html = (
        '<h2 class="section-title">계정별 상세</h2>'
        f'<div class="account-carousel"{open_attr}>'
        '<div class="carousel-bar">'
        '<button type="button" class="carousel-nav prev" aria-label="이전 계정" disabled>‹</button>'
        '<button type="button" class="carousel-nav next" aria-label="다음 계정" disabled>›</button>'
        '</div>'
        f'<div class="account-track">{account_cards}</div>'
        '</div>'
    )

    return header + stats + actions + cats + accounts_html + CAROUSEL_SCRIPT


@app.route("/")
def dashboard_page():
    range_key = request.args.get("range", "daily")
    if range_key not in {k for k, _ in RANGE_TABS}:
        range_key = "daily"

    period_nav_html = ""
    list_qs: str | None = None

    if range_key in {"daily", "weekly", "monthly"}:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_qs_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        period_nav_html = render_period_nav(range_key, anchor)
        list_qs = build_qs(**base_qs_params)
    elif range_key == "yearly":
        # 화살표 이동 없이 기존과 동일하게 "올해"만 보여준다(요청 범위 밖) — 목록 링크도 없음.
        since, until, label = range_bounds("yearly", datetime.now())
        base_qs_params = {"range": "yearly"}
    else:  # all — 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 기준 통계.
        since, until, label = EPOCH_START, None, "전체(누적)"
        base_qs_params = {"range": "all"}
        list_qs = "range=all"

    content = render_dashboard_report(range_key, since, until, label, base_qs_params)
    list_link_html = (
        f'<a class="period-list-link" href="/list?{list_qs}">→ 이 기간 목록 보기 (페이지네이션)</a>'
        if list_qs else ""
    )
    body = f'{render_range_tabs(range_key)}{period_nav_html}{content}{list_link_html}'
    return page("메일 대시보드", body, "dashboard")


@app.route("/list")
def message_list_page():
    """일일/주간/월간/전체 각 기간의 메일을 계정/카테고리 필터 + 페이지네이션으로
    보여주는 별도 화면. 리포트 화면(카테고리별 요약)과 달리 개별 메일 행을 그대로
    나열한다 — "전체" 탭(`/`)이 원래 하던 걸 render_message_list()로 일반화해서
    daily/weekly/monthly 기간에도 재활용한다."""
    range_key = request.args.get("range", "daily")
    if range_key not in {"daily", "weekly", "monthly", "all"}:
        range_key = "daily"

    if range_key == "all":
        since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
        until = None
        base_params = {"range": "all"}
        note = f"최근 {MSG_DEFAULT_SINCE_DAYS}일(약 2년)간 저장된 전체 메일입니다."
        period_nav = ""
        back_qs = "range=all"
    else:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        note = f"{label} 기간의 메일 목록입니다."
        period_nav = render_period_nav(range_key, anchor)
        back_qs = build_qs(**base_params)

    content = render_message_list(since, until, "/list", base_params, note)
    body = (
        f'<p class="nav"><a class="back-link" href="/?{back_qs}">← 리포트로 돌아가기</a></p>'
        f'{period_nav}{content}'
    )
    return page("메일 목록", body, "dashboard")


# ---------------------------------------------------------------------------
# 설정 (/settings) — 카테고리 관리 + 메일 계정 관리
# ---------------------------------------------------------------------------

def _delete_form(delete_url: str, confirm_msg: str) -> str:
    # 삭제 폼은 반드시 수정 폼 바깥의 형제 요소여야 한다 — <form> 안에 <form>을 중첩하면
    # 브라우저가 중첩을 무시하고 안쪽 폼 제출을 바깥 폼으로 흡수해버려서, 예전엔 "삭제"를
    # 눌러도 실제로는 수정(업데이트) 폼이 제출되는 버그가 있었다.
    return (
        f'<form class="cfg-delete" method="post" action="{delete_url}" '
        f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
        f'<button class="btn danger" type="submit">삭제</button></form>'
    )


def category_fields(action_url: str, submit_label: str, category: dict | None, delete_url: str | None) -> str:
    """카테고리 추가/수정 폼의 알맹이 (페이지 래퍼 없음). /settings의 인라인 편집과
    /settings/categories/{new,edit} 폴백 페이지가 함께 쓴다."""
    category = category or {
        "name": "", "description": "", "action": "keep", "priority": "NORMAL",
        "senders": [], "domains": [], "title": [], "contents": [],
    }
    category.setdefault("domains", [])
    name_field = (
        f'<input type="text" name="name" value="{esc(category["name"])}" required placeholder="영문 소문자 키">'
        if not delete_url
        else f'<input type="text" value="{esc(category["name"])}" disabled>'
    )
    action_options = "".join(
        f'<option value="{a}"{" selected" if a == category["action"] else ""}>{a}</option>' for a in ACTIONS
    )
    priority_options = "".join(
        f'<option value="{p}"{" selected" if p == category["priority"] else ""}>{p}</option>' for p in PRIORITIES
    )
    form = f"""
    <form class="cfg-form" method="post" action="{action_url}">
      <div class="row">
        <label><span>이름 (규칙 키) <b class="req">*</b></span>{name_field}</label>
        <label>description<input type="text" name="description" value="{esc(category['description'])}"></label>
      </div>
      <div class="row">
        <label>action<select name="action">{action_options}</select>
          <span class="hint">trash=휴지통 / save=보관 / read=읽음 / keep=그대로</span>
        </label>
        <label>priority<select name="priority">{priority_options}</select>
          <span class="hint">동률이면 먼저 만든 쪽 우선</span>
        </label>
      </div>
      <label>keywords.senders — 완전 일치하는 전체 이메일 주소, 한 줄에 하나
        <textarea name="senders" placeholder="noreply@example.com">{esc(kw_to_text(category['senders']))}</textarea></label>
      <label>keywords.domains — 발신 도메인(서브도메인 포함), 한 줄에 하나. 한 도메인을 한 카테고리가 독점할 때만
        <textarea name="domains" placeholder="lguplus.co.kr">{esc(kw_to_text(category['domains']))}</textarea></label>
      <label>keywords.title — 제목 부분 문자열, 한 줄에 하나
        <textarea name="title">{esc(kw_to_text(category['title']))}</textarea></label>
      <label>keywords.contents — 본문 부분 문자열(있으면 본문 추가 조회 발생)
        <textarea name="contents">{esc(kw_to_text(category['contents']))}</textarea></label>
      <div class="actions-row"><button class="btn" type="submit">{submit_label}</button></div>
    </form>
    """
    if delete_url:
        form += _delete_form(delete_url, f"{category['name']} 카테고리를 삭제할까요?")
    return form


def account_fields(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    account = account or {"type": "gmail", "user": "", "password": ""}
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == account["type"] else ""}>{PROVIDER_LABEL.get(t, t)}</option>'
        for t in IMAP_SERVERS
    )
    form = f"""
    <form class="cfg-form" method="post" action="{action_url}">
      <div class="row">
        <label>메일 종류<select name="type">{type_options}</select></label>
        <label><span>이메일 주소 <b class="req">*</b></span><input type="email" name="user" value="{esc(account['user'])}" autocomplete="username" required></label>
      </div>
      <label><span>앱 비밀번호 <b class="req">*</b></span>
        <span class="hint">2단계 인증 후 발급 권장 · config/accounts.yaml에 평문 저장</span>
        <input type="password" name="password" value="{esc(account['password'])}" autocomplete="off" required></label>
      <div class="actions-row"><button class="btn" type="submit">{submit_label}</button></div>
    </form>
    """
    if delete_url:
        form += _delete_form(delete_url, f"{account['user']} 계정을 삭제할까요?")
    return form


def _cfg_page(submit_label: str, fields_html: str, hint: str) -> str:
    """인라인 편집을 못 쓰는 경우(직접 URL 접근)용 폴백 페이지."""
    body = (
        f'<p class="nav"><a class="back-link" href="/settings">← 설정</a></p>'
        f'<h1 class="page-title">{esc(submit_label)}</h1>'
        f'<p class="sub">{hint}</p>'
        f'<div class="card">{fields_html}</div>'
    )
    return page(submit_label, body, "settings")


def category_form(action_url: str, submit_label: str, category: dict | None, delete_url: str | None) -> str:
    return _cfg_page(
        submit_label,
        category_fields(action_url, submit_label, category, delete_url),
        "senders는 완전 일치 이메일 주소, title/contents는 부분 문자열 키워드 (한 줄에 하나).",
    )


def account_form(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    return _cfg_page(
        submit_label,
        account_fields(action_url, submit_label, account, delete_url),
        "앱 비밀번호 사용을 권장합니다. config/accounts.yaml에 평문으로 저장됩니다 — 로컬 전용 도구.",
    )


def _cfg_add(summary: str, fields_html: str, is_open: bool = False) -> str:
    return (
        f'<details class="cfg-item cfg-add"{" open" if is_open else ""}>'
        f'<summary><span class="cfg-add-label">{esc(summary)}</span></summary>'
        f'<div class="cfg-body">{fields_html}</div></details>'
    )


def _cfg_item(summary_cells: str, fields_html: str) -> str:
    return (
        f'<details class="cfg-item">'
        f'<summary>{summary_cells}<span class="cfg-chevron">▸</span></summary>'
        f'<div class="cfg-body">{fields_html}</div></details>'
    )


def _err_banner(msg: str) -> str:
    return f'<div class="form-error" role="alert" tabindex="-1">{esc(msg)}</div>'


def _form_to_category(form) -> dict:
    return {
        "name": (form.get("name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "action": form.get("action") or "keep",
        "priority": form.get("priority") or "NORMAL",
        "senders": text_to_kw(form.get("senders", "")),
        "domains": text_to_kw(form.get("domains", "")),
        "title": text_to_kw(form.get("title", "")),
        "contents": text_to_kw(form.get("contents", "")),
    }


def _form_to_account(form) -> dict:
    return {
        "type": form.get("type") or "gmail",
        "user": (form.get("user") or "").strip(),
        "password": form.get("password") or "",
    }


def _category_section(error: dict | None = None) -> str:
    categories = config_store.list_categories(DB_PATH)
    add_prefill = _form_to_category(error["form"]) if error and error.get("form") else None
    banner = _err_banner(error["msg"]) if error else ""
    items = _cfg_add(
        "+ 카테고리 추가",
        category_fields("/settings/categories", "추가", add_prefill, None),
        is_open=add_prefill is not None,
    )
    for c in categories:
        summary = (
            f'<span class="cfg-main"><span class="cfg-name">{esc(c["name"])}</span>'
            f'<span class="cfg-desc">{esc(c["description"])}</span></span>'
            f'<span class="pill {c["action"]}">{c["action"]}</span>'
            f'<span class="cfg-tag">{c["priority"]}</span>'
            f'<span class="cfg-tag cfg-counts" title="senders / domains / title / contents">'
            f'{len(c["senders"])} / {len(c.get("domains", []))} / {len(c["title"])} / {len(c["contents"])}</span>'
        )
        items += _cfg_item(
            summary,
            category_fields(f"/settings/categories/{c['name']}", "저장",
                            c, f"/settings/categories/{c['name']}/delete"),
        )
    return (
        f'{banner}'
        f'<div class="cfg-toolbar">'
        f'<form method="post" action="/settings/categories/export" style="margin:0">'
        f'<button class="btn secondary" type="submit">categories.json으로 내보내기</button></form>'
        f'</div>'
        f'<div class="cfg-list">{items}</div>'
    )


def _accounts_section(error: dict | None = None) -> str:
    accounts_list = load_accounts(ACCOUNTS_PATH)
    if env_mode():
        rows = "".join(
            f'<div class="cfg-item cfg-static"><div class="cfg-summary-static">'
            f'<span class="cfg-name">{esc(PROVIDER_LABEL.get(a["type"], a["type"]))}</span>'
            f'<span class="cfg-desc">{esc(a["user"])}</span></div></div>'
            for a in accounts_list
        )
        return (
            '<p class="sub">계정은 데스크톱 앱의 <strong>계정 설정</strong> 창에서 관리됩니다 '
            '(자격증명은 암호화 볼트에 저장). 여기서는 편집할 수 없습니다.</p>'
            f'<div class="cfg-list">{rows}</div>'
            if accounts_list else '<p class="empty">연결된 메일 계정이 없습니다.</p>'
        )
    add_prefill = _form_to_account(error["form"]) if error and error.get("form") else None
    banner = _err_banner(error["msg"]) if error else ""
    items = _cfg_add(
        "+ 계정 추가",
        account_fields("/settings/accounts", "추가", add_prefill, None),
        is_open=add_prefill is not None,
    )
    for a in accounts_list:
        summary = (
            f'<span class="cfg-main"><span class="cfg-name">{esc(a["user"])}</span>'
            f'<span class="cfg-desc">{esc(PROVIDER_LABEL.get(a["type"], a["type"]))}</span></span>'
        )
        items += _cfg_item(
            summary,
            account_fields(f"/settings/accounts/{esc(a['user'])}", "저장",
                           a, f"/settings/accounts/{esc(a['user'])}/delete"),
        )
    return f'{banner}<div class="cfg-list">{items}</div>'


@app.route("/settings")
def settings_page(cat_error: dict | None = None, acc_error: dict | None = None):
    tabs = generate_html.render_tab_group(
        "settings",
        [
            ("카테고리", "", _category_section(cat_error)),
            ("메일 계정", "", _accounts_section(acc_error)),
        ],
        1 if acc_error else 0,
    )
    body = f"""
    <h1 class="page-title">⚙️ 설정</h1>
    <p class="sub">"수정"/"추가"를 누르면 그 자리에서 펼쳐집니다. 저장하면 다음 파이프라인
    실행부터 바로 반영됩니다 (data/app.db, config/accounts.yaml).</p>
    {tabs}
    """
    return page("설정", body, "settings")


def _desktop_accounts_page():
    return page(
        "계정 편집 불가",
        "<p class='empty'>이 앱은 자격증명을 암호화 볼트에서 읽고 있습니다. "
        "계정 추가/수정/삭제는 데스크톱 앱의 <strong>계정 설정</strong> 창에서 하세요. "
        "<a href='/settings'>← 설정</a></p>",
        "settings",
    ), 403


@app.route("/settings/categories/new", methods=["GET"])
def new_category_form():
    return category_form("/settings/categories", "카테고리 추가", None, None)


@app.route("/settings/categories", methods=["POST"])
def create_category():
    name = request.form.get("name", "").strip()
    if not name:
        return settings_page(cat_error={"msg": "카테고리 이름을 입력하세요.", "form": request.form}), 400
    if config_store.get_category(DB_PATH, name):
        return settings_page(cat_error={
            "msg": f"'{name}'은(는) 이미 있는 카테고리입니다. 다른 이름을 쓰거나 기존 항목을 수정하세요.",
            "form": request.form,
        }), 400
    config_store.add_category(
        DB_PATH,
        name=name,
        description=request.form.get("description", "").strip(),
        action=request.form.get("action", "keep"),
        priority=request.form.get("priority", "NORMAL"),
        senders=text_to_kw(request.form.get("senders", "")),
        domains=text_to_kw(request.form.get("domains", "")),
        title=text_to_kw(request.form.get("title", "")),
        contents=text_to_kw(request.form.get("contents", "")),
    )
    return redirect(url_for("settings_page"))


@app.route("/settings/categories/<name>/edit", methods=["GET"])
def edit_category_form(name: str):
    category = config_store.get_category(DB_PATH, name)
    if not category:
        return page("찾을 수 없음", "<p class='empty'>그런 카테고리가 없습니다. <a href='/settings'>설정으로</a></p>", "settings"), 404
    return category_form(f"/settings/categories/{name}", f"'{name}' 수정", category, f"/settings/categories/{name}/delete")


@app.route("/settings/categories/<name>", methods=["POST"])
def update_category(name: str):
    if not config_store.get_category(DB_PATH, name):
        return settings_page(cat_error={"msg": f"'{name}' 카테고리를 찾을 수 없습니다.", "form": None}), 404
    config_store.update_category(
        DB_PATH,
        name=name,
        description=request.form.get("description", "").strip(),
        action=request.form.get("action", "keep"),
        priority=request.form.get("priority", "NORMAL"),
        senders=text_to_kw(request.form.get("senders", "")),
        domains=text_to_kw(request.form.get("domains", "")),
        title=text_to_kw(request.form.get("title", "")),
        contents=text_to_kw(request.form.get("contents", "")),
    )
    return redirect(url_for("settings_page"))


@app.route("/settings/categories/<name>/delete", methods=["POST"])
def delete_category(name: str):
    config_store.delete_category(DB_PATH, name)
    return redirect(url_for("settings_page"))


@app.route("/settings/categories/export", methods=["POST"])
def export_json():
    config_store.export_to_json(DB_PATH, JSON_EXPORT_PATH)
    return redirect(url_for("settings_page"))


@app.route("/settings/accounts/new", methods=["GET"])
def new_account_form():
    if env_mode():
        return _desktop_accounts_page()
    return account_form("/settings/accounts", "계정 추가", None, None)


@app.route("/settings/accounts", methods=["POST"])
def create_account():
    if env_mode():
        return _desktop_accounts_page()
    typ = request.form.get("type", "")
    user = request.form.get("user", "").strip()
    password = request.form.get("password", "")
    if typ not in IMAP_SERVERS:
        return settings_page(acc_error={"msg": "메일 종류를 선택하세요.", "form": request.form}), 400
    if not user or not password:
        return settings_page(acc_error={"msg": "이메일 주소와 앱 비밀번호를 모두 입력하세요.", "form": request.form}), 400
    if any(a["user"] == user for a in load_accounts(ACCOUNTS_PATH)):
        return settings_page(acc_error={"msg": f"'{user}'은(는) 이미 등록된 계정입니다.", "form": request.form}), 400
    add_account(ACCOUNTS_PATH, type=typ, user=user, password=password)
    return redirect(url_for("settings_page"))


@app.route("/settings/accounts/<user>/edit", methods=["GET"])
def edit_account_form(user: str):
    if env_mode():
        return _desktop_accounts_page()
    accounts_list = load_accounts(ACCOUNTS_PATH)
    account = next((a for a in accounts_list if a["user"] == user), None)
    if not account:
        return page("찾을 수 없음", "<p class='empty'>그런 계정이 없습니다. <a href='/settings'>설정으로</a></p>", "settings"), 404
    return account_form(f"/settings/accounts/{user}", f"'{user}' 수정", account, f"/settings/accounts/{user}/delete")


@app.route("/settings/accounts/<user>", methods=["POST"])
def update_account_route(user: str):
    if env_mode():
        return _desktop_accounts_page()
    typ = request.form.get("type", "")
    new_user = request.form.get("user", "").strip()
    password = request.form.get("password", "")
    if typ not in IMAP_SERVERS:
        return settings_page(acc_error={"msg": "메일 종류를 선택하세요.", "form": request.form}), 400
    if not new_user or not password:
        return settings_page(acc_error={"msg": "이메일 주소와 앱 비밀번호를 모두 입력하세요.", "form": request.form}), 400
    update_account(ACCOUNTS_PATH, original_user=user, type=typ, user=new_user, password=password)
    return redirect(url_for("settings_page"))


@app.route("/settings/accounts/<user>/delete", methods=["POST"])
def delete_account_route(user: str):
    if env_mode():
        return _desktop_accounts_page()
    delete_account(ACCOUNTS_PATH, user)
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# 작업 실행 (/tasks) — 파이프라인 실행 버튼 + 목록 인라인 대량 선택(localStorage로
# 페이지 간 유지) + 실행 전 요약 확인 모달
# ---------------------------------------------------------------------------

def render_task_action_status(run: dict | None) -> str:
    if run is None:
        return ""
    rows = []
    for r in run["results"]:
        fail_note = f", {r['failed']}건 실패" if r["failed"] else ""
        extra_note = f" ({esc(r['note'])})" if r.get("note") else ""
        rows.append(f"<div>{esc(r['account'])} · {esc(r['action'])} — {r['done']}건 완료{fail_note}{extra_note}</div>")
    return f"""
    <div class="run-status">
      <strong>액션 처리 결과</strong> · {run['ran_at']}
      {"".join(rows) or '<div>처리한 항목이 없습니다.</div>'}
    </div>
    """


def render_tasks_page(
    page_items: list[dict], page_num: int, total_pages: int, total: int, page_size: int,
    account_filter: str | None, category_filter: str | None,
    all_users: list[str], account_type_by_user: dict[str, str], categories: dict,
) -> str:
    filter_state = {"account": account_filter or "", "category": category_filter or "", "page_size": page_size}
    return_qs = build_qs(**filter_state)

    account_options = '<option value="">전체 계정</option>' + "".join(
        f'<option value="{esc(u)}"{" selected" if u == account_filter else ""}>'
        f'{esc(PROVIDER_LABEL.get(account_type_by_user.get(u, ""), ""))} · {esc(u)}</option>'
        for u in all_users
    )
    category_options = (
        '<option value="">전체 카테고리</option>'
        '<option value="__uncategorized__"' + (' selected' if category_filter == "__uncategorized__" else "") + '>미분류</option>'
        + "".join(
            f'<option value="{esc(name)}"{" selected" if name == category_filter else ""}>{esc(name)}</option>'
            for name in categories
        )
    )
    filter_form = f"""
    <form class="filter-form" method="get" action="/tasks">
      <label>계정<select name="account">{account_options}</select></label>
      <label>카테고리<select name="category">{category_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True, with_select=True)
    prev_qs = build_qs(**filter_state, page=page_num - 1)
    next_qs = build_qs(**filter_state, page=page_num + 1)
    prev_link = f'<a href="/tasks?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/tasks?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    task_result_json = json.dumps(
        {"failed_keys": (LAST_TASK_ACTION or {}).get("failed_keys", [])}
    ).replace("<", "\\u003c")

    body = f"""
    <h1 class="page-title">작업 실행</h1>
    <p class="sub">새로고침은 메일함을 조회만 하고 실제로 처리하진 않습니다(dry-run). 실제 처리는
    action이 keep이 아닌 카테고리에 매칭된 메일을 진짜로 휴지통/보관 이동·읽음 표시합니다.</p>
    <div class="task-run-bar">
      <form class="slow-form" method="post" action="/tasks/run" style="margin:0">
        <button class="btn secondary" type="submit">새로고침 (dry-run)</button>
      </form>
      <form class="slow-form" method="post" action="/tasks/apply" style="margin:0"
            onsubmit="return confirm('실제로 메일함을 정리합니다(휴지통 이동/보관/읽음 표시). 계속할까요?')">
        <button class="btn danger" type="submit">실제 처리 (--apply)</button>
      </form>
    </div>
    {run_status_html(LAST_RUN)}

    <h2 class="section-title">개별 메일 액션 처리</h2>
    <p class="sub">아래 목록에서 처리할 메일을 체크로 고르세요. 페이지를 넘겨도 선택은
    유지됩니다(이 브라우저에 저장, 7일 후 만료). "선택 실행"을 누르면 총 건수·카테고리별
    내역을 확인한 뒤 휴지통/보관/읽음을 실행합니다. 이미 처리된(휴지통/보관) 메일은
    목록에서 빠집니다.</p>
    {render_task_action_status(LAST_TASK_ACTION)}
    <script id="task-result" type="application/json">{task_result_json}</script>
    {filter_form}

    <div class="sel-bar">
      <span class="sel-count" id="sel-count">0건 선택됨</span>
      <button type="button" class="btn" id="sel-run" disabled>선택 실행 →</button>
      <button type="button" class="btn secondary" id="sel-clear">전체 해제</button>
    </div>
    <p id="sel-warn" class="form-error" hidden>브라우저 저장 공간이 가득 차 선택이 저장되지 않습니다. "전체 해제"로 비우세요.</p>
    {table}
    {pagination}

    <form id="task-form" method="post" action="/tasks/action">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>

    <dialog id="confirm-modal">
      <h3 style="margin:0 0 8px">선택한 메일 처리</h3>
      <div id="confirm-body" class="confirm-body">불러오는 중…</div>
      <p class="confirm-hint">처리 방법을 고르세요 — 실제로 메일함이 바뀝니다.</p>
      <div class="confirm-actions">
        <button type="button" class="btn" data-action="trash">🗑️ 휴지통 이동</button>
        <button type="button" class="btn" data-action="save">📥 보관</button>
        <button type="button" class="btn" data-action="read">✅ 읽음 표시</button>
      </div>
      <div class="actions-row">
        <button type="button" class="btn secondary" id="confirm-cancel">닫기</button>
      </div>
    </dialog>
    {TASKS_SCRIPT}
    """
    return page("작업 실행", body, "tasks")


@app.route("/tasks")
def tasks_page():
    account_filter = request.args.get("account", "").strip() or None
    category_filter = request.args.get("category", "").strip() or None
    try:
        page_size = int(request.args.get("page_size", str(MSG_PAGE_SIZE_DEFAULT)))
    except ValueError:
        page_size = MSG_PAGE_SIZE_DEFAULT
    page_size = max(MSG_PAGE_SIZE_MIN, min(MSG_PAGE_SIZE_MAX, page_size))

    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, all_users, account_type_by_user, categories = load_all_messages(since, None, account_filter)

    if category_filter == "__uncategorized__":
        pool = [m for m in all_messages if m["_category"] is None]
    elif category_filter:
        pool = [m for m in all_messages if m["_category"] == category_filter]
    else:
        pool = all_messages
    # 이미 처리된(active가 아닌) 메일은 액션 대상에서 뺀다 — 다시 처리할 게 없다.
    pool = [m for m in pool if m.get("status", "active") == "active"]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    try:
        page_num = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page_num = 1
    page_num = min(page_num, total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    return render_tasks_page(
        page_items, page_num, total_pages, total, page_size,
        account_filter, category_filter, all_users, account_type_by_user, categories,
    )


@app.route("/tasks/run", methods=["POST"])
def tasks_run():
    global LAST_RUN
    LAST_RUN = run_pipeline(apply=False)
    return redirect(url_for("tasks_page"))


@app.route("/tasks/apply", methods=["POST"])
def tasks_apply():
    global LAST_RUN
    LAST_RUN = run_pipeline(apply=True)
    return redirect(url_for("tasks_page"))


def _active_selection(sel):
    """선택 key(`account::uid`) 목록에서 지금도 status='active'인 것만 검증한다.

    반환: (by_account: {계정: [uid,...]}, valid: {key,...}, all_messages, categories).
    선택은 며칠씩 localStorage에 남아 있을 수 있어(그새 다른 데서 처리됨) 서버에서
    다시 확인한다."""
    wanted = {s for s in sel if "::" in s}
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, _, _, categories = load_all_messages(since, None, None)
    by_account: dict[str, list[str]] = defaultdict(list)
    valid: set[str] = set()
    for m in all_messages:
        key = f'{m["account"]}::{m["uid"]}'
        if key in wanted and m.get("status", "active") == "active":
            by_account[m["account"]].append(m["uid"])
            valid.add(key)
    return by_account, valid, all_messages, categories


@app.route("/tasks/action/preview", methods=["POST"])
def tasks_action_preview():
    """"선택 실행" 확인 모달용 요약(JSON) — 지금도 처리 가능한(active) 메일의 총 건수 +
    카테고리별/계정별 내역 + 실제 제출할 key 목록. 이미 사라진 선택은 missing으로 센다."""
    wanted = {s for s in request.form.getlist("sel") if "::" in s}
    _, valid, all_messages, _ = _active_selection(wanted)
    chosen = [m for m in all_messages if f'{m["account"]}::{m["uid"]}' in valid]
    by_cat = Counter((m.get("_category") or "미분류") for m in chosen)
    by_acct = Counter(m["account"] for m in chosen)
    return jsonify({
        "total": len(valid),
        "missing": len(wanted - valid),
        "keys": sorted(valid),
        "by_category": [{"name": k, "count": v} for k, v in by_cat.most_common()],
        "by_account": [{"account": k, "count": v} for k, v in by_acct.most_common()],
    })


@app.route("/tasks/action", methods=["POST"])
def tasks_action():
    global LAST_TASK_ACTION
    if _is_cross_origin_post():
        return "cross-origin POST 거부", 403
    sel = request.form.getlist("sel")
    action = request.form.get("action", "")
    return_qs = request.form.get("return_qs", "")

    # 제출된 선택 중 지금도 active인 것만 처리한다(오래된 localStorage 선택 방어).
    by_account, _valid, _all, _cats = _active_selection(sel)
    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    run_at = datetime.now().isoformat(timespec="seconds")

    def process_account(user: str, uids: list[str]) -> dict:
        """한 계정의 IMAP 액션만 수행하고 결과 dict를 반환한다 (DB 미접근 — 스레드 병렬용)."""
        account = accounts_cfg.get(user)
        if not account:
            return {"account": user, "candidates": len(uids), "succeeded": [], "failed_uids": list(uids),
                    "folder_display": None, "note": "계정 설정을 찾을 수 없음"}
        if action not in ("read", *MSG_ACTION_FOLDER_FINDERS):
            return {"account": user, "candidates": len(uids), "succeeded": [], "failed_uids": list(uids),
                    "folder_display": None, "note": f"알 수 없는 action: {action}"}

        succeeded: list[str] = []
        failed_uids: list[str] = list(uids)
        folder_display, note = None, None
        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
            imap.login(account["user"], account["password"])
            imap.select("INBOX", readonly=False)
            try:
                if action == "read":
                    succeeded, failed_uids = mark_as_read(imap, uids)
                else:
                    folder = MSG_ACTION_FOLDER_FINDERS[action](imap, account["type"])
                    if not folder:
                        succeeded, failed_uids, note = [], [], "대상 폴더를 찾지 못함"
                    else:
                        succeeded, failed_uids = move_to_folder(imap, uids, folder)
                        folder_display = decode_mailbox_name(folder)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except (imaplib.IMAP4.error, OSError) as e:
            succeeded, failed_uids, note = [], list(uids), str(e)

        return {"account": user, "candidates": len(uids), "succeeded": succeeded,
                "failed_uids": failed_uids, "folder_display": folder_display, "note": note}

    results = []
    failed_keys: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account) or 1)) as executor:
        futures = [executor.submit(process_account, user, uids) for user, uids in by_account.items()]
        for future in as_completed(futures):
            r = future.result()
            user, succeeded = r["account"], r["succeeded"]
            done, failed = len(succeeded), len(r["failed_uids"])
            if succeeded and r["note"] is None:
                if action == "read":
                    mark_message_status(DB_PATH, user, succeeded, is_read=True)
                elif action in MSG_ACTION_STATUS:
                    mark_message_status(DB_PATH, user, succeeded, status=MSG_ACTION_STATUS.get(action))
            failed_keys.extend(f"{user}::{uid}" for uid in r["failed_uids"])
            manual_note = "수동 선택 처리(작업 실행 화면)" + (f" - {r['note']}" if r["note"] else "")
            log_action_run(DB_PATH, run_at, False, user, action, r["candidates"], done, failed,
                           r["folder_display"], manual_note)
            results.append({"account": user, "action": action, "done": done, "failed": failed, "note": r["note"]})

    LAST_TASK_ACTION = {"ran_at": run_at, "results": results, "failed_keys": failed_keys}
    # done=1 → 클라이언트 스크립트가 성공분을 localStorage 선택에서 지운다(실패분은 남김).
    suffix = f"{return_qs}&done=1" if return_qs else "done=1"
    return redirect(f"/tasks?{suffix}")


# ---------------------------------------------------------------------------
# 정리함 (/vault) — 보관함(archived) / 휴지통(trashed) 전용 화면
#   보관함: "되돌리기"(원래 메일함=INBOX로 이동)  ·  휴지통: "영구 삭제"(EXPUNGE)
# 메일이 옮겨지면 UID가 바뀌므로, 대상 폴더에서 messages.message_id 로 다시 찾아
# 처리한다. message_id 가 없는(레거시) 행은 IMAP 반영 없이 DB만 정리한다.
# ---------------------------------------------------------------------------

def render_vault_action_status(run: dict | None) -> str:
    if run is None:
        return ""
    verb = {"restore": "되돌리기", "purge": "영구 삭제"}.get(run["kind"], run["kind"])
    done_word = "되돌림" if run["kind"] == "restore" else "삭제됨"
    rows = []
    for r in run["results"]:
        bits = [f'서버 반영 {r["done"]}건 {done_word}']
        if r.get("db_only"):
            bits.append(f'{r["db_only"]}건은 목록에서만 정리(예전 메일, IMAP 미반영)')
        if r.get("failed"):
            bits.append(f'{r["failed"]}건 실패(서버에서 못 찾음 — 목록 유지)')
        if r.get("note"):
            bits.append(esc(r["note"]))
        rows.append(f'<div>{esc(r["account"])} — {" · ".join(bits)}</div>')
    return f"""
    <div class="run-status">
      <strong>{verb} 결과</strong> · {run['ran_at']}
      {"".join(rows) or "<div>처리한 항목이 없습니다.</div>"}
    </div>
    """


def _vault_tabs_html(active_tab: str) -> str:
    parts = []
    for key, label, *_ in VAULT_TABS:
        cls = ' class="active"' if key == active_tab else ""
        parts.append(f'<a href="/vault?tab={key}"{cls}>{label}</a>')
    return f'<div class="range-tabs">{"".join(parts)}</div>'


@app.route("/vault")
def vault_page():
    tab = request.args.get("tab", "archive")
    if tab not in VAULT_TAB_BY_KEY:
        tab = "archive"
    _, _, status, _, verb = VAULT_TAB_BY_KEY[tab]

    account_filter = request.args.get("account", "").strip() or None
    try:
        page_size = int(request.args.get("page_size", str(MSG_PAGE_SIZE_DEFAULT)))
    except ValueError:
        page_size = MSG_PAGE_SIZE_DEFAULT
    page_size = max(MSG_PAGE_SIZE_MIN, min(MSG_PAGE_SIZE_MAX, page_size))

    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, all_users, account_type_by_user, categories = load_all_messages(
        since, None, account_filter, status=status
    )
    pool = sorted(all_messages, key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    try:
        page_num = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page_num = 1
    page_num = min(page_num, total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    filter_state = {"tab": tab, "account": account_filter or "", "page_size": page_size}
    return_qs = build_qs(**filter_state)

    account_options = '<option value="">전체 계정</option>' + "".join(
        f'<option value="{esc(u)}"{" selected" if u == account_filter else ""}>'
        f'{esc(PROVIDER_LABEL.get(account_type_by_user.get(u, ""), ""))} · {esc(u)}</option>'
        for u in all_users
    )
    filter_form = f"""
    <form class="filter-form" method="get" action="/vault">
      <input type="hidden" name="tab" value="{esc(tab)}">
      <label>계정<select name="account">{account_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True, with_select=True)
    prev_qs = build_qs(**filter_state, page=page_num - 1)
    next_qs = build_qs(**filter_state, page=page_num + 1)
    prev_link = f'<a href="/vault?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/vault?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    if verb == "restore":
        action_url, intro = "/vault/restore", "보관(save) 처리한 메일입니다. 골라서 원래 받은편지함으로 되돌릴 수 있습니다."
        action_btn = '<button class="btn" type="submit" form="vault-form">선택한 메일 되돌리기</button>'
    else:
        action_url, intro = "/vault/purge", "휴지통으로 보낸 메일입니다. 골라서 서버에서 완전히 삭제(복구 불가)할 수 있습니다."
        action_btn = (
            '<button class="btn danger" type="submit" form="vault-form" '
            "onclick=\"return confirm('선택한 메일을 영구 삭제합니다. 복구할 수 없습니다. 계속할까요?')\">"
            "선택한 메일 영구 삭제</button>"
        )

    body = f"""
    <h1 class="page-title">🗂️ 정리함</h1>
    <p class="sub">{esc(intro)}</p>
    {_vault_tabs_html(tab)}
    {render_vault_action_status(LAST_VAULT_ACTION)}
    {filter_form}
    <form id="vault-form" method="post" action="{action_url}">
      <input type="hidden" name="tab" value="{esc(tab)}">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>
    <div class="sel-bar">
      <span class="sel-count">이 페이지에서 체크한 메일에 적용 · 전체 {total}건</span>
      {action_btn}
    </div>
    {table}
    <p class="sub" style="margin-top:6px">※ 예전에 저장돼 식별자(Message-ID)가 없는 메일은
    IMAP 서버에는 반영되지 않고 이 목록에서만 정리됩니다.</p>
    {pagination}
    """
    return page("정리함", body, "vault")


_ALLOWED_ORIGINS = ("http://127.0.0.1:5000", "http://localhost:5000")


def _is_cross_origin_post() -> bool:
    """상태 변경 POST가 로컬 앱 자신이 아닌 다른 출처에서 왔는지 판정한다.

    이 앱은 127.0.0.1 전용이고 세션 쿠키가 없어 SameSite 보호가 없다 — 브라우저의
    다른 탭이 cross-origin 폼 POST로 /vault/purge(영구삭제) 등을 때리는 CSRF를 막는다.
    Origin/Referer 헤더가 아예 없으면(사용자가 직접 돌리는 curl/스크립트) 통과시킨다.
    """
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin not in _ALLOWED_ORIGINS
    referer = request.headers.get("Referer")
    if referer:
        return not any(referer == o or referer.startswith(o + "/") for o in _ALLOWED_ORIGINS)
    return False


def _vault_process(kind: str):
    """/vault/restore · /vault/purge 공통 처리.

    kind="restore": 보관 폴더에서 message_id로 찾아 INBOX로 이동 → 그 행은 삭제(다음
                    fetch가 새 UID로 다시 넣는다). 레거시 행은 status='active'로만.
    kind="purge":   휴지통 폴더에서 message_id로 찾아 EXPUNGE → messages 행 삭제.
    둘 다 계정별로 스레드 병렬(IMAP만), DB 갱신은 메인 스레드에서.

    안전장치:
    - 제출된 uid를 그 탭의 status(archived/trashed)인 실제 행하고만 교집합 — 오래된
      화면이나 위조 POST로 active(받은편지함) 메일을 지우는 걸 막는다.
    - message_id가 있는데 서버 폴더에서 못 찾으면(=조회 실패거나 이미 지워짐) '실패'로
      친다. DB 행은 건드리지 않는다. message_id가 아예 없는 레거시 행만 IMAP 없이
      DB만 정리한다.
    """
    global LAST_VAULT_ACTION
    tab = request.form.get("tab", "archive")
    if tab not in VAULT_TAB_BY_KEY:
        tab = "archive"
    _, _, status_want, _, _ = VAULT_TAB_BY_KEY[tab]
    sel = request.form.getlist("sel")
    return_qs = request.form.get("return_qs", "")

    submitted: dict[str, set[str]] = defaultdict(set)
    for item in sel:
        if "::" in item:
            acc, uid = item.split("::", 1)
            submitted[acc].add(uid)

    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)

    # 메인 스레드에서: 제출 uid ∩ (그 탭 status인 실제 행) + message_id 매핑.
    by_account: dict[str, list[str]] = {}
    mids_by_account: dict[str, dict[str, str | None]] = {}
    for user, uids in submitted.items():
        valid_rows = {
            m["uid"]: m for m in query_messages(DB_PATH, since, None, account=user, status=status_want)
        }
        keep = [u for u in uids if u in valid_rows]
        if keep:
            by_account[user] = keep
            mids_by_account[user] = {u: valid_rows[u].get("message_id") for u in keep}

    finder = find_archive_folder if kind == "restore" else find_trash_folder
    run_at = datetime.now().isoformat(timespec="seconds")

    def process_account(user: str, uids: list[str]) -> dict:
        account = accounts_cfg.get(user)
        mids = mids_by_account.get(user, {})
        db_only = [u for u in uids if not mids.get(u)]      # 레거시(message_id 없음) → DB만
        resolvable = [u for u in uids if mids.get(u)]
        imap_done: list[str] = []
        failed: list[str] = []
        folder_display = None
        note = None

        if account and resolvable:
            try:
                imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
                imap.login(account["user"], account["password"])
                try:
                    folder = finder(imap, account["type"])
                    if not folder:
                        note = "대상 폴더를 찾지 못함"
                        failed = list(resolvable)
                    else:
                        folder_display = decode_mailbox_name(folder)
                        select_folder(imap, folder)  # 루프 밖에서 1회만 SELECT
                        orig_to_new: dict[str, str] = {}
                        claimed: set[str] = set()
                        for u in resolvable:
                            try:
                                new_uid = find_message_uid_by_id(imap, folder, mids[u], select=False)
                            except imaplib.IMAP4.error:
                                new_uid = None  # 조회 실패 → 실패로 (DB는 안 건드림)
                            if new_uid and new_uid not in claimed:
                                orig_to_new[u] = new_uid
                                claimed.add(new_uid)
                            else:
                                failed.append(u)
                        if orig_to_new:
                            targets = list(orig_to_new.values())
                            if kind == "restore":
                                # move_to_folder는 현재 선택된 메일함 기준 → 폴더 재선택.
                                select_folder(imap, folder)
                                ok_new, bad_new = move_to_folder(imap, targets, "INBOX")
                            else:
                                ok_new, bad_new = permanent_delete(imap, folder, targets)
                            ok_set, bad_set = set(ok_new), set(bad_new)
                            for orig, new in orig_to_new.items():
                                if new in ok_set:
                                    imap_done.append(orig)
                                elif new in bad_set:
                                    failed.append(orig)
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
            except (imaplib.IMAP4.error, OSError) as e:
                note = str(e)
                failed = list(resolvable)
        elif not account:
            note = "계정 설정을 찾을 수 없음"
            failed = list(uids)
            db_only = []

        return {
            "account": user, "candidates": len(uids), "imap_done": imap_done,
            "db_only": db_only, "failed": failed,
            "folder_display": folder_display, "note": note,
        }

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account) or 1)) as executor:
        futures = [executor.submit(process_account, u, uids) for u, uids in by_account.items()]
        for future in as_completed(futures):
            r = future.result()
            user = r["account"]
            if kind == "restore":
                # 되돌린 행은 삭제 — UID가 바뀌었으니 다음 fetch가 새 UID로 재삽입한다.
                # (레거시 db_only 행은 실제 이동 여부를 알 수 없어 status만 active로.)
                if r["imap_done"]:
                    delete_messages(DB_PATH, user, r["imap_done"])
                if r["db_only"]:
                    mark_message_status(DB_PATH, user, r["db_only"], status="active")
            else:
                gone = list(r["imap_done"]) + list(r["db_only"])
                if gone:
                    delete_messages(DB_PATH, user, gone)
            action_name = "restore" if kind == "restore" else "purge"
            log_note = ("정리함 " + ("되돌리기" if kind == "restore" else "영구삭제")
                        + (f" - {r['note']}" if r["note"] else ""))
            done_n = len(r["imap_done"])
            log_action_run(DB_PATH, run_at, False, user, action_name, r["candidates"],
                           done_n + len(r["db_only"]), len(r["failed"]), r["folder_display"], log_note)
            results.append({
                "account": user, "done": done_n, "db_only": len(r["db_only"]),
                "failed": len(r["failed"]), "note": r["note"],
            })

    if not results:
        results = [{"account": "—", "done": 0, "db_only": 0, "failed": 0,
                    "note": "처리 대상이 없습니다(이미 처리됐거나 상태가 바뀜)"}]
    LAST_VAULT_ACTION = {"ran_at": run_at, "kind": kind, "results": results}
    return redirect(f"/vault?{return_qs}" if return_qs else "/vault")


@app.route("/vault/restore", methods=["POST"])
def vault_restore():
    if _is_cross_origin_post():
        return "cross-origin POST 거부", 403
    return _vault_process("restore")


@app.route("/vault/purge", methods=["POST"])
def vault_purge():
    if _is_cross_origin_post():
        return "cross-origin POST 거부", 403
    return _vault_process("purge")


def main() -> None:
    """`python -m admin_ui` / `mail-admin` 진입점."""
    print(f"메일 대시보드: http://127.0.0.1:5000  (DB: {DB_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
