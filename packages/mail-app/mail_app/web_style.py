"""admin_app.py(로컬 Flask 화면)와 generate_html.py(Artifact용 정적 리포트) 둘 다 쓰는
공유 CSS + 상단 nav 컴포넌트.

예전엔 admin_app.py의 BASE_STYLE과 generate_html.py의 build() 안 <style>이 서로 다른
CSS 변수 값/이름을 쓰고, generate_html.py에만 다크모드 블록(@media prefers-color-scheme:
dark, :root[data-theme="dark"])이 있어서 두 화면이 서로 다른 팔레트로 보였다(사용자
피드백: "뒤죽박죽"). 이 파일 하나로 토큰과 컴포넌트 스타일을 통일하고, 다크모드는
완전히 제거해서 OS 설정과 무관하게 항상 같은 라이트 팔레트를 쓴다("다크테마가 아닌
눈으로 보기 좋은 테마" 요청 반영).

generate_html.py는 Claude Artifact로 독립 게시되는 정적 조각(fragment)이라 자기
완결적이어야 한다 - 그래서 이 STYLE_CSS를 그대로 자기 <style> 태그 안에 박아 넣는다.
admin_app.py는 페이지 전체를 감싸는 page() 안에서 한 번만 <style>로 포함한다(동일한
CSS를 두 곳에서 각자 자기 완결적으로 쓰는 것 - 값은 이 파일 하나가 유일한 소스).
"""

MAX_WIDTH = "880px"

