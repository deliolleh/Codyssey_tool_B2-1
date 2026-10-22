"""CLI 계층 — 사용자와 시스템 사이의 인터페이스(대화)를 아는 유일한 곳.

명령 문법(argparse)·프롬프트·출력 형식·종료 코드를 안다.
오류의 판정 기준은 모른다 — 모델·저장소가 판정한 결과(cause/hint)를
사람의 언어로 바꿔 전달하고, 필요하면 다시 묻는 것까지가 CLI의 일이다.
스키마(models)·절차(services)·파일(storage)은 모른다.
"""
from __future__ import annotations

import argparse
import logging
import unicodedata
from pathlib import Path
from typing import Callable

try:
    # import 자체가 input()의 라인 편집기를 교체한다 — 한글 백스페이스/방향키/히스토리.
    import readline  # noqa: F401
except ImportError:
    pass  # readline이 없는 플랫폼(Windows 기본 등)에선 기본 입력 유지

from budget_app import models, services
from budget_app.decorators import setup_logging
from budget_app.errors import BudgetError, ValidationError
from budget_app.storage import DEFAULT_DATA_DIR, Storage

LOG_FILENAME = 'app.log'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='budget_app',
        description='나만의 용돈 기입장 — 파일 기반 가계부 콘솔 프로그램',
    )
    parser.add_argument(
        '--data-dir',
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f'저장 폴더 (기본: ./{DEFAULT_DATA_DIR})',
    )
    # 명령(add, list, …)은 기능 루프마다 여기에 추가된다.
    subparsers = parser.add_subparsers(dest='command', metavar='<command>')
    subparsers.add_parser('add', help='거래를 대화형으로 추가')
    list_parser = subparsers.add_parser('list', help='최신순 거래 목록')
    list_parser.add_argument('--limit', type=int, default=10, help='표시 건수 (기본: 10)')
    search_parser = subparsers.add_parser(
        'search',
        help='조건 검색 (조건 여러 개 = AND)',
        description='조건에 맞는 거래를 최신순으로 출력합니다. '
                    '조건을 여러 개 주면 모두 만족(AND)하는 거래만 출력됩니다.',
    )
    search_parser.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD', help='이 날짜부터')
    search_parser.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD', help='이 날짜까지')
    search_parser.add_argument('--type', dest='tx_type', help='income 또는 expense')
    search_parser.add_argument('--category', help='카테고리 (완전 일치)')
    search_parser.add_argument('--q', dest='query', help='메모 키워드 (부분 일치, 대소문자 무시)')
    search_parser.add_argument('--tag', help='태그 (부분 일치, 대소문자 무시)')
    search_parser.add_argument('--limit', type=int, help='표시 건수 (기본: 전부)')
    summary_parser = subparsers.add_parser('summary', help='월별 요약 (총수입/총지출/잔액 + 지출 TOP)')
    summary_parser.add_argument('--month', dest='year_month', metavar='YYYY-MM',
                                required=True, help='요약할 연-월 (예: 2026-10)')
    summary_parser.add_argument('--top', type=int, default=3,
                                help='지출 상위 카테고리 수 (기본: 3, 동점은 함께 표시)')
    budget_parser = subparsers.add_parser('budget', help='월 예산 설정/조회')
    budget_sub = budget_parser.add_subparsers(dest='budget_command', metavar='<set|list>')
    budget_set = budget_sub.add_parser('set', help='월 예산 설정 (재설정하면 새 값이 유효)')
    budget_set.add_argument('--month', dest='year_month', metavar='YYYY-MM',
                            required=True, help='예산을 걸 연-월 (예: 2026-10)')
    budget_set.add_argument('--amount', type=int, required=True, help='예산 금액 (양수)')
    budget_sub.add_parser('list', help='설정된 월 예산의 현재 값 목록')
    category_parser = subparsers.add_parser('category', help='카테고리 추가/목록/삭제')
    category_sub = category_parser.add_subparsers(dest='category_command', metavar='<add|list|remove>')
    category_sub.add_parser('add', help='카테고리 등록 (대화형)')
    category_sub.add_parser('list', help='카테고리 목록')
    category_sub.add_parser('remove', help='카테고리 삭제 — 사용 중이면 차단 (대화형)')
    return parser


