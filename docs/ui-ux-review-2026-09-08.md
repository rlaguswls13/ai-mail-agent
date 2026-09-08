# UI/UX 검토 리포트 — admin_ui (2026-09-08)

**검토 대상**: `/`, `/list`, `/tasks`, `/settings` · 데스크톱(1000px) + 모바일(390px)
**방법**: `ui-ux-pro-max` 스킬 (119 UX 가이드라인 + 사전 배포 체크리스트) + Playwright 실제 렌더 + `web_style.py` / `admin_app.py` / `generate_html.py` 코드 검토

**진행 결과 (2026-09-08)**: #1~#17 **전부 적용 완료** (사용자 "순차로 진행" + "#15도 진행").
커밋 `5c81e0d`(P1) · `fc22c0f`(A) · `3973b48`(B) · `b3103de`(C) · `<font>`(#15).
"참고"의 이모지→SVG 교체는 **폐기**(사용자 결정) — 이모지 유지.

> ⚠️ 스킬의 `--design-system` 추천(glassmorphism 다크 / Fira Code / "실시간 운영 랜딩")은
> **채택 안 함**. "dashboard" 키워드가 마케팅 랜딩 패턴으로 오라우팅됐고, 이 제품은 내부
> 도구 + 의도된 라이트 웜 팔레트임. `--domain ux` 결과만 반영.

---

## 한눈에 보기

| # | 우선순위 | 항목 | 영향 | 공수 | 파일 | 판단 |
|---|:---:|---|---|:---:|---|:---:|
| 1 | 🔴 P1 | 메일 목록 테이블이 ~700px 미만에서 판독 불가 | 큼 (모바일 전멸) | S | web_style · admin_app | |
| 2 | 🔴 P1 | `/` 모바일 페이지 전체 가로 넘침 (375→444px) | 큼 | XS | web_style | |
| 3 | 🔴 P2 | 전역 키보드 포커스 링 없음 | 큼 (a11y) | XS | web_style | |
| 4 | 🔴 P2 | 폼 검증·에러 피드백 부재 (중복 이름 → 500) | 중 | M | admin_app | |
| 5 | 🟠 P3 | `/tasks`·`/sync` 버튼이 최대 2분 무반응 | 중 | S | admin_app(+JS) | |
| 6 | 🟡 P4 | `/tasks` "액션 처리 (4698건 대상)" — 필터 없이 전체가 파괴적 액션 대상 | 중 (위협적) | S | admin_app | |
| 7 | 🟡 P4 | `/tasks` 버튼 간 빈 공간 + 강한 버튼 3개 경쟁 | 소 | XS | web_style | |
| 8 | 🟡 P4 | "전체" 탭이 "2000-01-01 ~ 지금" 표시 | 소 | XS | generate_html | |
| 9 | 🟡 P4 | 빈 상태가 텍스트만 (액션·안내 없음) | 소 | S | admin_app | |
| 10 | 🟡 P4 | `.section-title`이 `<div>` (실제 `<h2>` 아님) | 소 (a11y) | XS | admin_app · generate_html | |
| 11 | 🟡 P4 | 설명 텍스트가 880px 전체폭 (~110자) | 소 | XS | web_style | |
| 12 | 🟡 P4 | `/favicon.ico` 404 (모든 페이지 콘솔 에러) | 소 | XS | admin_app · generate_html | |
| 13 | 🟡 P5 | `prefers-reduced-motion` 미대응 | 소 (a11y) | XS | web_style | |
| 14 | 🟡 P5 | 액션 색상 클래스명이 의미와 불일치 (`accent`=save, `trend`=read) | 소 (유지보수) | S | 3파일 | |
| 15 | 🟡 P5 | 폰트 크기 12종 스캐터 (0.62~1.9rem), 스케일 없음 | 소 | M | web_style | |
| 16 | 🟡 P5 | 작은 muted 텍스트 대비 경계값 (~4.5:1) | 소 (a11y) | XS | web_style | |
| 17 | 🟡 P5 | 터치 타깃 24×24px 미만 (number input, 칩, `···`) | 소 (데스크톱 주력) | S | web_style | |
| — | ⚪ 참고 | 이모지 아이콘 → SVG 교체 (스킬 권장) | — | — | — | **기각 권장** |

공수: XS ≈ 5–15분 · S ≈ 15–40분 · M ≈ 40–90분

**추천 실행 묶음**
- **묶음 A** (반응형+a11y 필수): #1 #2 #3 #10 #11 #13 #16 — web_style 중심, 회귀 위험 낮음, 실사용 impact 최대
- **묶음 B** (안전장치+피드백): #4 #5 #6 #7 — admin_app 로직 + 최소 JS
- **묶음 C** (정리·카피): #8 #9 #12 #14 #15 #17 — 리팩터/문구

---

## 상세

### 🔴 P1 — 반응형 깨짐

#### 1. 메일 목록 테이블이 좁은 화면에서 무너짐
- **증상**: 390px에서 헤더가 겹쳐 `제\n실\n록\n태`처럼 나옴. 셀 내용 판독 불가.
- **원인**: `.msg-table { table-layout: fixed }` + 6열 + `colgroup` 고정폭(date 92px + account 20% + cat 94px + status 118px + sender 21%)이 뷰포트 초과. `overflow-x: auto` 래퍼도, 카드 폴백도 없음.
- **스킬 규칙**: `data-table`(HIGH), `horizontal-scroll`(HIGH), `table-handling`
- **수정안**: `msg_table()` 반환값을 `<div class="table-scroll">`(‑`overflow-x:auto; -webkit-overflow-scrolling:touch`)로 감싸고 `.msg-table { min-width: 720px }`. → 좁으면 테이블만 가로 스크롤(페이지 본문은 안 넘침). 여유되면 후속으로 640px 미만 카드 스택.
- **위치**: `admin_app.py` `msg_table()` · `web_style.py`

#### 2. `/` 모바일 페이지 전체 가로 넘침
- **증거**: viewport 375px인데 `document.scrollWidth = 444`. 범인 = `.action-badge.done` (예: "6건 '보관함'(으)로 보관 완료", `white-space: nowrap`, ~240px)이 `.action-row`(`justify-content: space-between`, no-wrap) 안에서 안 줄어듦.
- **스킬 규칙**: `horizontal-scroll`(HIGH), `long-token-wrapping`
- **수정안**: `.action-row { flex-wrap: wrap; }` · `.action-badge { white-space: normal; }` · `render_matches`의 긴 링크에 `overflow-wrap: anywhere`.
- **위치**: `web_style.py`

---

### 🔴 P2 — 접근성

#### 3. 전역 키보드 포커스 링 없음
- **증상**: `web_style.py`에 `:focus-visible` 규칙 없음. `render_tab_group`이 만드는 라디오 탭만 아웃라인 있음. 버튼·링크·`<details><summary>`·입력·페이지네이션 전부 Tab 이동 시 아무 표시 없음 → 키보드 사용자가 현재 위치를 못 봄.
- **스킬 규칙**: `focus-states`(CRITICAL), `focus-appearance`, `keyboard-nav`
- **수정안**: `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }` 전역 1줄 + `.cfg-item > summary:focus-visible` 등 개별 보정.
- **위치**: `web_style.py`

#### 4. 폼 검증·에러 피드백 부재
- **증상**:
  - 카테고리 이름 **중복** → `config_store.add_category` → SQLite IntegrityError → **Flask 500 페이지**
  - 이름 **빈 값** → `request.form["name"]` KeyError → 400
  - 필수 표시(`*`) 없음. 에러를 필드 근처에 인라인으로 안 보여줌.
- **스킬 규칙**: `error-placement`(HIGH), `inline-validation`, `error-clarity`, `required-indicators`, `aria-live-errors`
- **수정안**: `create_category` / `update_category` / `create_account`에 서버 검증 → 실패 시 `/settings` 재렌더(해당 `<details>` 열어둔 채, 필드 위 `role="alert"` 메시지 + 원인·해결 명시: "이미 있는 이름입니다"). `name` 필드에 `required` 표시.
- **위치**: `admin_app.py`

---

### 🟠 P3 — 비동기 피드백

#### 5. `/tasks`·`/sync` 버튼이 최대 2분 무반응
- **증상**: "새로고침(dry-run)", "실제 처리(--apply)", "동기화" — `fetch_mail` subprocess 동기 실행(계정 수에 따라 30초~2분) 동안 버튼이 아무 반응 없이 페이지가 얼어붙음. 트레이엔 "동기화 중…"이 뜨지만 웹 UI엔 없음.
- **스킬 규칙**: `loading-buttons`(CRITICAL), `submit-feedback`, `loading-states`, `progressive-loading`
- **수정안**: 최소 vanilla JS — form submit 시 해당 버튼 `disabled` + 텍스트 "실행 중… (수십 초~2분 소요)" + CSS 스피너. (프로젝트가 최소 JS 허용)
- **위치**: `admin_app.py` `page()` 하단 공용 `<script>` + 버튼 마크업

---

### 🟡 P4 — 콘텐츠·계층·카피

#### 6. `/tasks` "액션 처리 (4698건 대상) →"
- **증상**: 필터를 안 걸면 **저장된 전체 메일(≈4698건)**이 파괴적 액션(휴지통/보관 이동) 모달의 기본 대상으로 표시됨. 숫자 자체는 맞지만(730일 창) 위협적이고 실수 유발.
- **스킬 규칙**: `confirmation-dialogs`, `destructive-emphasis`, `progressive-disclosure`
- **수정안**: 계정/카테고리 필터 선택 전엔 버튼 비활성 + "먼저 대상을 좁히세요" 안내. 또는 기본 창을 최근 N일로 축소.
- **위치**: `admin_app.py:1185` 부근 (`render_tasks_page`)

#### 7. `/tasks` 버튼 배치
- **증상**: "새로고침(dry-run)"(secondary)과 "실제 처리(--apply)"(danger) 사이에 `toolbar { justify-content: space-between }`로 거대한 빈 공간. 화면에 강한 버튼 3개(필터 적용 navy, 액션 처리 navy, 실제 처리 red) 경쟁.
- **스킬 규칙**: `primary-action`(화면당 primary CTA 1개), `destructive-emphasis`
- **수정안**: dry-run/apply 두 버튼을 `gap`으로 묶기. "실제 처리"만 danger, 나머지는 secondary로 낮춤.
- **위치**: `web_style.py` `.toolbar`

#### 8. "전체" 탭 날짜 표기
- **증상**: "메일 리포트 · **2000-01-01** ~ 지금" — `EPOCH_START = datetime(2000,1,1)`이 그대로 노출. 실제 데이터는 2025-12-31부터.
- **수정안**: 실제 최소 `message_date` 조회해서 표시하거나 "전체 기간"으로.
- **위치**: `generate_html.py` `build()` / `build_report()`

#### 9. 빈 상태
- **증상**: "카테고리가 없습니다." / "해당 조건의 메일이 없습니다." — 텍스트만. 다음 행동 안내 없음.
- **스킬 규칙**: `empty-states`(메시지 + 액션)
- **수정안**: "+ 카테고리 추가로 시작하세요" 식 CTA/힌트 추가.
- **위치**: `admin_app.py` `.empty` 여러 곳

#### 10. `.section-title`이 `<div>`
- **증상**: 페이지가 `h1.page-title` → `div.section-title` 순. 실제 제목 계층(`h2`)이 아니라 스크린리더 목차에서 누락.
- **스킬 규칙**: `heading-hierarchy`(순차 h1→h6)
- **수정안**: `<h2 class="section-title">`로 변경. CSS는 그대로 (`h2` reset이 `margin:0`이라 안전).
- **위치**: `admin_app.py` · `generate_html.py`

#### 11. 설명 텍스트 줄길이
- **증상**: `.sub` / 헬퍼 텍스트가 880px 컨테이너 전체폭 = ~110자/줄. 권장 65–75자.
- **스킬 규칙**: `line-length`, `line-length-control`
- **수정안**: `.sub { max-width: 60ch; }`
- **위치**: `web_style.py`

#### 12. favicon 404
- **증상**: 모든 페이지에서 `GET /favicon.ico 404` 콘솔 에러.
- **수정안**: `page()`의 `<head>`에 `<link rel="icon" href="data:image/svg+xml,...📬...">` (이모지 data-URI).
- **위치**: `admin_app.py` `page()` · `generate_html.py` `build()`

---

### 🟡 P5 — 토큰·모션 정리 (유지보수 성격)

#### 13. `prefers-reduced-motion` 미대응
- chevron 회전(0.15s), hover transition 등. `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; } }` 추가.
- 스킬 규칙: `reduced-motion`(CRITICAL 등급)

#### 14. 액션 색상 클래스명 불일치
- `generate_html.ACTION_COLOR_CLASS = {trash:"trash", save:"accent", read:"trend", keep:"muted"}` — `save`인데 클래스는 `accent`, `read`인데 `trend`. `.pill.save`(웹)와 `.pill.accent`(리포트)가 따로 존재하는 등 혼선.
- 수정안: 클래스명을 의미 그대로(`save`/`read`/`trash`/`keep`)로 통일. `web_style.py`의 `.pill.*`, `.mini-stat.*`, `.cat-row.*`, `.cat-action-pill.*` + `ACTION_COLOR_CLASS` + `MSG_ACTION_PILL_CLASS` 동시 변경.
- 스킬 규칙: `color-semantic`

#### 15. 폰트 크기 스캐터
- 0.62 / 0.72 / 0.75 / 0.78 / 0.8 / 0.82 / 0.85 / 0.88 / 0.9 / 1.05 / 1.4 / 1.5 / 1.9 rem — 12종, 스케일 없음.
- 수정안: `--text-xs:0.75rem / --text-sm:0.85rem / --text-base:1rem / --text-lg:1.15rem / --text-xl:1.5rem` 토큰으로 수렴 (일부 미세값은 반올림).
- 스킬 규칙: `font-scale`

#### 16. 작은 muted 텍스트 대비
- `--text-muted:#726C5E` on `--bg:#F7F5F1` ≈ **4.5:1 경계값**. `.d-time`(0.82em), `.cfg-desc`(0.8rem), `.chip`(0.82rem) 등 작은 글씨에서 위태로움.
- 수정안: `--text-muted`를 한 단계 어둡게(예 `#655F52`, ≈ 5.3:1) 하거나, 작은 텍스트만 `--text` 쪽으로.
- 스킬 규칙: `color-contrast`(CRITICAL), `color-accessible-pairs`

#### 17. 터치 타깃 크기
- `<input type=number>` 80px폭·~34px높이, `.pill`(~20px), `···` more-toggle, `.mini-stat`/`.acct-filter-chip` 칩들이 24×24 CSS px 미만.
- 데스크톱 주력이라 우선순위 낮음. 페이지네이션 `···`와 number input 정도만 높이 확보.
- 스킬 규칙: `web-target-size`(24×24 AA), `touch-target-size`

---

### ⚪ 참고 — 스킬 권장이나 기각 권장

**이모지 아이콘 → SVG(Heroicons/Lucide) 교체** (`no-emoji-icons` — HIGH)
- 대상: `⚙️` `🗑️` `📥` `✅` `▸` `···`
- **기각 권장 이유**:
  1. 로컬 단일 PC 전용 → 이모지 렌더 일관성 보장
  2. 상태 배지는 텍스트 라벨 동반(`🗑️ 휴지통 이동됨`) → `color-not-only` 이미 충족
  3. 아이콘셋 인라인/CDN은 "표준 라이브러리 지향" 프로젝트 정신에 역행, `generate_html` 정적 조각의 자기완결성도 해침
- 하고 싶다면: 장식용 `▸`(chevron) `···`(더보기)만 CSS 삼각형/점으로 정리하는 선에서 (P5에 포함 가능)

---

## 검토 시 참고 (현재 잘 되어 있는 점)

- URL로 상태 전달 (`?range=`/`?date=`/`?acct=`/`?q=`) — `deep-linking` ✅
- `font-variant-numeric: tabular-nums` 대부분 적용 — `number-tabular` ✅
- 활성 nav 표시 (`.top-nav a.active`) ✅
- 파괴적 액션에 `confirm()` ✅
- CSS 전용 탭/토글 (JS 최소) — 프로젝트 제약과 정합 ✅
- 라이트 웜 팔레트 일관 (다크모드 의도적 제거) ✅

---

## ✅ 진행 결과 (2026-09-08 — "순차로 진행")

#15(폰트 스케일) 제외 **16개 항목 전부 적용**. 커밋 4개:

| 커밋 | 범위 | 항목 |
| :--- | :--- | :--- |
| `5c81e0d` | P1 반응형 | #1 테이블 `.table-scroll` 래퍼+min-width · #2 `/` 가로 오버플로 제거 |
| `fc22c0f` | 묶음 A 접근성·계층 | #3 전역 `:focus-visible` · #10 `.section-title`→`<h2>` · #11 `.sub` max-width 68ch · #13 `prefers-reduced-motion` · #16 `--text-muted` #726C5E→#655F51 |
| `3973b48` | 묶음 B 피드백·안전장치 | #4 폼 검증+`.form-error` 배너(입력값 보존) · #5 `.slow-form` 스피너 · #6 `/tasks` 필터 없으면 액션 버튼 비활성 · #7 버튼 그룹화 · #12 viewport meta+favicon |
| `b3103de` | 묶음 C 카피·색상·터치 | #8 전체 탭 실제 최소 날짜 · #9 빈 상태 카피 · #14 색상 클래스 `accent/trend`→`save/read` · #17 입력 min-height 40px |

**검증**: 매 묶음마다 py_compile · 전 라우트 200 · Playwright(데스크톱 1000px + 모바일 390px)
실제 렌더 · `generate_html` 정적 리포트 · `desktop/pybundle` 재vendor.

### ✅ #15 폰트 스케일 토큰화 — 완료

`web_style.py`에 흩어진 14종 `font-size`(0.62~1.9rem)를 `:root`의 6단계 토큰으로 수렴:
`--fs-xs 0.72 / --fs-sm 0.8 / --fs-md 0.85(기본) / --fs-lg 1.05 / --fs-xl 1.45 / --fs-display 1.9`.
46곳 치환, 대부분 ±0.03rem(±0.5px) 이동이라 시각적 변화 없음(Playwright computed size로 확인).
가장 큰 이동: `.cat-action-pill` 0.62→0.72(원래 12px 미만이라 오히려 개선), h1 1.4/1.5→1.45(통일).

### ❌ 참고 — 이모지 아이콘 → SVG 교체: 폐기

사용자 결정으로 폐기. 이모지 유지(로컬 단일 PC · 상태 배지는 텍스트 라벨 동반).
