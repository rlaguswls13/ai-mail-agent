# 메일 대시보드 리포트

Gmail / Naver / Outlook 세 메일함을 매일 새벽에 로컬에서 IMAP으로 조회해
총 메일 수 / 광고 메일 수 / 관심사(커스텀 카테고리) 메일 수를 집계하고,
오전 6시에 HTML 대시보드 아티팩트 링크로 받아보기 위한 프로젝트입니다.

분류 로직은 카테고리를 **priority(HIGH → NORMAL → LOW) 순**으로, 카테고리 안에서는
**발신인 → 제목 → 본문** 순으로 키워드를 매칭합니다. 카테고리 설정과 조회한 원본 메일은
전부 `data/app.db` 하나(SQLite)에 저장되고, 카테고리는 로컬 웹 화면(`src/admin_app.py`)에서
추가/수정합니다 (아래 2절). 분류 자체는 순수 Python(표준 라이브러리만 사용)으로 처리되어
LLM 토큰을 전혀 쓰지 않습니다.

## 0. Python

이 PC에는 `python` 에 Python 3.11이 이미 설치되어 있습니다.
아래 명령어들은 전부 이 경로를 명시적으로 씁니다.

PATH에 등록해서 매번 전체 경로를 안 쓰고 `python` 명령만 쓰고 싶다면:

```powershell
setx PATH "$env:PATH;D:\dev-tool\python;D:\dev-tool\python\Scripts"
```

(새 터미널을 열어야 반영됩니다. 아직 설치가 안 된 다른 PC라면 `winget install Python.Python.3.12` 로 설치하세요.)

## 1. 계정 설정 — `src/config/accounts.yaml` 만들기

`src/config/accounts.yaml.example`을 복사해 `src/config/accounts.yaml`로 만들고 계정을 채워 넣으세요.
`src/config/accounts.yaml`은 절대 git이나 공유 폴더에 올리지 마세요.

```bash
cp src/config/accounts.yaml.example src/config/accounts.yaml
```

`type`(gmail/naver/outlook), `user`, `password` 세 값을 계정마다 채우면 됩니다.
**같은 type을 여러 번 적으면 계정을 여러 개(예: gmail 2개) 등록할 수 있습니다.**
쓰지 않는 계정은 그 블록을 삭제하거나 앞에 `#`을 붙여 주석 처리하세요.

```yaml
accounts:
  - type: gmail
    user: you@gmail.com
    password: "xxxx xxxx xxxx xxxx"

  - type: gmail
    user: you.second@gmail.com
    password: "xxxx xxxx xxxx xxxx"

  - type: naver
    user: you@naver.com
    password: "..."

  - type: outlook
    user: you@outlook.com
    password: "..."
```

### Gmail 앱 비밀번호 발급
1. Google 계정 → 보안 → 2단계 인증을 먼저 켭니다 (필수).
2. https://myaccount.google.com/apppasswords 접속
3. 앱 이름을 아무거나 입력(예: mail-dashboard) 후 생성된 16자리 비밀번호를 `password`에 입력

### Naver 앱 비밀번호 발급
1. 네이버 메일 설정 → POP3/IMAP 설정에서 **IMAP/SMTP 사용**을 켭니다.
2. 네이버 계정 → 보안설정 → 2단계 인증을 켠 상태라면 "환경설정 > 보안설정 > 2단계 인증 > 앱 비밀번호" 에서 발급
3. 2단계 인증을 쓰지 않는다면 네이버 계정 비밀번호를 그대로 `password`에 사용 가능(보안상 2단계 인증 + 앱 비밀번호 권장)

### Outlook 앱 비밀번호 발급
1. https://account.microsoft.com/security 접속, 2단계 인증(이중 인증)을 켭니다 (필수).
2. "고급 보안 옵션" → "앱 암호" 에서 새 앱 암호 생성
3. 생성된 값을 `password`에 입력

## 2. 웹 화면 (`src/admin_app.py`) — 대시보드 / 작업 실행 / 설정

