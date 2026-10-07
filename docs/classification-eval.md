# 분류 정확도 평가 & 라벨링 루프

카테고리 분류 규칙(`mail_core/classify.py`)을 **미분류 비율** 프록시가 아니라
카테고리별 precision/recall/F1 로 측정하고 조이기 위한 도구 모음.

분류 방식 자체는 그대로다 — sender/domain/title 키워드 매칭, ML 없음.
바뀐 건 "얼마나 정확한지"를 숫자로 볼 수 있게 된 것뿐.

## 구성 요소

| 도구 | 역할 |
|------|------|
| `admin_ui` `/label` | 메일별 정답 카테고리를 손으로 지정 (미분류만 필터 가능) |
| `python -m mail_app.import_labels` | 공개 데이터셋(SpamAssassin 등) / CSV → 라벨 데이터 |
| `python -m mail_app.classify_eval` | 라벨 데이터로 현재 규칙 채점 (confusion matrix, 오분류 목록) |

## 라벨 데이터 포맷

`data/labels/*.jsonl` (gitignore됨 — 개인 메일 헤더 포함). 한 줄 = 한 JSON:

```json
{"key": "acct:gcd1324@gmail.com:12345", "sender": "...", "subject": "...",
 "label": "job", "source": "manual", "labeled_at": "2026-09-10T..."}
```

- `label` = 카테고리명 또는 `"__none__"` (= 액션 대상 아님 / 미분류가 정답)
- `source` = `manual` 또는 `public:<name>` — eval 로드 시 같은 `key`면 manual 우선

## 워크플로

1. **손 라벨링** — `python -m admin_ui` → `/label` → `?only=uncat` 로 미분류 메일부터
   정답 지정 → "export" 눌러 `data/labels/manual.jsonl` 생성.
2. **공개 데이터 추가** (선택) — SpamAssassin 공개 코퍼스를
   <https://spamassassin.apache.org/old/publiccorpus/> 에서 받아 압축 해제 후:
   ```
   python -m mail_app.import_labels corpus --name sa2003 --spam ./spam --ham ./easy_ham
   ```
   spam→`ad`, ham→`__none__`. 한국어 수제 리스트는 CSV 어댑터:
   ```
   python -m mail_app.import_labels csv --name kr_hand --input labels.csv   # 컬럼: sender,subject,label
   ```
3. **채점** —
   ```
   python -m mail_app.classify_eval
   ```
   macro-F1, 카테고리별 precision/recall, confusion matrix, 오분류 top 20 출력.
   `--json` 기계 판독용, `--min-macro-f1 0.7` 회귀 게이트(미달 시 exit 1).
4. **규칙 조이기** — confusion matrix에서 새는 쌍을 보고
   `scripts/rebuild_rules.py` 로 도메인/키워드/우선순위 조정 → 3번 재실행해 개선 확인.
   회귀 방지: `pytest packages/mail-core/tests/test_classify.py` + eval macro-F1 비교.
5. **과적합 점검** — 규칙을 튜닝에 쓴 라벨로만 재면 점수가 부풀려진다. 두 가지로 확인:
   - `python -m mail_app.classify_eval --since YYYY-MM-DD` — 마지막 튜닝 **이후에 새로 단**
     라벨만 평가(규칙이 한 번도 못 본 데이터. 가장 정직한 지표).
   - `python -m mail_app.classify_eval --holdout 20` — key 해시로 결정적 분할해 튜닝용/홀드아웃
     macro-F1 격차를 출력(0.10 이상이면 과적합 의심). **튜닝 스크립트는 tune 분할만 보고 규칙을
     바꿔야** 의미가 있다 - 이미 전체 라벨로 튜닝한 규칙에는 홀드아웃이 독립적이지 않다
     (2026-09-11 튜닝은 223건 전부 사용 → 이후 신규 라벨에 `--since 2026-09-12` 를 쓸 것).

## 지금 상태

라벨 223건(수동, 2026-09-10~11)으로 2라운드 튜닝 완료(macro-F1 0.94, 로컬 전용). 이 라벨은
튜닝에 전부 쓰였으므로 신규 라벨(`--since 2026-09-12`)로 일반화 성능을 재야 한다.
