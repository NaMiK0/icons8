"""Оркестрация прогона.

Пока собраны две стадии — чтение выгрузки и нормализация. Результат стадий
печатается сводкой: на ней видно, правильно ли прочитан чужой файл, прежде
чем к делу подключатся фильтры, кластеризация и LLM.
"""

import logging
import sys

from ..domain.models import LexicalGroup, Query
from ..ingest.loader import InputError, LoadResult, load_export
from ..pipeline import normalize
from ..settings import ConfigError
from . import options as options_module

log = logging.getLogger("seo_landings")

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_NOTHING_TO_DO = 2

TOP_ROWS = 10


def main(argv: list[str] | None = None) -> int:
    try:
        options = options_module.resolve(argv)
    except ConfigError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    _setup_logging(options.log_level)

    try:
        loaded = load_export(options.input_path)
    except InputError as error:
        print(f"Не удалось прочитать выгрузку: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    queries, duplicates = normalize.dedupe(loaded.queries)
    if not queries:
        print("В выгрузке нет ни одного запроса.", file=sys.stderr)
        return EXIT_NOTHING_TO_DO

    queries, truncated = normalize.truncate(queries, options.max_queries)
    groups = normalize.build_groups(queries)

    _print_input_summary(options, loaded, len(queries), len(duplicates), len(truncated))
    _print_queries("Топ по кликам", sorted(queries, key=lambda q: -q.clicks)[:TOP_ROWS])
    _print_queries("Топ по потенциалу роста", sorted(queries, key=lambda q: -q.potential)[:TOP_ROWS])
    _print_groups(groups)
    _print_warnings(loaded.warnings)

    return EXIT_OK


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format="%(levelname)s: %(message)s")


def _print_input_summary(
    options: options_module.Options,
    loaded: LoadResult,
    kept: int,
    duplicates: int,
    truncated: int,
) -> None:
    config_path = options.settings.path or "встроенные значения"
    columns = ", ".join(f"{name}={title}" for name, title in loaded.columns_found.items())

    print(f"\nВыгрузка: {options.input_path}")
    print(f"  конфиг:          {config_path}")
    print(f"  кодировка:       {loaded.encoding}, разделитель: {loaded.delimiter!r}")
    print(f"  колонки:         {columns or '—'}")
    if loaded.columns_missing:
        print(f"  не найдены:      {', '.join(loaded.columns_missing)}")
    print(f"  строк прочитано: {loaded.rows_read}")
    if loaded.rows_skipped:
        print(f"  строк пропущено: {loaded.rows_skipped} (пустой запрос)")
    print(f"  запросов:        {kept}" + (f" (склеено дублей: {duplicates})" if duplicates else ""))
    if truncated:
        print(f"  усечено:         {truncated} (лимит --max-queries={options.max_queries})")


def _print_queries(title: str, queries: list[Query]) -> None:
    print(f"\n{title}")
    print(f"  {'запрос':<38} {'клики':>7} {'показы':>9} {'поз.':>6} {'потенциал':>11}")
    for query in queries:
        position = f"{query.position:.1f}" if query.position is not None else "—"
        print(
            f"  {_clip(query.text, 38):<38} {query.clicks:>7} {query.impressions:>9} "
            f"{position:>6} {query.potential:>11,.0f}"
        )


def _print_groups(groups: list[LexicalGroup]) -> None:
    multi = [group for group in groups if len(group.queries) > 1]
    print(f"\nЛексических групп: {len(groups)} (из них с несколькими запросами: {len(multi)})")
    for group in groups[:TOP_ROWS]:
        variants = ", ".join(q.text for q in group.queries[:4])
        if len(group.queries) > 4:
            variants += f", … (+{len(group.queries) - 4})"
        print(f"  {_clip(group.key, 26):<26} потенциал {group.potential:>10,.0f}  ← {variants}")


def _print_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    print("\nПредупреждения:")
    for warning in warnings:
        print(f"  • {warning}")


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"