카테고리(`security`/`payment`/`job`/`ad`/`interest`/`notice`/`tools` 등)는 `data/app.db`(SQLite)의
`categories` 테이블에 저장되고, 로컬 웹 화면에서 추가/수정/삭제합니다. 화면에서 저장하면
다음 `fetch_mail.py` 실행부터 바로 반영됩니다 — 파일을 직접 고칠 필요가 없습니다.
`data/app.db`는 카테고리 설정과 원본 메일 로그(`messages`/`action_runs` 테이블, 3절)를
파일 하나에 함께 담고 있습니다(예전엔 `categories.db`/`mail_log.db`로 나뉘어 있었는데
하나로 합쳤습니다).

### 최초 1회 준비

```powershell
python -m pip install -r requirements.txt   # Flask 설치(관리 화면 전용, 파이프라인 본체는 여전히 표준 라이브러리만 사용)
```

(이 프로젝트는 이미 `data/app.db`가 있는 상태로 세팅돼 있습니다. `categories.json` -> `categories.db`
-> `app.db` 병합에 쓰인 일회성 마이그레이션 스크립트들은 이미 실행 완료 후 지웠습니다 —
과정과 이유는 `docs/wiki.md`에 기록돼 있습니다.)

### 실행

```powershell
python src\admin_app.py
```
(또는 `run_admin.bat` 더블클릭) 후 브라우저에서 `http://127.0.0.1:5000` 접속. **로컬(127.0.0.1)
전용이라 인증이 없습니다 — 외부에 노출하지 마세요.**

화면은 상단 nav로 세 곳을 오갑니다:

- **`/` 대시보드** — 일일/주간/월별/연도별/전체 5개 탭. 전부 같은 화면에서 기간만
  바뀌며 `data/app.db`를 그 기간으로 즉시 재쿼리해서 카테고리별 통계·액션 처리 현황·
  계정별 상세를 보여줍니다(IMAP 재조회 없음). 일일/주간/월별 탭은 화면 상단의 ← 이전 /
  다음 → 화살표로 과거 기간을 오갈 수 있습니다(기본값: 오늘/이번 주/이번 달,
  `?date=YYYY-MM-DD`로 기준일 지정 — 미래로는 못 넘어갑니다). 연도별은 이동 기능 없이
  항상 "올해"만 보여줍니다. **"전체" 탭은 고정 기간이 아니라 `data/app.db`에 실제로
  쌓여있는 전체 기간 기준 통계**입니다.
  - 화면 오른쪽 위 **"동기화" 버튼**을 누르면 `fetch_mail.py`를 `--since` 없이(=계정별로
    마지막 저장된 시점부터 이어서, dry-run) 실행하고 같은 화면으로 돌아옵니다. 실제
    메일함을 바꾸는 `--apply`는 여전히 `/tasks`에서만 합니다.
  - 통계 타일과 "카테고리별 상세" 탭, 계정 태그와 계정 카드 안쪽 카테고리는 전부
    건수 내림차순(동률은 이름순)으로 상위 몇 개만 기본 노출하고 나머지는 **"···"를
    누르면 펼쳐집니다**(자바스크립트 없이 CSS만으로 동작).
  - 상단 **계정 태그**(포털 · 이메일 · 총 건수)를 클릭하면 아래 "계정별 상세"의 그
    계정 카드로 포커스가 이동하며 자동으로 펼쳐집니다. **계정 카드 자체를 클릭해도**
    같은 방식으로 펼쳐지고, 그 안에서 카테고리 필터 칩을 눌러가며 **실제
    페이지네이션 목록**을 볼 수 있습니다(한 번에 한 계정만 펼쳐집니다 — 다른 계정을
    열면 이전 계정은 자동으로 닫힙니다). 이 상태는 전부 URL 쿼리(`?acct=`/`acct_cat=`/
    `acct_page=`)로 서버가 관리해서, 링크를 그대로 공유해도 같은 화면이 열립니다.
