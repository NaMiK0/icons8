"""Разбор аргументов командной строки.

Правило приоритета: флаг CLI > config.toml > встроенный дефолт. Поэтому
значения по умолчанию у аргументов — None: только так видно, задал ли
пользователь флаг явно.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings, load_settings


@dataclass(slots=True)
class Options:
    """Итоговые настройки прогона: флаги, слитые с конфигом."""

    input_path: Path
    out_dir: Path
    settings: Settings
    max_queries: int
    target_pages: int
    tolerance: int
    keep_brand: bool
    keep_third_party: bool
    log_level: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Собрать связанные лендинги из выгрузки Google Search Console.",
    )
    parser.add_argument("--input", required=True, metavar="PATH",
                        help="выгрузка .csv или .zip из Search Console")
    parser.add_argument("--out", metavar="DIR", default="out",
                        help="каталог результата (по умолчанию: out)")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="путь к config.toml (по умолчанию: config/config.toml)")
    parser.add_argument("--max-queries", type=int, default=None, metavar="INT",
                        help="сколько запросов брать из выгрузки после склейки дублей")
    parser.add_argument("--target-pages", type=int, default=None, metavar="INT",
                        help="сколько лендингов собрать (по умолчанию: 10)")
    parser.add_argument("--tolerance", type=int, default=None, metavar="INT",
                        help="допустимое отклонение от целевого числа страниц")
    parser.add_argument("--keep-brand", action="store_true",
                        help="не отбрасывать запросы своего бренда")
    parser.add_argument("--keep-third-party", action="store_true",
                        help="не отбрасывать чужие бренды и связанные с ними символы")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="подробность вывода (по умолчанию: INFO)")
    return parser


def resolve(argv: list[str] | None = None) -> Options:
    """Разобрать аргументы и слить их с конфигом."""
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)

    return Options(
        input_path=Path(args.input),
        out_dir=Path(args.out),
        settings=settings,
        max_queries=_first(args.max_queries, settings.get("input.max_queries"), 500),
        target_pages=_first(args.target_pages, settings.get("pages.target"), 10),
        tolerance=_first(args.tolerance, settings.get("pages.tolerance"), 2),
        keep_brand=args.keep_brand,
        keep_third_party=args.keep_third_party,
        log_level=args.log_level,
    )


def _first(*values):
    """Первое заданное значение: флаг, затем конфиг, затем дефолт."""
    for value in values:
        if value is not None:
            return value
    return None
