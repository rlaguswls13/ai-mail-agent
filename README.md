# ai-mail-agent - 메일 분류 & 대시보드

[![CI](https://github.com/rlaguswls13/ai-mail-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/rlaguswls13/ai-mail-agent/actions/workflows/ci.yml)

Gmail / Naver / Outlook 메일함을 로컬에서 IMAP으로 조회해, 규칙 기반으로 카테고리를
분류하고(광고 / 보안 / 결제 / 채용 / 관심사 등), 카테고리별 `action`(휴지통 이동 /
보관 / 읽음 표시)에 따라 메일함을 정리할 수 있는 **개인용 로컬 도구**입니다.
집계 결과는 로컬 웹 대시보드(또는 트레이 상주 데스크톱 앱)로 봅니다.

- **LLM 토큰을 전혀 쓰지 않습니다.** 분류는 순수 Python(표준 라이브러리)으로 처리됩니다.
- 분류 순서: 카테고리를 **priority(HIGH → NORMAL → LOW)** 순으로, 카테고리 안에서는
  **발신인 → 제목 → 본문** 순으로 키워드를 매칭합니다.
- 카테고리 설정과 조회한 원본 메일은 전부 `data/app.db` 하나(SQLite)에 저장됩니다.

> ⚠️ **개인용 프로젝트입니다.** 무보증(MIT), 데스크톱 빌드는 코드 서명이 안 돼 있습니다
> (§5의 Smart App Control 참고). `--apply`는 실제 메일함을 바꾸므로 처음엔 반드시
> dry-run으로 며칠 결과를 확인한 뒤 쓰세요.

## 코드 구조

3개의 Python 패키지로 나뉩니다 (`packages/`):

| 패키지 | 위치 | 런타임 의존성 | 역할 |
| :-- | :-- | :-- | :-- |
| `mail-core` | `packages/mail-core/mail_core/` | 없음 (표준 라이브러리만) | IMAP 조회 + 규칙 기반 분류 + 메일함 액션 |
| `mail-app` | `packages/mail-app/mail_app/` | 없음 (형제 `mail-core`) | `app.db` 영속 · 파이프라인 오케스트레이션 · 대시보드 리포트 · CLI |
| `admin-ui` | `packages/admin-ui/admin_ui/` | `flask` | 로컬 관리 웹 UI (127.0.0.1 전용) |

`desktop/`은 위 파이프라인을 감싸는 Electron 트레이 앱입니다(§5). 파이프라인 패키지는
그대로 두고 `python -m admin_ui`를 자식 프로세스로 호출합니다.

## 0. Python 설치

Python **3.11 이상**이 필요합니다.

```powershell
winget install Python.Python.3.12       # Windows
# 또는 https://www.python.org/downloads/ 에서 설치 (설치 시 "Add to PATH" 체크)
```

임베더블 배포판 등 PATH에 없는 Python을 쓰면, 아래 배치 파일들은 `set PY=C:\path\to\python.exe`
로 지정할 수 있습니다.

### 패키지 개발 설치 (editable)

3개 패키지를 editable로 설치하면 어디서든 `python -m mail_app.fetch_mail` /
`python -m admin_ui` 가 동작합니다.

```powershell
python -m pip install hatchling      # 빌드 백엔드 (dev 도구, 1회)
dev-install.bat                       # = pip install --no-build-isolation -e packages/mail-core -e packages/mail-app -e packages/admin-ui
```

editable 설치를 건너뛰어도 `run_admin.bat` / `run_daily.bat` 은 `PYTHONPATH` 를 스스로
잡아 동작합니다.

### 테스트

```powershell
python -m pip install pytest
python -m pytest -q        # 저장소 루트에서 - 3개 패키지 테스트를 한 번에
```

표준 라이브러리만으로 IMAP/SQLite 를 흉내 내는 회귀 테스트라 실제 계정·네트워크가
필요 없습니다. `push`/PR 마다 GitHub Actions(`.github/workflows/ci.yml`)가
Windows·Linux × Python 3.11/3.12/3.13 으로 같은 스위트를 돌립니다.

## 1. 계정 설정 - `config/accounts.yaml`

`config/accounts.yaml.example`을 복사해 `config/accounts.yaml`로 만들고 계정을 채웁니다.
**`config/accounts.yaml`은 절대 git이나 공유 폴더에 올리지 마세요** (`.gitignore`에 이미 제외).

```bash
cp config/accounts.yaml.example config/accounts.yaml
```

`type`(gmail / naver / outlook), `user`, `password` 세 값을 계정마다 채웁니다.
같은 `type`을 여러 번 적으면 계정을 여러 개(예: gmail 2개) 등록할 수 있습니다.
쓰지 않는 블록은 삭제하거나 `#`으로 주석 처리하세요.

Outlook은 비밀번호가 아니라 OAuth2를 씁니다 - `password`는 아무 값이나 두고(무시됨)
`type: outlook`, `user`만 맞추면 됩니다. 최초 1회 브라우저 로그인은 아래 §1-1 참고.

```yaml
accounts:
  - type: gmail
    user: you@gmail.com
    password: "xxxx xxxx xxxx xxxx"

  - type: naver
    user: you@naver.com
    password: "..."
```

데스크톱 앱을 쓰면 계정은 암호화 볼트(§5)에서 관리되고 `accounts.yaml`은 백업/CLI
폴백으로만 남습니다.

### 앱 비밀번호 발급

- **Gmail**: Google 계정 → 보안 → 2단계 인증 활성화 → <https://myaccount.google.com/apppasswords>
  에서 16자리 앱 비밀번호 생성.
- **Naver**: 네이버 메일 설정에서 **IMAP/SMTP 사용** 활성화 → (2단계 인증 사용 시)
  보안설정에서 앱 비밀번호 발급. IMAP 토글과 POP3 토글은 별개이니 IMAP을 켜세요.
- **Outlook**: 앱 비밀번호는 안 됩니다. MS가 2024년 9월 Outlook.com 개인 계정의
  Basic Auth(앱 비밀번호 포함)를 없애서 IMAP이 OAuth2(`AUTH=XOAUTH2`)만 받습니다 - §1-1 참고.

### 1-1. Outlook 최초 로그인 (OAuth2)

`config/accounts.yaml`에 `type: outlook` 계정을 넣은 뒤 한 번 실행합니다:

```bash
python -m mail_app.outlook_login
```

안내된 코드를 <https://www.microsoft.com/link>에 입력하고 로그인 → 메일 접근에
동의하면(앱 이름은 "Mozilla Thunderbird"로 표시 - 공개 client_id 재사용)
refresh token이 `config/outlook_token.json`(git 제외)에 저장됩니다. 이후
`fetch_mail` 등이 access token을 자동 갱신하므로 다시 로그인할 일은 없습니다
(갱신 실패 시 `python -m mail_app.outlook_login --force`).

## 2. 웹 화면 (`admin_ui`) - 대시보드 / 작업 실행 / 설정

카테고리(`security` / `payment` / `job` / `ad` / `interest` / `notice` / `tools` 등)는
`data/app.db`의 `categories` 테이블에 저장되고, 로컬 웹 화면에서 추가/수정/삭제합니다.
저장하면 다음 파이프라인 실행부터 바로 반영됩니다.

`config/categories.json.example`에 기본 카테고리 규칙 예시가 있습니다. 파이프라인은
이 JSON을 읽지 않고 `data/app.db`만 봅니다 - JSON은 사람이 읽는 참고용입니다(관리
화면의 "categories.json으로 내보내기"로 현재 상태를 파일로 덤프할 수 있습니다).

### 실행

```powershell
python -m admin_ui        # 또는 run_admin.bat
```

브라우저에서 `http://127.0.0.1:5000` 접속. **로컬(127.0.0.1) 전용이라 인증이 없습니다 -
외부에 노출하지 마세요.**

화면:

- **`/` 대시보드** - 일일 / 주간 / 월별 / 연도별 / 전체 탭. 같은 화면에서 기간만 바뀌며
  `data/app.db`를 그 기간으로 즉시 재쿼리(IMAP 재조회 없음). 일일/주간/월별 탭은
  `← 이전 / 다음 →` 화살표로 과거 기간 이동(`?date=YYYY-MM-DD`). 오른쪽 위 **"동기화"**
  버튼은 `fetch_mail`을 `--since` 없이(계정별 마지막 저장 시점부터) dry-run 실행합니다.
  통계 타일·카테고리 탭·계정 카드는 건수 내림차순 상위 N개만 노출하고 "···"로 펼칩니다
  (JS 없이 CSS만). 계정 카드를 클릭하면 그 계정만 카테고리 필터 + 페이지네이션 목록으로
  펼쳐집니다(상태는 URL 쿼리로 관리 - 링크 공유 가능).
- **`/list` 목록** - 계정/카테고리 드롭다운 필터 + 페이지네이션. "이 조건으로 작업 실행"
  버튼으로 `/tasks`와 연결.
- **`/tasks` 작업 실행** - "새로고침(dry-run)" / "실제 처리(--apply)" 버튼으로 CLI 없이
  파이프라인 실행. 아래 페이지네이션 목록에서 메일을 체크박스로 골라(페이지를 넘겨도
  선택 유지 - 이 브라우저에 저장) "선택 실행"으로 요약 확인 후 휴지통/보관/읽음
  개별 처리(§2-2).
- **`/vault` 정리함** - 처리된 메일을 다시 보는 화면. `보관함`(save 처리) 탭에서 골라
  **원래 받은편지함으로 되돌리기**, `휴지통`(trash 처리) 탭에서 골라 **서버에서 영구
  삭제**(복구 불가). 메일이 옮겨지면 UID가 바뀌므로 `Message-ID`로 대상 폴더에서 다시
  찾습니다 - `Message-ID`가 없는 예전 메일은 IMAP 반영 없이 목록(DB)에서만 정리됩니다.
- **`/settings`** - 카테고리 관리 + 메일 계정 관리(`accounts.yaml`을 직접 고칠 필요 없음).

카테고리 필드:

- `description`: 사람이 읽는 설명 (로직에 영향 없음)
- `action`: `"trash"`(휴지통) / `"save"`(보관 폴더로 이동) / `"read"`(읽음 표시만) /
  `"keep"`(리포트에만 표시, 기본값)
- `priority`: `"HIGH"` / `"NORMAL"` / `"LOW"` - 한 메일이 여러 카테고리에 걸릴 때 높은
  priority부터 검사해 먼저 매칭되는 쪽으로 확정.
- `keywords.senders` / `keywords.title` / `keywords.contents`: 이 순서로 검사. 발신인에
  걸리면 제목/본문은 안 보고, 제목에 걸리면 본문은 안 봅니다.
  - **`senders`는 완전한 이메일 주소와 정확히 일치할 때만 매칭**(부분 문자열/도메인 매칭
    아님, 대소문자 무시). `noreply` 같은 광범위한 값을 넣으면 무관한 메일까지 잡히니,
    넓게 잡으려면 `title`(부분 문자열 매칭)을 쓰세요.
  - `contents`(본문)에 키워드를 하나라도 넣은 카테고리가 있으면, 발신인/제목으로 못 거른
    메일에 한해 본문을 추가로 내려받아 검사합니다(계정별 IMAP 조회 1회 추가). 비워 두면
    본문 조회 자체를 안 해서 더 빠릅니다.

### 2-1. 작업 실행 화면 (`/tasks`)

- **새로고침 (dry-run)** - `python -m mail_app.fetch_mail`(dry-run) →
  `-m mail_app.generate_html` 순서 실행, 로그를 같은 페이지에 표시.
- **실제 처리 (--apply)** - 같은 흐름을 `--apply`로. 확인창이 한 번 뜹니다.
- 백그라운드 없이 동기 실행이라, 완료될 때까지(계정 수에 따라 수십 초~1~2분) 페이지가
  대기합니다.

### 2-2. 개별 메일 액션 처리

`/tasks` 화면 아래쪽 페이지네이션 목록에서(이미 처리된 메일은 자동 제외) 처리할 메일을
체크박스로 고릅니다 - 계정/카테고리 필터를 바꾸거나 페이지를 넘겨도 선택은 유지됩니다
(브라우저 `localStorage`). "선택 실행 →"을 누르면 **총 건수 + 카테고리별·계정별 내역**을
요약한 확인 모달이 뜨고, 휴지통/보관/읽음 중 하나를 고르면 확인창을 거쳐 실제로
처리합니다. 결과는 `data/app.db`의 `action_runs`에 "수동 선택 처리"로 기록되고, 처리된
메일은 `/vault`(§화면 목록)에서 되돌리거나 영구 삭제할 수 있습니다.

## 3. CLI 실행

```powershell
python -m mail_app.fetch_mail                 # dry-run - 대상 건수만 로그/DB에 기록
python -m mail_app.fetch_mail --apply         # 실제로 처리 (휴지통/보관/읽음)
python -m mail_app.generate_html              # 기본: 어제 0시 ~ 지금
python -m mail_app.generate_html --since 2026-08-01 --until 2026-08-16   # 임의 기간 재생성
```

(editable 설치를 안 했다면 `run_daily.bat` 을 쓰거나 `PYTHONPATH` 에
`packages/mail-core;packages/mail-app;packages/admin-ui` 를 넣으세요.)

- `fetch_mail`은 조회한 원본 메일(제목/발신인/날짜/uid/`Message-ID`)을 `data/app.db`의
  `messages` 테이블에 누적 저장합니다 - 같은 `(계정, uid)`는 중복 저장 안 됨. `--since` 없이
  실행하면 **계정별로 마지막 저장 시점부터** 이어서 조회하므로 며칠 건너뛰어도 메일을
  놓치지 않습니다.
- `generate_html`은 DB를 쿼리해 **현재** `categories` 규칙으로 재분류합니다 - 규칙을
  나중에 바꿔도 IMAP 재조회 없이 과거 메일을 다시 분류할 수 있습니다.
- `dashboard.html`은 `<title>`/`<style>`/본문만 있는 조각(fragment)입니다.
- 메일 제목 딥링크: Gmail은 `X-GM-THRID`, Naver는 웹메일 read URL의 숫자가 IMAP UID와
  일치하는 점을 이용합니다. Outlook 딥링크는 아직 미구현입니다.
- Outlook은 IMAP 로그인에 OAuth2 access token(자동 갱신)을 씁니다 - `mail_core/imap_auth.py`
  가 계정 `type`에 따라 LOGIN(gmail/naver) / XOAUTH2(outlook)를 고릅니다.

### 3-1. `--apply` 주의

`trash`/`save`는 완전 삭제가 아니라 폴더 이동입니다(휴지통은 프로바이더가 보통 30일
보관). 그래도 실제 계정을 건드리므로, 처음엔 `--apply` 없이 며칠 dry-run 결과
(`data/app.db`의 `action_runs`, 대시보드의 "액션" 섹션)를 확인한 뒤 적용하세요.
잘못 옮긴 메일은 `/vault` 정리함에서 되돌릴 수 있습니다(휴지통의 "영구 삭제"만 복구 불가).

## 4. 자동 실행 (Windows 작업 스케줄러)

데스크톱 앱(§5)을 쓰면 앱 내부 스케줄러가 대신하므로 이 절은 필요 없습니다. 앱 없이
쓸 때만:

`fetch_mail` → `generate_html` 을 순서대로 실행하는 `run_daily.bat` 이 루트에 있습니다.

```powershell
schtasks /Create /TN "ai-mail-agent-daily" /TR "<repo>\run_daily.bat" /SC DAILY /ST 05:50
```

(`<repo>`는 이 저장소의 실제 경로. `schtasks /TR`은 `&&`를 해석하지 않으므로 배치
파일로 감싸는 편이 안전합니다.)

## 5. 데스크톱 앱 (`desktop/`)

`ai-mail-agent`를 트레이 상주 Electron 앱으로 감쌉니다. Electron이 `admin_ui`(Flask)를
`python -m admin_ui` 자식 프로세스로 띄우고 창에 대시보드를 로드하며, 앱 내부 스케줄러가
매일 정해진 시각에 동기화(dry-run)를 돌립니다.

**개발 실행:**

```powershell
cd desktop
npm install          # 최초 1회 (electron 다운로드)
npm start
```

**포터블 exe 빌드 (완전 독립 실행):**

```powershell
cd desktop
npm run icons        # (선택) 아이콘 다시 생성
npm run dist         # prepare_python.py(embed Python + Flask + 3패키지 vendor) → electron-builder
                     # → desktop/dist/ai-mail-agent-<버전>-portable.exe
```

빌드된 exe는 **설치·외부 Python·저장소 체크아웃 없이** 실행됩니다. Python 3.11(embed) +
Flask + 3개 파이프라인 패키지가 번들 Python의 `Lib/`에 들어가고 `python -m admin_ui`로
실행됩니다. 데이터는 `%APPDATA%\ai-mail-agent-desktop\data\`에 생기고, 첫 실행 시 기존
`app.db`를 가져올지 물어봅니다.

> **⚠️ 코드 서명 (미적용)**: Windows **Smart App Control**이 켜져 있으면 서명 안 된 exe의
> 실행을 차단합니다("애플리케이션 제어 정책에서 이 파일을 차단했습니다"). 이 프로젝트는
> 코드 서명 인증서가 없습니다. 대응:
> 1. **개인 PC 한정** - 자체 서명 인증서를 만들어 `LocalMachine\TrustedPublisher` +
>    `TrustedRoot` 저장소에 넣으면 그 PC에서 SAC/SmartScreen이 신뢰합니다
>    (`New-SelfSignedCertificate` → `signtool sign` → `Import-Certificate`).
>    `electron-builder`의 `win.certificateFile` / `certificatePassword`로 자동 서명 가능.
> 2. **Azure Trusted Signing** - Microsoft 클라우드 서명 서비스(월 ~$10, 신원 확인 필요).
> 3. **SAC 예외 / 끄기** - 설정 → 개인정보 및 보안 → Windows 보안 → 앱 & 브라우저 제어.
> 4. **`npm start`(개발 모드)** - SAC 영향 없음.

**실행 모드 3가지** (`main.js` `resolveRuntime()`):

- `npm start` → **dev**: `desktop/..`의 `packages/*`, `config.pythonPath`, `repo/data`
- 패키징 exe (기본) → **bundled**: 번들 Python, 데이터는 `%APPDATA%\...\data`
- 패키징 exe + `config.repoPath` 지정 → **checkout**: 지정한 체크아웃의 `packages/*`/data

- **설정 파일**: `%APPDATA%\ai-mail-agent-desktop\config.json`
  (`pythonPath` / `repoPath` / `flaskPort` / `schedule.{enabled,hour,minute}` /
  `runOnStartupIfStale` / `autoLaunch`).
- **자동 실행은 dry-run(`/sync`)만** 합니다 - `--apply`는 창의 "작업 실행" 화면에서 수동으로만.
- **자격증명**: 처음 켜면 `config/accounts.yaml`을 읽어 암호화 볼트
  (`%APPDATA%\ai-mail-agent-desktop\accounts.enc`, Windows DPAPI)로 이관합니다. 이후
  계정 CRUD는 트레이 "계정 설정…" 창에서 하고, 파이프라인에는 관리 UI 프로세스의
  **stdin**으로 복호화된 계정 JSON을 넘깁니다(프로세스 환경 블록에 평문 비밀번호를
  남기지 않기 위함). `safeStorage`를 못 쓰면 볼트 없이 `accounts.yaml`로 동작합니다.
- **메일 로그 암호화**: 번들 실행 첫 부팅에 데이터 폴더에 Windows **EFS**(`cipher /e`)를
  겁니다. EFS를 못 쓰는 환경(Windows Home 등)이면 건너뛰므로 BitLocker를 권장합니다.
- **업데이트**: 트레이 "업데이트 확인…" 또는 부팅 30초 뒤 자동으로 GitHub Releases의
  최신 태그를 확인합니다(`desktop/updater.js`, 외부 패키지 없음). 새 버전이 있으면 알림 +
  릴리스 페이지를 엽니다.
- **릴리스**(메인테이너): `npm version patch && GH_TOKEN=<token> npm run release` -
  electron-builder가 portable exe를 GitHub Releases에 게시합니다
  (`desktop/package.json`의 `build.publish.owner`가 대상 리포).

## 폴더 구조

```
ai-mail-agent/
  LICENSE                  # MIT
  dev-install.bat          # 3개 패키지 editable 설치
  run_daily.bat            # 스케줄러용 실행 래퍼 (fetch → generate)
  run_admin.bat            # 웹 화면 실행 = python -m admin_ui
  requirements.txt         # 설치 안내 주석 + flask (admin-ui 전용)

  packages/mail-core/                    # 레이어 1 - 표준 라이브러리만, 재사용 가능
    mail_core/accounts.py                #   계정 로딩 - MAIL_AGENT_ACCOUNTS 우선, accounts.yaml 폴백 + CRUD
    mail_core/mail_fetch.py              #   IMAP 조회 (UID 기반, 날짜 청크/배치 fetch)
    mail_core/imap_auth.py               #   계정별 IMAP 인증 분기 (LOGIN / Outlook XOAUTH2)
    mail_core/oauth_outlook.py           #   Outlook OAuth2 device-code 로그인 + 토큰 갱신 (stdlib)
    mail_core/classify.py                #   priority/키워드(발신인→제목→본문) 기반 분류
    mail_core/actions.py                 #   메일함 액션 (휴지통 이동/보관/읽음/되돌리기/영구삭제)

  packages/mail-app/                     # 레이어 2 - app.db 영속 · 오케스트레이션 · 리포트 · CLI
    mail_app/fetch_mail.py               #   CLI (python -m mail_app.fetch_mail)
    mail_app/outlook_login.py            #   CLI (python -m mail_app.outlook_login) - Outlook OAuth2 최초 로그인
    mail_app/generate_html.py            #   CLI (python -m mail_app.generate_html)
    mail_app/mail_log_store.py           #   app.db의 messages/action_runs CRUD + 상태 동기화
    mail_app/config_store.py             #   app.db의 categories CRUD
    mail_app/web_style.py                #   admin_ui/generate_html 공유 CSS + nav
    mail_app/app_paths.py                #   데이터/설정 경로 해석 (MAIL_AGENT_DATA_DIR / repo의 data·config)

  packages/admin-ui/                     # 레이어 3 - Flask 로컬 관리 웹 UI (127.0.0.1 전용)
    admin_ui/admin_app.py                #   app 조립 + 전역 요청 가드(cross-origin/DNS-rebinding) + 진입점
    admin_ui/_shared.py                  #   공유 커널 - 상수 · page()/esc() · run_state
    admin_ui/_render.py                  #   공유 메일목록 조회/렌더 · run_pipeline
    admin_ui/dashboard.py                #   블루프린트: 대시보드(/) · 목록(/list) · 동기화(/sync)
    admin_ui/settings.py                 #   블루프린트: 카테고리 · 계정 CRUD (/settings)
    admin_ui/tasks.py                    #   블루프린트: 파이프라인 실행 + 개별 메일 액션 (/tasks)
    admin_ui/vault.py                    #   블루프린트: 보관함/휴지통 되돌리기 · 영구삭제 (/vault)
    admin_ui/__main__.py                 #   python -m admin_ui 진입점

  config/accounts.yaml.example           # 계정 자격증명 템플릿 (실제 파일은 git 제외)
  config/categories.json.example         # 기본 카테고리 규칙 예시 (실제 파일은 git 제외)
  data/app.db                            # categories + messages + action_runs 단일 SQLite (git 제외)
  data/dashboard.html                    # 최신 대시보드 조각 (git 제외)

  desktop/                # Electron 데스크톱 셸 (packages/ 파이프라인 무변경, 자식 프로세스로 호출)
  desktop/main.js         #   창 · 트레이 · Flask 수명주기 · 자격증명 볼트 주입
  desktop/flask.js        #   admin_ui 자식 spawn / 헬스폴링 / restart
  desktop/scheduler.js    #   앱 내부 스케줄러 (매일 dry-run /sync)
  desktop/vault.js        #   safeStorage(DPAPI) 자격증명 볼트
  desktop/config.js       #   앱 설정 영속 - userData/config.json
  desktop/updater.js      #   GitHub Releases 업데이트 확인 (외부 패키지 없음)
  desktop/assets/make_icons.py       # 아이콘 생성 (표준 라이브러리 PNG/ICO 인코더)
  desktop/scripts/prepare_python.py  # 빌드 전처리 - embed Python + Flask + 3패키지 vendor → pybundle/Lib/
  desktop/scripts/run-python.js      # npm 스크립트용 Python 러너
```