- **`/list` 목록 화면** — 일일/주간/월별/전체 리포트 화면 하단의 "이 기간 목록 보기
  (페이지네이션)" 링크로 들어가는 별도 화면입니다("전체"는 최근 2년 고정 기간). 계정/
  카테고리를 드롭다운으로 골라 여러 계정을 한 번에 필터링하고, "이 조건으로 작업
  실행" 버튼으로 `/tasks`와 바로 연결됩니다 — 위 대시보드의 계정 카드 인라인 목록(한
  계정만, 필터 없이 카테고리 칩만)과는 용도가 다릅니다.
- **`/tasks` 작업 실행** — "새로고침(dry-run)"/"실제 처리(--apply)" 버튼으로 CLI 없이
  파이프라인을 실행하고, "액션 처리" 버튼으로 모달을 열어 그 안에서 메일을 체크하거나
  드래그해서 휴지통/보관/읽음으로 개별 처리합니다(아래 2-2절).
- **`/settings` ⚙️ 설정** — 카테고리 관리(아래)와 메일 계정 관리(연결할 Gmail/Naver/Outlook
  계정을 이 화면에서 추가/수정/삭제 — `src/config/accounts.yaml`을 직접 손으로 고칠 필요
  없음, 처음엔 계정이 하나도 없는 빈 상태로 시작합니다).

카테고리 하나는 이런 필드로 구성됩니다:

- `description`: 이 카테고리가 뭔지 (사람이 읽는 용도, 로직에 영향 없음)
- `action`: 매칭된 메일을 어떻게 처리할지
  - `"trash"` — 휴지통으로 이동 (아래 3-1 참고)
  - `"save"` — 보관 폴더로 이동 (받은편지함에서 빠짐). Gmail은 이미 모든 메일이 `[Gmail]/All Mail`에 있어서, 사실상 받은편지함 라벨만 떼는 것과 같습니다(웹 UI의 "보관" 버튼과 동일한 효과).
  - `"read"` — 읽음(`\Seen`)으로 표시만 하고 위치는 그대로 둠
  - `"keep"` — 아무것도 안 함, 리포트에만 표시 (기본값)
- `priority`: `"HIGH"` / `"NORMAL"` / `"LOW"`. 메일 하나가 여러 카테고리에 걸릴 수 있을 때, priority가 높은 카테고리부터 검사해서 먼저 매칭되는 쪽으로 확정됩니다. 예: `ad`가 `HIGH`라서, 광고 메일 제목에 우연히 "AI" 같은 관심사 키워드가 섞여 있어도 광고로 먼저 분류됩니다.
- `keywords.senders` / `keywords.title` / `keywords.contents`: 이 순서대로 검사합니다. 발신인 주소에 걸리면 제목/본문은 안 보고, 제목에 걸리면 본문은 안 봅니다.
  - **`senders`는 `aaa@aaa.aaa.com`처럼 완전한 이메일 주소와 정확히 일치할 때만 매칭됩니다** (부분 문자열이나 도메인 매칭이 아닙니다 — 대소문자는 무시). 도메인이나 `noreply` 같은 일반 키워드로도 매칭시킬 수는 있지만, 그렇게 하면 무관한 발신인까지 너무 광범위하게 잡힙니다. 실제로 `ad.keywords.senders`에 `"noreply"`를 넣어뒀다가 결제/보안 알림 메일까지 전부 광고로 잘못 분류된 적이 있습니다(→ 아래 3-1의 휴지통 이동 대상에 잘못 들어감). 그래서 `senders`는 **정확한 전체 주소만** 쓰고, 넓게 잡고 싶으면 `title`(제목 키워드)을 쓰세요 — 부분 문자열 매칭이라 도메인 단위로 넓게 잡고 싶은 경우에도 `title`이나 `contents`로 처리하는 걸 권장합니다.
  - `title`은 부분 문자열 매칭입니다(대소문자 무시) — 제목에 그 문자열이 포함되기만 하면 걸립니다.
  - `contents`(본문 키워드)도 부분 문자열 매칭이고, **정말 필요할 때만 쓰세요.** 이게 비어있는 카테고리만 있으면 본문 조회 자체를 안 해서 지금처럼 빠르지만, 어떤 카테고리든 `contents`에 키워드를 하나라도 넣으면, 발신인/제목으로 못 거른 메일에 한해 메일 본문을 추가로 내려받아 검사합니다(계정별로 한 번 더 IMAP 조회가 생김 — 그 메일들만).