STYLE_CSS = f"""
:root {{
  --bg: #F7F5F1;
  --surface: #FFFFFF;
  --surface-2: #F0EDE6;
  --text: #1C1B19;
  --text-muted: #655F51;
  --border: #E4E0D8;
  --accent: #2C4A6E;
  --accent-soft: #E4EBF3;
  --trash: #B4791F;
  --trash-soft: #F6E9D3;
  --trend: #2F7A6B;
  --trend-soft: #DFEFEA;
  --danger: #B4432E;
  --danger-soft: #F7E3DE;

  /* 타이포 스케일 - 예전엔 0.62~1.9rem 사이 14종이 흩어져 있었다(사용자 UI 검토 #15).
     6단계로 수렴. 대부분 ±0.05rem 이내 이동이라 시각적 변화는 거의 없다. */
  --fs-xs: 0.72rem;      /* 소형 uppercase 레이블 · action 핀 · tab-count */
  --fs-sm: 0.8rem;       /* 캡션 · 힌트 · 상태 배지 · mini-stat */
  --fs-md: 0.85rem;      /* 기본 UI 본문 (가장 많이 씀) */
  --fs-lg: 1.05rem;      /* 섹션 제목 · 기간 라벨 */
  --fs-xl: 1.45rem;      /* 페이지 h1 */
  --fs-display: 1.9rem;  /* 통계 타일 큰 숫자 */
}}

* {{ box-sizing: border-box; }}
body {{
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 32px 20px 60px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Pretendard,
    "Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji", sans-serif;
}}
h1, h2 {{ text-wrap: balance; margin: 0; }}
.wrap {{ max-width: {MAX_WIDTH}; margin: 0 auto; }}
a {{ color: var(--accent); }}

/* 키보드 포커스 - 전역. 마우스 클릭엔 안 뜨고(:focus-visible) Tab 이동에만 뜬다. */
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }}
.btn:focus-visible {{ outline-offset: 3px; }}
.cfg-item > summary:focus-visible, .account-detail > summary:focus-visible {{ outline-offset: -2px; }}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
  }}
}}

/* 상단 nav - 모든 화면에 동일한 순서(대시보드/작업 실행/설정)로 표시 */
.top-nav {{
  display: flex; align-items: center; gap: 4px; margin-bottom: 24px;
  padding-bottom: 14px; border-bottom: 1px solid var(--border);
  max-width: {MAX_WIDTH}; margin-left: auto; margin-right: auto;
}}
.top-nav .brand {{ font-weight: 700; margin-right: auto; color: var(--text); }}
.top-nav a {{
  display: inline-flex; align-items: center; gap: 4px; text-decoration: none;
  color: var(--text-muted); font-size: var(--fs-md); font-weight: 600;
  padding: 7px 12px; border-radius: 8px;
}}
.top-nav a:hover {{ background: var(--surface-2); color: var(--text); }}
.top-nav a.active {{ background: var(--accent-soft); color: var(--accent); }}

h1.page-title {{ font-size: var(--fs-xl); margin: 0 0 4px; }}
.sub {{ color: var(--text-muted); font-size: var(--fs-md); margin-bottom: 24px; max-width: 68ch; }}

.btn {{
  display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  background: var(--accent); color: #fff; border: none; border-radius: 8px;
  padding: 9px 16px; font-size: var(--fs-md); font-weight: 600; text-decoration: none;
  white-space: nowrap;
}}
.btn.secondary {{ background: var(--surface-2); color: var(--text); }}
.btn.danger {{ background: var(--danger); }}
.btn:disabled {{ opacity: 0.55; cursor: progress; }}
.btn[disabled]:not(.slow-running) {{ cursor: not-allowed; }}

/* 느린 subprocess 실행(/sync·/tasks) 중 버튼에 붙는 스피너 */
.spin {{
  display: inline-block; width: 0.85em; height: 0.85em; margin-right: 5px;
  border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%;
  animation: spin 0.6s linear infinite; vertical-align: -2px;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

/* 폼 검증 실패 배너 (/settings 인라인 추가·수정) */
.form-error {{
  background: var(--danger-soft); color: var(--danger);
  border: 1px solid var(--danger); border-radius: 8px;
  padding: 10px 14px; margin-bottom: 14px; font-size: var(--fs-md); font-weight: 600;
}}
.form-error:focus-visible {{ outline: 2px solid var(--danger); outline-offset: 2px; }}

/* /tasks 실행 버튼 그룹 - 두 버튼을 붙여 둔다(예전 .toolbar는 space-between으로 벌어졌음) */
.task-run-bar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }}

/* 화면 상단 "← 돌아가기" 류 - 밑줄 링크 대신 작은 secondary 버튼 */
p.nav {{ margin: 0 0 16px; }}
.back-link {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 12px; border-radius: 8px; font-size: var(--fs-md); font-weight: 600;
  text-decoration: none; color: var(--text); background: var(--surface-2);
}}
.back-link:hover {{ background: var(--accent-soft); color: var(--accent); }}

table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; border: 1px solid var(--border); }}
th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: var(--fs-md); }}
tr:last-child td {{ border-bottom: none; }}
th {{ color: var(--text-muted); font-size: var(--fs-sm); text-transform: uppercase; letter-spacing: 0.04em; }}

.pill {{ display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: var(--fs-sm); font-weight: 700; background: var(--surface-2); color: var(--text-muted); }}
.pill.trash {{ background: var(--trash-soft); color: var(--trash); }}
.pill.save {{ background: var(--accent-soft); color: var(--accent); }}
.pill.read {{ background: var(--trend-soft); color: var(--trend); }}

/* 메일이 이미 처리됐음을 알려주는 상태 배지 (3b) */
.status-badge {{ display: inline-flex; align-items: center; gap: 3px; padding: 2px 8px; border-radius: 999px; font-size: var(--fs-xs); font-weight: 600; white-space: nowrap; }}
.status-badge.trashed {{ background: var(--trash-soft); color: var(--trash); }}
.status-badge.archived {{ background: var(--accent-soft); color: var(--accent); }}
.status-badge.read {{ background: var(--trend-soft); color: var(--trend); }}

.toolbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; flex-wrap: wrap; }}
form.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; display: flex; flex-direction: column; gap: 14px; }}
label {{ display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-md); font-weight: 600; }}
label .hint {{ font-weight: 400; color: var(--text-muted); font-size: var(--fs-sm); }}
input[type=text], input[type=password], input[type=email], input[type=date], input[type=number], input[type=search], select, textarea {{
  font: inherit; padding: 9px 10px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); min-height: 40px;
}}
textarea {{ min-height: 70px; font-family: ui-monospace, monospace; font-size: var(--fs-md); }}
.row {{ display: flex; gap: 14px; flex-wrap: wrap; }}
.row > label {{ flex: 1; min-width: 160px; }}
.actions-row {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; }}
.empty {{ color: var(--text-muted); padding: 20px; text-align: center; font-size: var(--fs-md); }}

.run-status {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 20px; font-size: var(--fs-md); }}
.run-status .badge {{ display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: var(--fs-xs); font-weight: 700; margin-left: 6px; }}
.run-status .badge.ok {{ background: var(--trend-soft); color: var(--trend); }}
.run-status .badge.fail {{ background: var(--danger-soft); color: var(--danger); }}
.run-status pre {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin: 8px 0 0; font-size: var(--fs-sm); max-height: 220px; overflow: auto; white-space: pre-wrap; }}

.filter-form {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 16px; }}
.filter-form label {{ min-width: 130px; }}
/* 좁은 숫자 입력(페이지당 건수 등)은 라벨까지 130px로 늘리면 입력칸 오른쪽에 죽은
   여백이 생긴다 - 이 라벨만 내용 너비로. */
.filter-form label:has(input[type=number]) {{ min-width: 0; }}
.filter-form input[type=number] {{ width: 80px; }}
.filter-form input[type=search] {{ min-width: 200px; }}

/* 메일 목록 테이블 - 열 너비를 colgroup으로 고정(table-layout: fixed)해서, 데스크톱에선
   제목/발신인/계정만 말줄임(…)으로 잘리고 날짜/카테고리/상태 칸은 안 눌린다. 상태는 별도
   칸으로 분리해서 제목 칸이 지저분해지지 않게 한다. 테이블 min-width 미만 화면에선
   .table-scroll 안에서 테이블만 가로 스크롤 - 페이지 본문(body)은 절대 안 넘친다. */
.table-scroll {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.msg-table {{ table-layout: fixed; min-width: 480px; }}
.msg-table.msg-table--wide {{ min-width: 620px; }}
.msg-table td, .msg-table th {{ vertical-align: middle; }}
.msg-table td.msg-subj {{ vertical-align: top; }}
/* 발신인 칸만 한 줄 말줄임 - 제목 칸은 메타(윗줄)+제목(아랫줄) 2줄이라 자르지 않는다. */
.msg-table td.msg-sender {{
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
/* 제목 칸 윗줄: 카테고리 칩 + 상태 배지를 작게 한 줄로. */
.msg-table .subj-meta {{
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  margin-bottom: 3px; font-size: var(--fs-xs); line-height: 1.4;
}}
.msg-table .subj-meta .pill {{ padding: 1px 7px; font-size: var(--fs-xs); }}
.msg-table .subj-meta-sep {{ color: var(--text-muted); opacity: 0.6; }}
.msg-table .subj-meta .st-none {{ color: var(--text-muted); opacity: 0.5; }}
/* 계정 칸은 제공자(윗줄) + 전체 이메일(아랫줄, 작게) 2줄. 날짜 칸(.msg-date)과 같은 방식. */
.msg-table td.msg-account {{ line-height: 1.3; }}
.msg-table .msg-account .a-provider {{ display: block; }}
.msg-table .msg-account .a-email {{
  display: block; color: var(--text-muted); font-size: 0.82em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.msg-table col.c-date {{ width: 92px; }}
.msg-table col.c-account {{ width: 20%; }}
.msg-table col.c-sender {{ width: 24%; }}
.msg-table .msg-date {{ white-space: nowrap; line-height: 1.3; }}
.msg-table .msg-date .d-date {{ display: block; font-variant-numeric: tabular-nums; }}
.msg-table .msg-date .d-time {{ display: block; color: var(--text-muted); font-size: 0.82em; font-variant-numeric: tabular-nums; }}
.msg-table .subj {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.msg-table tbody tr:hover {{ background: var(--surface-2); }}
/* 표 안에서 클릭 가능한 것(제목 링크 등)만 굵게 + 손 커서 - 어디를 누를 수 있는지 바로 보이게 */
.msg-table a {{ color: var(--text); text-decoration: none; font-weight: 700; cursor: pointer; }}
.msg-table a:hover {{ color: var(--accent); }}

/* 목록 하단 페이지네이션 - 기간 이동(.period-nav)과 완전히 같은 카드+알약 버튼 톤으로 통일.
   이전/다음을 카드 양 끝으로 밀고(space-between) 라벨을 가운데 두면, 880px 카드 안에서
   컨트롤이 중앙에 뭉쳐 좌우로 넓게 비는 문제가 사라진다(≤3 자식 전제). */
.pagination, .period-nav {{
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin: 16px 0 4px; padding: 10px 14px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}}
.period-nav {{ margin: 0 0 24px; }}
.pagination a, .pagination span.disabled,
.period-nav a, .period-nav span.disabled {{
  display: inline-flex; align-items: center; gap: 4px;
  padding: 7px 14px; border-radius: 8px; font-size: var(--fs-md); font-weight: 700;
  text-decoration: none; color: var(--text); background: var(--surface-2);
}}
.pagination a:hover, .period-nav a:hover {{ background: var(--accent-soft); color: var(--accent); }}
.pagination span.disabled, .period-nav span.disabled {{ color: var(--text-muted); opacity: 0.45; pointer-events: none; }}
.pagination .page-label {{
  font-variant-numeric: tabular-nums; color: var(--text-muted);
  font-weight: 600; padding: 0 2px; background: none;
}}

/* "이 기간 목록 보기" 링크 - 리포트 맨 아래 평문 텍스트로 묻히지 않게 카드 전체를 클릭
   가능한 링크로 만들어 강조 (링크 자체가 카드) */
.period-list-link {{
  display: block; margin: 20px 0 0; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  font-size: var(--fs-md); font-weight: 700; text-align: center; text-decoration: none;
}}
.period-list-link:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }}

/* 기간 탭(일일/주간/월별/연도별/전체) - CSS 전용 탭과 이름이 겹치지 않게 range-tab 접두사 사용 */
.range-tabs {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }}
.range-tabs a {{
  text-decoration: none; padding: 8px 16px; border-radius: 999px; font-size: var(--fs-md);
  font-weight: 600; color: var(--text-muted);
}}
.range-tabs a:hover {{ background: var(--surface-2); color: var(--text); }}
.range-tabs a.active {{ background: var(--accent); color: #fff; }}

/* /tasks·/vault 목록 인라인 대량 선택 - 목록 위 선택 바 + 체크박스 열. */
.sel-bar {{
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 4px 0 12px; padding: 10px 14px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}}
.sel-count {{ font-size: var(--fs-md); font-weight: 700; color: var(--accent); margin-right: auto; }}
.msg-table col.c-select {{ width: 38px; }}
.msg-table td.msg-select, .msg-table th[aria-label="선택"] {{ text-align: center; padding-left: 8px; padding-right: 8px; }}
.msg-table td.msg-select input {{ width: 16px; height: 16px; cursor: pointer; }}
.msg-table--select {{ min-width: 660px; }}
.msg-table--select.msg-table--wide {{ min-width: 800px; }}

/* 실행 전 요약 확인 모달 (/tasks "선택 실행") */
dialog#confirm-modal {{
  border: none; border-radius: 14px; padding: 22px; max-width: 480px; width: 92%;
  background: var(--surface); color: var(--text); box-shadow: 0 12px 40px rgba(0,0,0,0.3);
}}
dialog#confirm-modal::backdrop {{ background: rgba(0,0,0,0.45); }}
.confirm-body {{
  border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin: 4px 0 14px;
  font-size: var(--fs-md); max-height: 320px; overflow-y: auto;
}}
.confirm-total {{ margin: 0 0 8px; }}
.confirm-warn {{ color: var(--trash); font-weight: 600; font-size: var(--fs-sm); }}
.confirm-group {{ margin-top: 10px; }}
.confirm-group-title {{
  display: block; font-size: var(--fs-sm); text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted); margin-bottom: 4px;
}}
.confirm-group ul {{ margin: 0; padding-left: 18px; }}
.confirm-group li {{ font-variant-numeric: tabular-nums; }}
.confirm-hint {{ color: var(--text-muted); font-size: var(--fs-sm); margin: 0 0 8px; }}
.confirm-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }}
.confirm-actions .btn {{ flex: 1; justify-content: center; min-width: 110px; background: var(--surface-2); color: var(--text); }}
.confirm-actions .btn:hover {{ background: var(--accent-soft); color: var(--accent); }}

.header {{
  display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 8px 16px; margin-bottom: 24px;
  border-bottom: 1px solid var(--border); padding-bottom: 16px;
}}
.header h1 {{ font-size: var(--fs-xl); font-weight: 700; }}
.header-actions {{ display: flex; align-items: center; gap: 12px; }}
.header .meta {{ color: var(--text-muted); font-size: var(--fs-md); }}

.stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 28px; }}
.stat-tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }}
.stat-tile .label {{ font-size: var(--fs-sm); letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-muted); }}
.stat-tile .value {{ font-size: var(--fs-display); font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 4px; }}
.stat-tile.trash .value {{ color: var(--trash); }}
.stat-tile.read .value, .stat-tile.trend .value {{ color: var(--trend); }}
.stat-tile.save .value, .stat-tile.accent .value {{ color: var(--accent); }}
.stat-tile.muted .value {{ color: var(--text-muted); }}

/* render_capped() 공용 "···" 더보기 토글 - JS 없이 체크박스+레이블+형제 선택자로
   동작한다(기존 CSS 전용 라디오 탭과 같은 원리). 체크박스 자체는 항상 숨김. */
.more-toggle {{ display: none; }}

.stat-tile-extra {{ display: none; }}
.more-toggle:checked ~ .stat-tile-extra {{ display: block; }}
.stat-tile.stat-tile-more {{
  display: flex; align-items: center; justify-content: center; cursor: pointer;
  color: var(--text-muted); font-weight: 700; font-size: var(--fs-lg);
}}
.stat-tile.stat-tile-more:hover {{ background: var(--surface-2); color: var(--text); }}
.more-toggle:checked ~ .stat-tile-more {{ display: none; }}

.chip-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 28px; }}
.chip {{
  display: flex; align-items: center; gap: 6px; background: var(--surface-2); border-radius: 999px;
  padding: 6px 12px; font-size: var(--fs-md); color: var(--text-muted); text-decoration: none;
}}
.chip:hover {{ background: var(--accent-soft); color: var(--accent); }}
.chip-value {{ font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }}
.chip:hover .chip-value {{ color: var(--accent); }}
.chip-extra {{ display: none; }}
.more-toggle:checked ~ .chip-extra {{ display: flex; }}
.chip.chip-more {{ cursor: pointer; }}
.more-toggle:checked ~ .chip-more {{ display: none; }}

.section-title {{ font-size: var(--fs-lg); font-weight: 700; margin: 28px 0 12px; }}

/* /settings - 탭 + 인라인 추가/수정. 각 항목이 <details>라서 "수정"을 누르면 그 자리에서
   편집 폼이 펼쳐진다(별도 페이지 이동 없음). .account-detail과 같은 계열의 카드. */
.cfg-toolbar {{ display: flex; justify-content: flex-end; margin-bottom: 12px; }}
.cfg-list {{ display: flex; flex-direction: column; gap: 8px; }}
.cfg-item {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
.cfg-item > summary {{
  cursor: pointer; list-style: none; display: flex; align-items: center;
  gap: 8px 10px; padding: 12px 14px; font-size: var(--fs-md); flex-wrap: wrap;
}}
.cfg-item > summary::-webkit-details-marker {{ display: none; }}
.cfg-item[open] > summary {{ border-bottom: 1px solid var(--border); background: var(--surface-2); }}
/* 이름/설명 칸은 최소 45%를 확보 - 좁은 창에서는 오른쪽 메타 3칸이 이름 아래로
   줄바꿈되고, 넓은 창에서는 한 줄에 다 들어간다(메타 고정폭 합 ≈ 18em). */
.cfg-item .cfg-main {{ display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: min(45%, 12rem); }}
.cfg-name {{ font-weight: 700; }}
.cfg-desc {{ color: var(--text-muted); font-size: var(--fs-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.cfg-tag {{
  flex-shrink: 0;
  font-size: var(--fs-sm); font-weight: 600; color: var(--text-muted);
  background: var(--surface-2); border-radius: 999px; padding: 2px 9px;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}}
/* /settings 카테고리 행의 오른쪽 메타 3칸(액션 핀·우선순위·건수)을 고정폭 + 가운데
   정렬해서, 행마다 라벨 길이가 달라도(save/trash/keep, HIGH/NORMAL/LOW) 세로로 열이
   맞게 한다. 값은 가장 긴 라벨(trash / NORMAL / "27 / 25 / 0")을 담을 최소치. */
.cfg-item > summary .pill {{
  flex-shrink: 0; text-align: center; min-width: 4em;
}}
.cfg-item > summary .cfg-tag {{ display: inline-block; text-align: center; min-width: 5.8em; }}
.cfg-item > summary .cfg-tag.cfg-counts {{ min-width: 8.4em; }}
.cfg-item[open] > summary .cfg-tag {{ background: var(--surface); }}
.cfg-chevron {{ color: var(--text-muted); transition: transform 0.15s ease; flex-shrink: 0; }}
.cfg-item[open] > summary .cfg-chevron {{ transform: rotate(90deg); }}
.cfg-add > summary {{ color: var(--accent); font-weight: 700; }}
.cfg-add {{ border-color: var(--accent-soft); }}
.cfg-add[open] > summary {{ background: var(--accent-soft); border-bottom-color: var(--border); }}
.cfg-add-label {{ margin-right: auto; }}
.cfg-body {{ padding: 16px 14px; }}
.cfg-form {{ display: flex; flex-direction: column; gap: 12px; }}
.cfg-form label {{ display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-md); font-weight: 600; }}
.req {{ color: var(--danger); font-weight: 700; font-style: normal; }}
.cfg-form .row > label {{ flex: 1; min-width: 150px; }}
.cfg-form textarea {{ min-height: 58px; }}
.cfg-form .actions-row {{ margin-top: 2px; }}
.cfg-delete {{ margin: 10px 14px 14px; display: flex; justify-content: flex-end; }}
.cfg-static {{ padding: 0; }}
.cfg-summary-static {{ display: flex; flex-direction: column; gap: 2px; padding: 12px 14px; }}

.cat-row {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }}
.cat-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 12px; }}
.cat-name {{ font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
.cat-action-pill {{ font-size: var(--fs-xs); font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; padding: 2px 7px; border-radius: 999px; background: var(--surface-2); color: var(--text-muted); }}
/* action 색상: save(보관)=강조 네이비, read(읽음)=녹색, trash(휴지통)=앰버.
   클래스명이 곧 action 이름 - generate_html.ACTION_COLOR_CLASS 와 1:1. */
.cat-action-pill.trash {{ background: var(--trash-soft); color: var(--trash); }}
.cat-action-pill.save, .cat-action-pill.accent {{ background: var(--accent-soft); color: var(--accent); }}
.cat-action-pill.read, .cat-action-pill.trend {{ background: var(--trend-soft); color: var(--trend); }}
.cat-count {{ color: var(--text-muted); font-size: var(--fs-md); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.cat-bar {{ height: 6px; border-radius: 999px; background: var(--surface-2); overflow: hidden; margin-bottom: 10px; }}
.cat-bar-fill {{ height: 100%; background: var(--text-muted); border-radius: 999px; }}
.cat-row.trash .cat-bar-fill {{ background: var(--trash); }}
.cat-row.read .cat-bar-fill, .cat-row.trend .cat-bar-fill {{ background: var(--trend); }}
.cat-row.save .cat-bar-fill, .cat-row.accent .cat-bar-fill {{ background: var(--accent); }}

.mail-list {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }}
.mail-list li {{ display: flex; justify-content: space-between; align-items: center; gap: 4px 12px; flex-wrap: wrap; font-size: var(--fs-md); padding: 6px 8px; border-radius: 6px; background: var(--surface-2); }}
.mail-list .subj {{ flex: 1 1 55%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mail-list .subj a {{ color: inherit; text-decoration: none; }}
.mail-list .subj a:hover {{ text-decoration: underline; }}
.mail-list .sender {{ color: var(--text-muted); flex-shrink: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; font-size: var(--fs-sm); }}
.mail-list .more {{ color: var(--text-muted); justify-content: center; background: none; }}

.action-list {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 28px; }}
.action-row {{ display: flex; justify-content: space-between; align-items: center; gap: 6px 12px; flex-wrap: wrap; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; font-size: var(--fs-md); }}
.action-account {{ font-weight: 600; min-width: 0; overflow-wrap: anywhere; }}
.action-badge {{ font-size: var(--fs-sm); font-weight: 600; padding: 4px 10px; border-radius: 999px; white-space: nowrap; flex-shrink: 0; }}
.action-badge.pending {{ background: var(--accent-soft); color: var(--accent); }}
.action-badge.done {{ background: var(--trend-soft); color: var(--trend); }}
.action-badge.warn {{ background: var(--trash-soft); color: var(--trash); }}

.account-list {{ display: flex; flex-direction: column; gap: 10px; margin-bottom: 28px; }}

/* admin_app.py 라이브 대시보드 - "계정별 상세"를 세로로 쌓지 않고 한 번에 계정 1개만
   꽉 차게 보여주는 캐러셀로. 트랙은 가로 스크롤(스냅, 1칸=카드 1개) + ‹/› 버튼,
   가운데 "n / 전체" 위치 표시. */
.account-carousel {{ margin-bottom: 28px; }}
.carousel-bar {{ display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-bottom: 8px; }}
.carousel-count {{ font-size: var(--fs-sm); color: var(--text-muted); font-variant-numeric: tabular-nums; }}
.account-track {{
  display: flex; align-items: flex-start; gap: 12px; overflow-x: auto; padding-bottom: 6px;
  scroll-snap-type: x mandatory; scroll-behavior: smooth;
  scrollbar-width: none; transition: height 0.2s ease;
}}
.account-track::-webkit-scrollbar {{ display: none; }}
.account-track > .account-detail {{
  scroll-snap-align: start; flex: 0 0 100%; align-self: flex-start; min-width: 0;
}}
/* 카드 안 목록이 길어도 캐러셀 전체가 세로로 폭주하지 않게 높이를 제한. */
.account-track > .account-detail.open .account-detail-body {{ max-height: 62vh; overflow-y: auto; }}
.carousel-nav {{
  width: 34px; height: 34px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  font-size: var(--fs-lg); line-height: 1; display: flex; align-items: center; justify-content: center;
}}
.carousel-nav:hover:not(:disabled) {{ background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }}
.carousel-nav:disabled {{ opacity: 0.35; cursor: default; }}

/* 캐러셀 첫 슬라이드 - 전 계정 통합 카드. 헤더는 링크가 아니라 고정 제목. */
.unified-card .account-summary-link {{ cursor: default; }}
.unified-card .account-summary-link:hover {{ background: none; }}
.unified-actions {{ margin-bottom: 16px; }}
.unified-sub {{ font-size: var(--fs-md); font-weight: 700; margin: 0 0 8px; color: var(--text-muted); }}
/* 계정 카드의 "처리 예상" 한 줄 요약. */
.acct-action-note {{
  padding: 6px 16px 12px; font-size: var(--fs-sm); color: var(--text-muted);
}}
.account-detail.open .acct-action-note {{ border-bottom: 1px solid var(--border); padding-bottom: 12px; }}

.account-detail {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; scroll-margin-top: 16px; }}
.account-detail summary {{ cursor: pointer; list-style: none; display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; padding: 14px 16px; font-weight: 600; }}
.account-detail summary::-webkit-details-marker {{ display: none; }}
.account-detail[open] summary {{ border-bottom: 1px solid var(--border); }}
.account-name {{ margin-right: auto; }}
.account-mini-stats {{ display: flex; gap: 6px; flex-wrap: wrap; font-weight: 400; }}
.mini-stat {{
  display: inline-flex; align-items: center; justify-content: center;
  box-sizing: border-box; text-align: center;
  font-size: var(--fs-sm); color: var(--text-muted); background: var(--surface-2);
  border-radius: 999px; padding: 3px 9px; font-variant-numeric: tabular-nums;
}}
/* 카테고리명+건수 칩의 폭을 3글자 단위(3/6/9/12)로 양자화 - 가로로 정렬돼 보이게.
   generate_html.chip_width_bucket()가 클래스를 붙인다. */
.mini-stat.msw3  {{ min-width: 3.4em; }}
.mini-stat.msw6  {{ min-width: 4.9em; }}
.mini-stat.msw9  {{ min-width: 6.4em; }}
.mini-stat.msw12 {{ min-width: 7.9em; }}
.mini-stat.trash {{ color: var(--trash); }}
.mini-stat.read, .mini-stat.trend {{ color: var(--trend); }}
.mini-stat.save, .mini-stat.accent {{ color: var(--accent); }}
.mini-stat-extra {{ display: none; }}
.more-toggle:checked ~ .mini-stat-extra {{ display: inline-flex; }}
.mini-stat.mini-stat-more {{ cursor: pointer; }}
.more-toggle:checked ~ .mini-stat-more {{ display: none; }}
.chevron {{ color: var(--text-muted); transition: transform 0.15s ease; flex-shrink: 0; }}
.account-detail[open] .chevron {{ transform: rotate(90deg); }}
.account-detail-body {{ padding: 14px 16px; }}

/* admin_app.py 라이브 계정 카드 - <details>/<summary> 대신 순수 링크로 여닫는다(펼침
   상태를 URL의 ?acct=로 서버가 관리하므로, 네이티브 <details> 토글과 상태가 어긋나면
   안 돼서). 시각적으로는 정적 버전의 summary와 최대한 동일하게 보이도록 맞췄다. */
.account-summary-link {{
  cursor: pointer; text-decoration: none; color: inherit;
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px;
  padding: 14px 16px; font-weight: 600;
}}
.account-detail.open .account-summary-link {{ border-bottom: 1px solid var(--border); }}
.account-detail.open .chevron {{ transform: rotate(90deg); }}

/* 계정 태그(.chip) 클릭 -> 이 카드로 #fragment 이동 시, <details>가 open 속성 없이
   닫혀 있어도 :target이면 내용이 보이게 강제한다(JS 없이 "포커스 이동 + 펼쳐보이기"
   둘 다 구현). admin_app.py의 라이브 화면은 이와 별개로 서버가 open 속성 자체를
   내려주지만(계정 접기/펼치기 상태를 URL로 관리), 이 규칙은 정적 Artifact 등
   서버 상태가 없는 화면에서도 똑같이 동작하는 안전망이다. */
details.account-detail:not([open]):target .account-detail-body {{ display: block; }}
details.account-detail:target {{ border-color: var(--accent); }}
details.account-detail:not([open]):target summary .chevron {{ transform: rotate(90deg); }}

.tabs {{ margin-bottom: 12px; }}
.tab-input {{ position: absolute; opacity: 0; width: 1px; height: 1px; overflow: hidden; }}
.tab-nav {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}
.tab-label {{ cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; font-size: var(--fs-md); font-weight: 600; color: var(--text-muted); transition: background 0.1s ease, color 0.1s ease; }}
.tab-label:hover {{ color: var(--text); }}
.tab-count {{ font-variant-numeric: tabular-nums; font-weight: 400; opacity: 0.75; }}
.tab-label-extra {{ display: none; }}
.more-toggle:checked ~ .tab-label-extra {{ display: inline-flex; }}
.tab-label.tab-label-more {{ color: var(--text-muted); }}
.more-toggle:checked ~ .tab-label-more {{ display: none; }}
.tab-panels .tab-panel {{ display: none; }}
.tab-panel .cat-row {{ margin-bottom: 0; }}
.account-detail-body .tabs {{ margin-bottom: 0; }}

/* 기간 이동(이전/날짜/다음) - 컨테이너·버튼 톤은 위 .pagination과 공유(한 규칙에 묶음).
   여기선 가운데 날짜 라벨만 크게. */
.period-nav .period-label {{
  font-size: var(--fs-lg); font-weight: 700; font-variant-numeric: tabular-nums;
  min-width: 170px; text-align: center; color: var(--text); background: none; padding: 0;
}}

/* admin_app.py 라이브 화면 전용 - 계정 카드를 펼쳤을 때 그 계정의 카테고리 필터
   칩 + 실제 페이지네이션 목록 (정적 Artifact엔 없음, DB 실시간 조회가 필요해서). */
.acct-filter-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin: 0 0 14px; }}
.acct-filter-chip {{
  display: inline-flex; align-items: center; gap: 4px; text-decoration: none;
  padding: 5px 11px; border-radius: 999px; font-size: var(--fs-sm); font-weight: 600;
  color: var(--text-muted); background: var(--surface-2);
}}
.acct-filter-chip:hover {{ color: var(--text); }}
.acct-filter-chip.active {{ background: var(--accent); color: #fff; }}
.acct-filter-chip-extra {{ display: none; }}
.more-toggle:checked ~ .acct-filter-chip-extra {{ display: inline-flex; }}
.acct-filter-chip.acct-filter-more {{ cursor: pointer; }}
.more-toggle:checked ~ .acct-filter-more {{ display: none; }}
"""


def render_nav(active: str) -> str:
    """모든 화면 상단에 같은 순서로 표시하는 nav. active는 'dashboard'/'tasks'/'vault'/'label'/'settings' 중 하나."""
    items = [
        ("dashboard", "/", "대시보드"),
        ("tasks", "/tasks", "작업 실행"),
        ("vault", "/vault", "🗂️ 정리함"),
        ("label", "/label", "🏷️ 라벨링"),
        ("settings", "/settings", "⚙️ 설정"),
    ]
    active_cls = ' class="active"'
    links = "".join(
        f'<a href="{href}"{active_cls if key == active else ""}>{label}</a>'
        for key, href, label in items
    )
    return f'<nav class="top-nav"><span class="brand">메일 대시보드</span>{links}</nav>'