def display_width(text: str) -> int:
    """터미널 표시 폭. 한글 등 동아시아 전각 문자는 2칸을 차지한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """len()이 아니라 표시 폭 기준으로 왼쪽 정렬 — 한글 열이 어긋나지 않게."""
    return text + ' ' * (width - display_width(text))


def format_table(rows: list[list[str]], right_align: set[int]) -> list[str]:
    """열 맞춤 포맷터 (결정 3-3, 보너스 3). 외부 라이브러리 없이 문자열 정렬."""
    widths = [max(display_width(row[col]) for row in rows) for col in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = []
        for col, (cell, width) in enumerate(zip(row, widths)):
            if col in right_align:
                cells.append(' ' * (width - display_width(cell)) + cell)
            else:
                cells.append(pad(cell, width))
        lines.append(' | '.join(cells).rstrip())
    return lines


def ask(prompt: str, validator: Callable[[str], object] | None = None) -> str:
    """통과할 때까지 같은 필드를 다시 묻는다 (결정 2-1 A안).

    validator에는 모델의 검증 함수가 값(일급 객체)으로 전달된다 — 판정 기준은
    모델 소관, 오류 전달과 재질문은 CLI 소관.
    검증을 통과하기 전에는 return하지 않으므로, 호출한 쪽(cmd_add 등)의 실행은
    이 줄에서 대기한다 — 유효하지 않은 입력이 services 계층까지 도달할 수 없는 근거.
    탈출 경로는 세 가지: 유효한 입력(return), Ctrl+C(130), EOF(취소).
    """
    while True:
        raw = input(prompt).strip()
        if validator is None:
            return raw
        try:
            validator(raw)
        except BudgetError as err:
            print(f'[오류] {err.cause}')
            if err.hint:
                print(f'[힌트] {err.hint}')
            continue
        return raw


def ask_category(store: Storage) -> str:
    """등록된 카테고리를 받을 때까지 묻는다. 미등록이면 등록 유도 + 확인 (결정 2-3).

    묻기 전에 등록 목록을 보여준다 — 사용자가 뭘 칠 수 있는지 모른 채 묻지 않기.
    """
    print('[안내] 등록된 카테고리: ' + ', '.join(services.list_categories(store)))
    while True:
        name = input('카테고리: ').strip()
        if not name:
            print('[오류] 카테고리를 입력해주세요')
            continue
        if services.category_registered(store, name):
            return name
        print(f'[안내] 등록되지 않은 카테고리입니다: {name}')
        answer = input(f"'{name}'을(를) 새 카테고리로 등록할까요? (y/n): ").strip().lower()
        if answer == 'y':
            services.add_category(store, name)
            print(f'[저장 완료] category={name}')
            return name
        # n(및 그 외 입력)이면 재입력으로


def cmd_add(store: Storage) -> None:
    """add — 대화형 거래 추가. 필드 순서는 명세 예시를 따른다."""
    date = ask('날짜(YYYY-MM-DD): ', models.validate_date)
    tx_type = ask('타입(income/expense): ', models.validate_type)
    category = ask_category(store)
    amount = ask('금액(양수): ', models.validate_amount)
    memo = input('메모(선택): ').strip()
    tags = models.parse_tags(input('태그(쉼표로 구분, 없으면 엔터): '))
    tx = services.add_transaction(
        store, date=date, tx_type=tx_type, category=category,
        amount=amount, memo=memo, tags=tags,
    )
    print(f'[저장 완료] id={tx.id}')


def print_init_report(report: services.InitReport) -> None:
    if report.created_files:
        print('[안내] 저장 파일이 없어 새로 만들었습니다: '
              + ', '.join(report.created_files))
    if report.seeded_categories:
        print(f'[안내] 기본 카테고리 {len(report.seeded_categories)}개를 생성했습니다: '
              + ', '.join(report.seeded_categories))
        print('[힌트] category add/remove로 언제든 수정할 수 있습니다.')


def print_transactions(transactions: list, empty_message: str) -> None:
    """거래 목록을 열 맞춤으로 출력 (결정 3-3) — list와 search가 공유하는 출력기."""
    if not transactions:
        print(f'[안내] {empty_message}')
        return
    rows = [
        [tx.id, tx.date, tx.type, tx.category, f'{tx.amount:,}', tx.memo]
        for tx in transactions
    ]
    for line in format_table(rows, right_align={4}):  # 금액 열만 오른쪽 정렬
        print(line)


def cmd_list(store: Storage, limit: int) -> None:
    """list — 날짜 기준 최신순 거래 목록 (결정 3-1/3-2/3-3)."""
    print_transactions(services.list_transactions(store, limit),
                       '거래가 없습니다. add로 첫 거래를 추가해보세요.')


def cmd_search(store: Storage, args: argparse.Namespace) -> None:
    """search — 조건 검색, 모든 조건 AND (결정 4-1~4-4)."""
    results = services.search_transactions(
        store,
        date_from=args.date_from, date_to=args.date_to,
        tx_type=args.tx_type, category=args.category,
        query=args.query, tag=args.tag, limit=args.limit,
    )
    print_transactions(results, '조건에 맞는 거래가 없습니다.')


def cmd_summary(store: Storage, year_month: str, top_n: int) -> None:
    """summary — 월별 요약 출력 (결정 5-1/5-2). 예산 표시는 섹션 6에서 추가."""
    summary = services.summarize(store, year_month)
    ranked = services.top_expense_categories(summary.expense_by_category, top_n)
    if summary.transaction_count == 0:
        print(f'[안내] {summary.year_month}에는 데이터가 없습니다.')
        return
    print(f'총 수입: {summary.income_total:,}원')
    print(f'총 지출: {summary.expense_total:,}원')
    print(f'잔액: {summary.balance:,}원')
    if summary.budget_amount is not None:
        print(f'예산: {summary.budget_amount:,}원 (사용률 {summary.budget_usage_percent:.1f}%)')
        if summary.budget_exceeded_by > 0:
            print(f'[경고] 예산을 {summary.budget_exceeded_by:,}원 초과했습니다!')
    print()
    print(f'지출 TOP {top_n}')
    if not ranked:
        print('(지출 내역 없음)')
        return
    for rank, name, amount in ranked:
        print(f'{rank}) {name} {amount:,}원')


def cmd_budget(store: Storage, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """budget set/list — 예산 설정과 현재 값 조회 (결정 6-1/6-2)."""
    if args.budget_command == 'set':
        amount = services.set_budget(store, args.year_month, args.amount)
        print(f'[저장 완료] {args.year_month} 예산 {amount:,}원')
    elif args.budget_command == 'list':
        budgets = services.current_budgets(store)
        if not budgets:
            print('[안내] 설정된 예산이 없습니다. budget set으로 설정해보세요.')
            return
        rows = [[ym, f'{amount:,}'] for ym, amount in sorted(budgets.items())]
        for line in format_table(rows, right_align={1}):
            print(line)
    else:
        parser.parse_args(['budget', '--help'])  # 하위 명령 없으면 budget 도움말 출력 후 종료


def _non_empty(value: str) -> str:
    if not value:
        raise ValidationError('값이 비어 있습니다', hint='한 단어 이상 입력해주세요')
    return value


def cmd_category(store: Storage, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """category add/list/remove — 등록 목록 관리 (결정 7-1/7-2/7-3)."""
    if args.category_command == 'add':
        name = ask('카테고리명: ', _non_empty)
        services.add_category(store, name)
        print(f'[저장 완료] category={name}')
    elif args.category_command == 'list':
        for name in services.list_categories(store):
            print(f'- {name}')
    elif args.category_command == 'remove':
        print('[안내] 등록된 카테고리: ' + ', '.join(services.list_categories(store)))
        name = ask('삭제할 카테고리명: ', _non_empty)
        services.remove_category(store, name)
        print(f'[삭제 완료] category={name}')
    else:
        parser.parse_args(['category', '--help'])


def main(argv: list[str] | None = None) -> int:
    """진입점이자 예외의 단일 관문(결정 1-2).

    예외가 사용자 문장으로 변하는 곳은 여기뿐이다.
    정상 0 / 오류 1 / 입력 취소 130(결정 1-3)을 돌려준다.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = args.data_dir / LOG_FILENAME
    try:
        setup_logging(log_path)
    except OSError as exc:
        print(f'[오류] 로그 파일을 준비할 수 없습니다: {exc}')
        print('[힌트] --data-dir 경로가 올바른지, 쓰기 권한이 있는지 확인하세요')
        return 1
    try:
        store = Storage(args.data_dir)
        print_init_report(services.initialize(store))
        if args.command == 'add':
            cmd_add(store)
        elif args.command == 'list':
            cmd_list(store, args.limit)
        elif args.command == 'search':
            cmd_search(store, args)
        elif args.command == 'summary':
            cmd_summary(store, args.year_month, args.top)
        elif args.command == 'budget':
            cmd_budget(store, args, parser)
        elif args.command == 'category':
            cmd_category(store, args, parser)
        elif args.command is None:
            parser.print_help()
        return 0
    except BudgetError as err:
        print(f'[오류] {err.cause}')
        if err.hint:
            print(f'[힌트] {err.hint}')
        return 1
    except (KeyboardInterrupt, EOFError):
        print()  # ^C가 찍힌 줄을 끊고 새 줄에서 안내
        print('[안내] 입력을 취소했습니다. 저장된 것은 없습니다.')
        return 130
    except Exception:
        # 버그도 화면에는 원인+힌트 형식으로, 전문(스택트레이스)은 일지에만 (결정 1-2 안전망)
        logging.getLogger('budget_app').exception('예상치 못한 오류')
        print('[오류] 예상치 못한 문제가 발생했습니다')
        print(f'[힌트] {log_path}에 상세 기록을 남겼습니다')
        return 1