- 새 카테고리는 관리 화면의 "+ 카테고리 추가"로 만듭니다. `senders`/`title`/`contents`는 한 줄에 하나씩 입력합니다.
- 지금 기본 카테고리는 `security`(계정 보안 알림)/`payment`(결제·구독)/`job`(취업·채용)/`ad`(광고)/`interest`(IT 관심사)/`notice`(공공기관·금융기관 등의 공지/약관개정/고지서, keep)/`tools`(Notion/Docker/Google Cloud 등 개발·SaaS 도구 제품 알림, keep) 7개입니다. `security`/`payment`/`job`의 `senders`는 실제 받은 메일에서 뽑은 정확한 주소 목록이라, LinkedIn/원티드/사람인처럼 계속 새 발신 주소가 생기는 서비스는 새 주소를 못 잡을 수 있습니다 — 이런 경우 `title` 키워드(예: `job`의 "채용", "지원하신")가 폴백 역할을 합니다.
- 관리 화면의 "categories.json으로 내보내기" 버튼을 누르면 `data/app.db`의 `categories` 테이블 현재 상태를 `src/config/categories.json`으로 저장합니다 — git으로 변경 이력을 비교하기 좋은 사람이 읽는 백업용이고, **파이프라인은 더 이상 이 JSON 파일을 읽지 않습니다** (`data/app.db`가 유일한 소스입니다).

### 2-1. 작업 실행 화면 (`/tasks`) — CLI 없이 버튼으로

- **새로고침 (dry-run)** — `fetch_mail.py`(dry-run) → `generate_html.py`를 순서대로 실행하고,
  실행 로그를 같은 페이지에 보여줍니다.
- **실제 처리 (--apply)** — 같은 흐름을 `--apply`로 실행합니다. 실제로 메일함을 바꾸는
  버튼이라 클릭하면 확인창이 한 번 뜹니다.
- 두 버튼 다 클릭하고 나서 완료될 때까지(계정 수에 따라 몇십 초~1~2분) 페이지가 그대로
  대기합니다 — 백그라운드 처리 없이 동기 실행이라 개인용 로컬 도구치고는 이게 제일 단순합니다.
- 버튼 아래 "마지막 실행" 박스에 `fetch_mail.py`/`generate_html.py` 각각의 성공/실패와
  콘솔 출력이 그대로 표시됩니다(서버를 껐다 켜면 이 표시는 초기화되지만, 대시보드 자체와
  `action_runs` 기록은 `data/app.db`에 남아있어서 안 사라집니다).

### 2-2. 개별 메일 액션 처리 — 계정/카테고리로 좁힌 뒤 모달에서 체크/드래그

같은 `/tasks` 화면 아래쪽에서 계정/카테고리로 대상을 좁히고 "액션 처리 →"를 누르면
모달이 뜹니다(이미 처리된 휴지통/보관 메일은 대상에서 자동으로 빠집니다). 모달 안에서:

- 체크박스로 여러 건을 고른 뒤 🗑️휴지통 이동 / 📥보관 / ✅읽음 표시 칸을 클릭하면 선택한
  건 전부 처리됩니다.
- 항목 하나를 체크 없이 바로 칸으로 **드래그**하면 그 한 건만 처리됩니다.
- 실제로 메일함을 바꾸므로 클릭/드롭 시 확인창이 한 번 뜹니다. 카테고리 자동 규칙
  (`--apply`)과 무관한 수동 액션이고, 처리 결과는 `data/app.db`의 `action_runs`에 "수동
  선택 처리"로 별도 기록됩니다.
- 처리에 성공한 메일은 `messages` 테이블의 `status`(trashed/archived)·`is_read` 컬럼이
  즉시 갱신되어, 이후 대시보드/전체 목록에 🗑️/📥/✅ 배지로 표시됩니다(재조회 없이 그
  자리에서 아는 결과로 갱신 — "동기화"). 사용자가 다른 클라이언트(웹메일 등)에서 직접
  지운 메일은 다음 `fetch_mail.py` 실행 때 자동으로 `data/app.db`에서도 정리됩니다(조회한
  구간에서 더 이상 안 보이는, 아직 `active` 상태인 메일만 대상).

