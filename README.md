# budget_app — 파일 기반 가계부 콘솔 프로그램

터미널에서 명령어로 수입·지출을 기록하고 조회·검색·월별 요약·예산 관리까지 하는
Python 가계부입니다. 데이터는 JSONL 파일로 영구 저장되며, 저장 파일은 항상
제너레이터로 스트리밍 처리합니다(전체를 메모리에 올리지 않음).

## 요구 환경

- Python **3.10 이상**
- 표준 라이브러리만 사용 (별도 설치 불필요)

## 실행 방법

```bash
python3 -m budget_app <command> [options]
python3 -m budget_app --help              # 전체 도움말
python3 -m budget_app <command> --help    # 명령별 도움말
```

- 모든 옵션은 리눅스 표준 `--` 표기를 씁니다.
- 저장 폴더는 기본 `./data`이며 `--data-dir <경로>`로 바꿀 수 있습니다.

처음 실행하면 저장 파일을 자동 생성하고 기본 카테고리 13개를 등록합니다:

```
$ python3 -m budget_app list
[안내] 저장 파일이 없어 새로 만들었습니다: transactions.jsonl, categories.jsonl, budgets.jsonl, meta.jsonl
[안내] 기본 카테고리 13개를 생성했습니다: 식비, 카페, 유흥, 쇼핑, 교통, 주거, 통신, 의료, 여가, 생활, 급여, 용돈, 기타
[힌트] category add/remove로 언제든 수정할 수 있습니다.
[안내] 거래가 없습니다. add로 첫 거래를 추가해보세요.
```

## 저장 파일 위치와 형식

`./data/` 아래에 **JSONL**(한 줄 = JSON 레코드 하나, UTF-8)로 저장됩니다.

| 파일 | 내용 |
|---|---|
| `transactions.jsonl` | 거래 내역 |
| `categories.jsonl` | 카테고리 목록 |
| `budgets.jsonl` | 월 예산 — 같은 달을 재설정하면 새 줄이 추가되고, 마지막 값이 유효 |
| `meta.jsonl` | 거래 id 카운터 |
| `app.log` | 실행 로그 (INFO/ERROR) |

레코드의 필드 구성은 다음과 같습니다. 보기 쉽게 펼쳐 적었지만,
**실제 파일에는 레코드 하나가 한 줄의 JSON**으로 저장됩니다.

`transactions.jsonl` — 거래 한 건:

```json
{
  "id": "TX-000001",
  "type": "expense",
  "date": "2026-10-15",
  "amount": 15000,
  "category": "식비",
  "memo": "점심",
  "tags": ["meal"]
}
```

`categories.jsonl` — 카테고리 하나:

```json
{"name": "식비"}
```

`budgets.jsonl` — 월 예산 하나:

```json
{"year_month": "2026-10", "amount": 500000}
```

`meta.jsonl` — id 카운터:

```json
{"last_tx_no": 1}
```

수정·삭제는 임시 파일에 쓴 뒤 원자적으로 교체(`os.replace`)하므로, 작업 중
프로그램이 중단되어도 원본 파일은 깨지지 않습니다.

## 명령 요약

| 명령 | 역할 | 입력 방식 |
|---|---|---|
| `add` | 거래 추가 | 대화형 |
| `list` | 최신순 목록 (`--limit`, 기본 10) | 인자 |
| `search` | 조건 검색 — 기간·카테고리·타입·메모·태그 (AND 결합) | 인자 |
| `summary` | 월별 요약 (`--month` 필수, `--top` 기본 3) | 인자 |
| `budget set / list` | 월 예산 설정·조회 | 인자 |
| `category add / list / remove` | 카테고리 관리 | add·remove는 대화형 |
| `update` | 거래 수정 (**옵션 방식으로 고정** — 아래 참조) | 인자 |
| `delete` | 거래 삭제 (`--id`) | 인자 |
| `export` / `import` | CSV 내보내기/가져오기 | 인자 |

## 명령별 예시

### add — 거래 추가 (대화형)

날짜·타입·카테고리·금액·메모·태그를 차례로 묻습니다. 잘못된 입력은 그 필드만
다시 묻고, 미등록 카테고리를 입력하면 새로 등록할지(y/n) 확인합니다.
입력 도중 `Ctrl+C`를 누르면 아무것도 저장하지 않고 취소됩니다.

```
$ python3 -m budget_app add
날짜(YYYY-MM-DD): 2026-10-15
타입(income/expense): expense
[안내] 등록된 카테고리: 식비, 카페, 유흥, 쇼핑, 교통, 주거, 통신, 의료, 여가, 생활, 급여, 용돈, 기타
카테고리: 식비
금액(양수): 15000
메모(선택): 점심
태그(쉼표로 구분, 없으면 엔터): meal
[저장 완료] id=TX-000001
```

### list — 최신순 목록

거래 날짜 기준 최신순이며, 같은 날짜는 나중에 입력한 것이 위에 옵니다.

```
$ python3 -m budget_app list --limit 3
TX-000001 | 2026-10-15 | expense | 식비 |    15,000 | 점심
TX-000002 | 2026-10-14 | income  | 급여 | 3,000,000 | 월급
TX-000003 | 2026-10-12 | expense | 교통 |    20,000 | 택시
```

### search — 조건 검색

