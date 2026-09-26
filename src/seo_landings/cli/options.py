"""Разбор аргументов командной строки.

Правило приоритета: флаг CLI > config.toml > встроенный дефолт. Поэтому
значения по умолчанию у аргументов — None: только так видно, задал ли
пользователь флаг явно.

Переключатели парные (--llm / --no-llm): постоянное значение живёт в
config.toml с комментарием, что оно включает, а флаг меняет его на один
запуск в любую сторону.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings, load_settings

SWITCH = argparse.BooleanOptionalAction


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
    use_llm: bool
    use_cache: bool
    show_metrics: bool
    log_level: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Собрать связанные лендинги из выгрузки Google Search Console.",
        epilog="Постоянные значения переключателей — в config/config.toml, разделы [run] и "
               "[render]; флаги меняют их на один запуск.",
    )
    parser.add_argument("--input", required=True, metavar="PATH",
                        help="выгрузка .csv или .zip из Search Console")
    parser.add_argument("--out", metavar="DIR", default="out",
                        help="каталог результата (по умолчанию: out)")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="путь к config.toml (по умолчанию: config/config.toml)")

    volume = parser.add_argument_group("объём работы")
    volume.add_argument("--max-queries", type=int, default=None, metavar="N",
                        help="сколько запросов брать из выгрузки после склейки дублей "
                             "([input] max_queries, по умолчанию 500)")
    volume.add_argument("--target-pages", type=int, default=None, metavar="N",
                        help="сколько лендингов собрать ([pages] target, по умолчанию 10)")
    volume.add_argument("--tolerance", type=int, default=None, metavar="N",
                        help="допустимое отклонение от числа страниц "
                             "([pages] tolerance, по умолчанию 2)")

    switches = parser.add_argument_group("переключатели (значение по умолчанию — из config.toml)")
    switches.add_argument("--llm", action=SWITCH, default=None,
                          help="обращаться к модели; --no-llm — правила и шаблоны без ключа "
                               "([run] use_llm)")
    switches.add_argument("--cache", action=SWITCH, default=None,
                          help="брать ответы модели из кэша; --no-cache — спросить заново "
                               "([run] use_cache)")
    switches.add_argument("--keep-brand", action=SWITCH, default=None,
                          help="оставить запросы своего бренда ([run] keep_brand)")
    switches.add_argument("--keep-third-party", action=SWITCH, default=None,
                          help="оставить чужие торговые марки ([run] keep_third_party)")
    switches.add_argument("--metrics", action=SWITCH, default=None,
                          help="показывать на страницах клики, показы и позиции; "
                               "--no-metrics — для публикации ([render] show_search_metrics)")

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
        use_llm=_first(args.llm, settings.get("run.use_llm"), True),
        use_cache=_first(args.cache, settings.get("run.use_cache"), True),
        keep_brand=_first(args.keep_brand, settings.get("run.keep_brand"), False),
        keep_third_party=_first(args.keep_third_party, settings.get("run.keep_third_party"), False),
        show_metrics=_first(args.metrics, settings.get("render.show_search_metrics"), True),
        log_level=args.log_level,
    )


def _first(*values):
    """Первое заданное значение: флаг, затем конфиг, затем дефолт."""
    for value in values:
        if value is not None:
            return value
    return None