## 3. 수동 실행 테스트

```powershell
python src\fetch_mail.py
python src\generate_html.py
```

(PATH에 등록했다면 `python src/fetch_mail.py` 로 짧게 써도 됩니다.)

`fetch_mail.py`는 조회한 원본 메일(제목/발신인/날짜/uid)을 `data/app.db`(SQLite)의 `messages` 테이블에 누적 저장합니다 — 같은 `(계정, uid)`는 중복 저장되지 않습니다. `--since` 없이 실행하면 **계정별로 `data/app.db`에 저장된 마지막 메일 시점부터** 이어서 조회합니다(신규 계정은 어제부터) — 매일 자동 실행을 며칠 건너뛰어도(PC를 꺼뒀다 켠 경우 등) 그 사이 메일을 놓치지 않습니다. 특정 시점부터 강제로 다시 조회하고 싶으면 `--since YYYY-MM-DD`로 모든 계정에 그 날짜를 명시적으로 지정할 수 있습니다(백필용).

`generate_html.py`는 이 DB에서 원하는 기간을 쿼리해서 **그 시점의** `categories` 규칙(같은 `data/app.db`)으로 다시 분류해 `data/dashboard.html`(대시보드 조각)을 만듭니다. 카테고리 규칙을 나중에 바꿔도 IMAP을 다시 조회하지 않고 과거 메일을 재분류할 수 있다는 뜻입니다.

```powershell
python src\generate_html.py                                   # 기본: 어제 0시 ~ 지금
python src\generate_html.py --since 2026-08-01 --until 2026-08-16  # 임의 기간 재생성
```

`dashboard.html`은 `<title>`/`<style>`/본문만 있는 조각(fragment)입니다 — Claude Artifact 게시 도구가 `<html>/<head>/<body>`를 감싸서 배포하는 형식에 맞춘 것입니다.
Gmail/Naver 계정 메일은 제목을 누르면 해당 메일을 바로 여는 링크가 걸립니다.
- Gmail: IMAP 확장 속성 `X-GM-THRID`(내부 스레드 ID)로 `https://mail.google.com/mail/?authuser=<계정 이메일>#all/<hex-id>` 형태 링크를 만듭니다. `authuser`를 계정 이메일로 명시하는 게 중요합니다 — `/mail/u/0/...`처럼 슬롯 번호를 고정하면 브라우저에 Google 계정이 여러 개 로그인돼 있을 때 엉뚱한 계정으로 열려서 그 계정엔 없는 스레드라 조용히 전체 메일함 목록으로 빠지는 버그가 있었습니다(실제로 재현·확인).
- Naver: 웹메일의 메일 읽기 URL(`https://mail.naver.com/v2/read/0/<id>`)의 숫자 부분이 IMAP UID와 정확히 같다는 걸 확인해서, 별도 조회 없이 UID 그대로 링크를 만듭니다.

Outlook은 아직 미연동 상태라 위와 같은 링크를 확인하지 못했습니다(연동 시 확인 예정) — 지금은 계정이 없어서 텍스트로만 표시될 대상도 없습니다.

과거의 `data/report_*.json`/`data/latest.json` 파일은 더 이상 생성되지 않고, 2026-01-01부터의 전체 과거 메일을 실제 IMAP 재조회로 `data/app.db`에 백필한 뒤 삭제했습니다 — `data/app.db`가 유일한 원본 데이터 소스입니다.

### 3-1. 카테고리 자동 처리 — dry-run과 `--apply`

`fetch_mail.py`는 `action`이 `"keep"`이 아닌 카테고리에 매칭된 메일을 실제로 처리할 수 있습니다(`trash`=휴지통 이동, `save`=보관 이동, `read`=읽음 표시).

```powershell
python src\fetch_mail.py            # dry-run(기본값) — 실제로 처리하지 않고 몇 건이 대상인지만 로그/리포트에 남김
python src\fetch_mail.py --apply    # 실제로 처리
```