지원 조건: `--from` / `--to` (기간), `--category`, `--type`, `--q` (메모 키워드),
`--tag`, 그리고 `--limit` (건수 제한, 기본은 전부).
조건을 여러 개 주면 **모두 만족(AND)** 하는 거래만 나옵니다.
`--q`와 `--tag`는 부분 일치이며 대소문자를 구분하지 않습니다.

```
$ python3 -m budget_app search --type expense --q 점심
TX-000001 | 2026-10-15 | expense | 식비 | 15,000 | 점심
```

### summary · budget — 월별 요약과 예산

```
$ python3 -m budget_app budget set --month 2026-10 --amount 500000
[저장 완료] 2026-10 예산 500,000원

$ python3 -m budget_app summary --month 2026-10
총 수입: 3,050,000원
총 지출: 50,350원
잔액: 2,999,650원
예산: 500,000원 (사용률 10.1%)

지출 TOP 3
1) 식비 24,000원
2) 교통 21,550원
3) 카페 4,800원
```

예산을 초과하면 `[경고] 예산을 N원 초과했습니다!`가 함께 표시됩니다.
지출 TOP은 동점이면 함께 표시되어 줄 수가 N을 넘을 수 있습니다(공동 순위).
데이터가 없는 달은 `[안내] YYYY-MM에는 데이터가 없습니다.`를 출력합니다.

### update — 거래 수정 (옵션 방식으로 고정)

이 프로그램의 `update`는 **옵션 기반 한 가지 방식**입니다. 규칙:

- **지정한 옵션만** 변경됩니다 — 생략한 필드는 기존 값 유지
- `--memo ""` / `--tags ""` 처럼 **빈 문자열을 주면 그 필드를 비웁니다**
- `--tags`는 전체 교체입니다 (기존 태그에 추가가 아님)

```
$ python3 -m budget_app update --id TX-000001 --amount 18000
[수정 완료] TX-000001 | 2026-10-15 | expense | 식비 | 18,000 | 점심
```

### delete — 거래 삭제

```
$ python3 -m budget_app delete --id TX-000003
[삭제 완료] TX-000003 | 2026-10-12 | expense | 교통 | 20,000 | 택시
```

존재하지 않는 id는 `[오류] 존재하지 않는 거래 id입니다: ...`로 처리됩니다
(update도 동일). 삭제된 거래의 id 번호는 재사용되지 않습니다.

### category — 카테고리 관리

```
$ python3 -m budget_app category list
- 식비
- 카페
- 유흥
...
```

`category add`(대화형)로 등록, `category remove`(대화형)로 삭제합니다.
**사용 중인 카테고리는 삭제가 차단됩니다** — 해당 거래들의 카테고리를
`update --category`로 바꾼 뒤 다시 시도하세요.

### export / import — CSV 내보내기/가져오기

```
$ python3 -m budget_app export --out october.csv --month 2026-10
[완료] october.csv (6 records)

$ python3 -m budget_app import --from samples/sample-import.csv
[완료] imported=4, skipped=0
```

- export는 `--month YYYY-MM` **또는** `--from/--to YYYY-MM-DD` 중 **한 방식**의
  기간 조건이 필수입니다 (두 방식을 함께 쓰면 오류).
- export 대상 파일이 이미 있으면 덮어쓰기 전에 확인(y/n)합니다.
- import에서 검증에 실패한 행은 건너뛰고, 행 번호와 원인을 함께 보고합니다.
- CSV에 미등록 카테고리가 있으면 목록을 보여주고 일괄 등록 여부(y/n)를
  먼저 묻습니다. 등록하지 않으면 해당 행은 건너뜁니다.

## import/export CSV 스키마

| column | required | 설명 |
|:---:|:---:|:---|
| Date | Y | YYYY-MM-DD |
| Type | Y | income / expense |
| Category | Y | 등록된 카테고리 |
| Amount | Y | 양수 정수 |
| Memo | N | 문자열 |
| Tags | N | 쉼표(,) 구분 문자열 |
| 공통 | | UTF-8, 헤더 포함 |

예시 파일: [samples/sample-import.csv](samples/sample-import.csv)
(실제 지출 데이터가 아닌, 문서 예시용으로 만든 가공 데이터입니다)

주의: 스키마에 id가 없으므로 import된 거래는 **새 id를 발급**받습니다.
같은 CSV를 두 번 import하면 거래가 중복 등록됩니다.

## 오류 처리와 종료 코드

오류는 스택트레이스 대신 **원인 + 해결 힌트**로 출력됩니다:

```
$ python3 -m budget_app summary --month 2026-13
[오류] 월 형식이 올바르지 않습니다: '2026-13' (YYYY-MM)
[힌트] 예: 2026-10
```

| 종료 코드 | 의미 |
|:---:|:---|
| 0 | 정상 종료 |
| 1 | 오류 종료 |
| 130 | 대화형 입력 취소 (Ctrl+C / EOF) |

## 구조

```
budget_app/
  __main__.py   # python -m budget_app 진입점
  cli.py        # 명령 파싱·대화형 입력·출력 (사용자 접점)
  services.py   # 업무 규칙 (검색·집계·수정 절차)
  storage.py    # 파일 입출력 (스트리밍 리더, append, 원자적 교체)
  models.py     # 데이터 모델과 검증 (Transaction, CSV 스키마)
  errors.py     # 공용 예외 (원인+힌트)
  decorators.py # 공통 관심사 (실행 로그·시간 측정·예외 번역)
```
