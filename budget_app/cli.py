"""CLI 계층 — 사용자와 시스템 사이의 인터페이스(대화)를 아는 유일한 곳.

명령 문법(argparse)·프롬프트·출력 형식·종료 코드를 안다.
오류의 판정 기준은 모른다 — 모델·저장소가 판정한 결과(cause/hint)를
사람의 언어로 바꿔 전달하고, 필요하면 다시 묻는 것까지가 CLI의 일이다.
스키마(models)·절차(services)·파일(storage)은 모른다.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from budget_app import services
from budget_app.decorators import setup_logging
from budget_app.errors import BudgetError
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
    parser.add_subparsers(dest='command', metavar='<command>')
    return parser


def print_init_report(report: services.InitReport) -> None:
    if report.created_files:
        print('[안내] 저장 파일이 없어 새로 만들었습니다: '
              + ', '.join(report.created_files))
    if report.seeded_categories:
        print(f'[안내] 기본 카테고리 {len(report.seeded_categories)}개를 생성했습니다: '
              + ', '.join(report.seeded_categories))
        print('[힌트] category add/remove로 언제든 수정할 수 있습니다.')


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
        if args.command is None:
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