**`--apply`는 실제 계정의 메일을 진짜로 옮기거나 표시하는 명령입니다.** `trash`/`save`는 완전삭제(영구 삭제)가 아니라 폴더 이동만 하므로(휴지통은 프로바이더가 보통 30일 정도 보관), 처음 쓸 때는 `--apply` 없이 며칠 dry-run 결과(`data/app.db`의 `action_runs` 테이블, 대시보드의 "액션" 섹션)를 확인한 뒤 적용하는 걸 권장합니다.

## 4. 매일 새벽 자동 실행 (Windows 작업 스케줄러)

> **참고**: `desktop/` Electron 앱(§6, 개발 중)이 완성되면 이 절과 §5는 앱 내부
> 스케줄러로 대체됩니다. 아래는 앱을 아직 안 쓸 때의 방법입니다.

로컬 스크립트는 토큰을 쓰지 않으므로 05:50 같은 새벽 시간에 실행해 데이터를 미리 만들어 둡니다.

두 스크립트(`fetch_mail.py` → `generate_html.py`)를 순서대로 실행하는 `run_daily.bat`이 프로젝트 루트에 이미 있습니다.
`schtasks`의 `/TR`은 명령어 하나만 그대로 실행하고 `&&`를 해석하지 않기 때문에, 배치 파일 하나로 감싸는 편이 따옴표 중첩 없이 안전합니다.

```powershell
schtasks /Create /TN "MailDashboard-Fetch" /TR "<repo>\run_daily.bat" /SC DAILY /ST 05:50
```

## 5. 오전 6시 아티팩트 게시 (Claude 예약 작업)

`data/dashboard.html`을 실제 아티팩트 링크로 만들어 알림받는 부분만 Claude가 짧게 수행합니다
(이미 집계된 파일을 읽어서 게시만 하므로 토큰 소모가 최소화됩니다).

OMC `schedule` 스킬 또는 `/schedule` 로 다음 작업을 오전 6:00 매일 반복으로 등록하세요:

> `<repo>\data\dashboard.html` 파일을 읽어서
> Artifact로 게시하고(favicon 📬), 링크를 알려줘. 제목은 대시보드 상단의 날짜 범위를 참고해서 지어줘.

## 6. 데스크톱 앱 (`desktop/`)

`ai-mail-agent`를 트레이 상주 Electron 앱으로 감쌉니다. 파이프라인(`src/`)은 그대로,
Electron이 `admin_app.py`(Flask)를 자식 프로세스로 띄우고 창에 대시보드를 로드하며,
앱 내부 스케줄러가 매일 정해진 시각에 동기화(dry-run)를 돌립니다. Windows 작업
스케줄러(§4)와 Claude 예약 게시(§5)를 대체합니다.

**개발 실행:**
```powershell
cd desktop
npm install          # 최초 1회 (electron 다운로드)
npm start            # 앱 실행
```

**포터블 exe 빌드 (완전 독립 실행):**
```powershell
cd desktop
npm run icons        # (선택) 아이콘 다시 생성
npm run dist         # prepare_python.py(embed Python + Flask vendor) → electron-builder
                     # → desktop/dist/ai-mail-agent-<버전>-portable.exe
```
빌드된 exe는 **설치·외부 Python·저장소 체크아웃 없이** 더블클릭으로 실행됩니다.
Python 3.11(embed) + Flask가 exe 안에 번들되고, 파이프라인 `src/`도 함께 들어갑니다.
데이터(`app.db`, `dashboard.html`)는 `%APPDATA%\ai-mail-agent-desktop\data\`에 생깁니다.
첫 실행 시 기존 `app.db`(예전 CLI/개발 버전에서 쓰던 것)를 가져올지 물어봅니다.

> **⚠️ 코드 서명 (TODO / 개인용)**: Windows **Smart App Control**이 켜져 있으면 자체
> 서명된 exe의 실행을 차단합니다("애플리케이션 제어 정책에서 이 파일을 차단했습니다").
> 공인 CA에서 코드 서명(EV) 인증서 구매가 어려우면 대체 방법:
> 1. **개인 PC 한정** — 자체 서명 인증서를 만들어 `LocalMachine\TrustedPublisher` +
>    `TrustedRoot` 저장소에 넣으면 그 PC에서 SAC/SmartScreen이 exe를 신뢰합니다
>    (`New-SelfSignedCertificate` → `signtool sign` → `Import-Certificate`).
>    `electron-builder`의 `win.certificateFile`/`certificatePassword`로 자동 서명 가능.
> 2. **Azure Trusted Signing** — Microsoft 클라우드 서명 서비스(월 ~$10, 개인/조직 신원
>    확인 필요). EV 인증서 구매 없이 SmartScreen/SAC가 인정하는 서명.
> 3. **SAC 예외 / 끄기** — 설정 > 개인정보 및 보안 > Windows 보안 > 앱 & 브라우저 제어.
> 4. **`npm start`(개발 모드)** — SAC 영향 없음. 단일 사용자면 이걸로 충분할 수 있습니다.

> **실행 모드 3가지** (`main.js` `resolveRuntime()`):
> - `npm start` → **dev**: `desktop/..`의 소스, `config.pythonPath`, `repo/data`
> - 패키징 exe (기본) → **bundled**: 번들 Python·`src/`, 데이터는 `%APPDATA%\...\data`
> - 패키징 exe + `config.repoPath` 지정 → **checkout**: 그 체크아웃의 소스/data로 실행(파워유저)

- **설정 파일**: `%APPDATA%\ai-mail-agent-desktop\config.json`
  (`pythonPath` / `repoPath`(선택, checkout 모드) / `flaskPort` /
  `schedule.{enabled,hour,minute}` / `runOnStartupIfStale` / `autoLaunch`).
- **트레이 메뉴**: 열기 · 대시보드 새로고침 · 지금 동기화 · 자동 실행(스케줄) 토글 ·
  다음/마지막 실행 표시 · **계정 설정…** · **로그인 시 자동 실행** 토글(패키징 exe에서만) ·
  **업데이트 확인…** · 종료.
- **자동 실행은 dry-run(`/sync`)만** 합니다 — 실제 메일함을 바꾸는 `--apply`는 하지
  않습니다(창의 "작업 실행" 화면에서 수동으로만).
- **자격증명**: 앱을 처음 켜면 `src/config/accounts.yaml`을 읽어 **암호화 볼트**
  (`%APPDATA%\ai-mail-agent-desktop\accounts.enc`, Windows DPAPI — 이 PC의 사용자
  계정으로만 복호화)로 자동 이관합니다. 이후 계정 추가/수정/삭제는 트레이 **계정 설정…**
  창에서 하고, 파이프라인에는 환경변수(`MAIL_AGENT_ACCOUNTS`)로만 전달됩니다.
  `accounts.yaml`은 백업 겸 CLI 폴백으로 남습니다(git 제외). `safeStorage`를 못 쓰는
  환경이면 볼트 없이 `accounts.yaml`로 동작합니다.
- **메일 로그 암호화**: 번들 실행 첫 부팅에 데이터 폴더(`%APPDATA%\...\data`)에
  Windows **EFS**를 겁니다(`cipher /e`) — `app.db`(발신인·제목)와 `dashboard.html`이
  이 Windows 계정으로만 복호화됩니다. EFS를 못 쓰는 환경(Windows Home 등)이면 조용히
  건너뜁니다 — 그 경우 전체 디스크 암호화(BitLocker)를 켜는 걸 권장합니다. 개발/CLI
  실행(`npm start`, `python src/…`)의 저장소 `data/`는 건드리지 않습니다.
- **업데이트**: 트레이 "업데이트 확인…" 또는 부팅 30초 뒤 자동으로 GitHub Releases의
  최신 태그를 확인합니다(`desktop/updater.js`, 외부 패키지 없음). 새 버전이 있으면 알림 +
  릴리스 페이지를 엽니다 — 새 exe를 받아 기존 것과 교체하세요.
  릴리스하려면: `desktop/package.json`의 `repository`/`build.publish`에서 `OWNER`를
  실제 GitHub owner로 바꾸고, 저장소에 원격 추가 후 `npm version patch && npm run release`
  (electron-builder가 `latest.yml` + portable exe를 GitHub Releases에 게시).
- 진행 단계: **Milestone 7 완성** (Phase 1 Flask 감싸기 · Phase 2 스케줄러 · Phase 3
  자격증명 볼트 · Phase 4 포터블 패키징·자동시작 · Phase 5 Python 번들·독립 실행 +
  EFS·페이지네이션·아이콘·업데이트 확인). 백로그: 코드 서명(위 ⚠️), stdin 자격증명 전달.
  상세는 `docs/wiki.md`의 "Electron 데스크톱 앱" 절.

## 폴더 구조

```
ai-mail-agent/
  run_daily.bat            # 스케줄러가 호출하는 실행 순서(fetch → generate) 래퍼
  run_admin.bat            # 웹 화면(대시보드/작업 실행/설정) 실행
  requirements.txt         # admin_app.py 전용 의존성(Flask)
  src/fetch_mail.py        # CLI 진입점 — 아래 모듈들을 엮어서 실행만 함
  src/accounts.py          # 계정 로딩 — MAIL_AGENT_ACCOUNTS(볼트 주입) 우선, accounts.yaml 폴백 + CRUD
  src/mail_fetch.py        # IMAP 조회 (UID 기반, 날짜 청크/배치 fetch)
  src/classify.py          # priority/키워드(발신인→제목→본문) 기반 분류
  src/actions.py           # 분류 결과에 따른 메일함 액션 (휴지통 이동/보관/읽음 표시)
  src/generate_html.py     # app.db를 쿼리·재분류해서 대시보드 HTML 조각 생성
  src/mail_log_store.py    # app.db의 messages/action_runs 테이블 CRUD/쿼리 + 상태 동기화
  src/config_store.py      # app.db의 categories 테이블 CRUD
  src/web_style.py         # admin_app.py/generate_html.py 공유 CSS(단일 라이트 팔레트) + nav
  src/admin_app.py         # 대시보드(/) + 작업 실행(/tasks) + 설정(/settings) 로컬 웹 UI (Flask, 127.0.0.1 전용)
  src/config/accounts.yaml        # 계정 자격증명 (설정 화면에서 관리, git 제외)
  src/config/accounts.yaml.example
  src/config/categories.json      # 사람이 읽는 백업(관리 화면의 "내보내기"로 갱신, 파이프라인은 안 읽음)
  data/app.db             # categories + messages + action_runs 전부 담은 단일 SQLite 파일(git 제외), 2025-12-31~현재 전 구간 백필 완료
  data/dashboard.html    # 최신 대시보드 조각
  desktop/                # Electron 데스크톱 셸 (Node — src/ 파이프라인 무변경, 자식 프로세스로 호출)
  desktop/main.js         #   창 · 트레이 · Flask 수명주기 · 자격증명 볼트 주입
  desktop/flask.js        #   admin_app.py 자식 spawn/헬스폴링/restart
  desktop/scheduler.js    #   앱 내부 스케줄러 (매일 06:00 dry-run /sync, 자체 구현)
  desktop/vault.js        #   safeStorage(DPAPI) 자격증명 볼트 — userData/accounts.enc
  desktop/config.js       #   앱 설정 영속 — userData/config.json
  desktop/renderer/settings.html  # 계정 CRUD 창
  desktop/updater.js      #   GitHub Releases 업데이트 확인 (외부 패키지 없음)
  desktop/assets/make_icons.py    # 아이콘 생성 (표준 라이브러리 PNG/ICO 인코더 — 봉투 + 미읽음 점)
  desktop/scripts/prepare_python.py  # 빌드 전처리 — embed Python 다운로드 + Flask vendor → pybundle/
  desktop/scripts/run-python.js      # npm 스크립트용 Python 러너 (WindowsApps 스텁 회피)
  desktop/package.json    #   electron-builder 설정 (win portable + extraResources + publish:github)
  src/app_paths.py        # 데이터 디렉터리 해석 (MAIL_AGENT_DATA_DIR / repo의 data/)
```
