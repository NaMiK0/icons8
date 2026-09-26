"""Оркестрация прогона: от выгрузки до страниц и отчёта.

Порядок стадий: чтение → склейка дублей и усечение → правила → модель
решает, что отбросить → группы → модель собирает кластеры → модель пишет
тексты → HTML → отчёт. На каждой стадии с моделью есть запасной путь без
неё, и каждый такой переход виден в предупреждениях отчёта.
"""

import logging
import sys

from ..domain.models import Cluster, Decision, LexicalGroup, Query
from ..env import load_dotenv
from ..ingest.loader import InputError, LoadResult, load_export
from ..llm.cache import ResponseCache
from ..llm.client import LLMClient, LLMConfig
from ..pipeline import classify as classify_module
from ..pipeline import cluster as cluster_module
from ..pipeline import content as content_module
from ..pipeline import accounting, filters, llm_cluster, llm_content, normalize
from ..render.renderer import Renderer, SiteMeta
from ..reporting.excluded import write_excluded
from ..reporting.report import RunReport, describe_queries
from ..settings import ConfigError, model_chain
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
    load_dotenv()
    report = RunReport()

    try:
        loaded = load_export(options.input_path)
    except InputError as error:
        print(f"Не удалось прочитать выгрузку: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    for warning in loaded.warnings:
        report.warn(warning)

    queries, duplicates = normalize.dedupe(loaded.queries)
    if not queries:
        print("В выгрузке нет ни одного запроса.", file=sys.stderr)
        return EXIT_NOTHING_TO_DO

    queries, truncated = normalize.truncate(queries, options.max_queries)
    if truncated:
        report.warn(
            f"обработан топ-{options.max_queries} запросов по потенциалу, "
            f"остальные {len(truncated)} не рассматривались"
        )

    client = LLMClient(
        config=LLMConfig.from_env(options.settings),
        cache=ResponseCache(enabled=options.use_cache),
    )
    use_llm = options.use_llm
    if use_llm and not client.available:
        report.warn(
            "LLM_API_KEY не задан — модель не вызывается: используются ответы из кэша, "
            "а где их нет — правила и шаблоны"
        )

    decisions: list[Decision] = []
    if use_llm:
        # С моделью про чужие бренды решает она, а не список слов из конфига.
        rules_only = filters.FilterPolicy.from_settings(
            options.settings,
            keep_brand=options.keep_brand,
            keep_third_party=options.keep_third_party,
            use_offline_markers=False,
        )
        classified = classify_module.classify(
            filters.apply(queries, rules_only), client, options.settings
        )
        for warning in classified.warnings:
            report.warn(warning)
        use_llm = classified.used_llm
        if use_llm:
            decisions = classified.decisions

    if not use_llm:
        # Модели нет или она не ответила — грубые маркеры обязательны, иначе
        # «instagram logo» проскочит на страницы.
        offline = filters.FilterPolicy.from_settings(
            options.settings,
            keep_brand=options.keep_brand,
            keep_third_party=options.keep_third_party,
            use_offline_markers=True,
        )
        decisions = filters.apply(queries, offline)
    kept = [decision.query for decision in decisions if decision.keep]
    if not kept:
        print("После фильтров не осталось ни одного запроса.", file=sys.stderr)
        return EXIT_NOTHING_TO_DO

    groups = normalize.build_groups(kept)

    cluster_policy = cluster_module.ClusterPolicy.from_settings(
        options.settings, target=options.target_pages, tolerance=options.tolerance
    )
    cluster_source = "lexical"
    clusters: list[Cluster] = []
    uncovered: list[LexicalGroup] = []
    if use_llm:
        grouped = llm_cluster.build_clusters(groups, client, options.settings, cluster_policy)
        for warning in grouped.warnings:
            report.warn(warning)
        if grouped.used_llm:
            clusters, uncovered = grouped.clusters, grouped.uncovered
            cluster_source = "llm"

    if not clusters:
        clusters, uncovered = cluster_module.build_clusters(groups, cluster_policy)
    if not clusters:
        print("Не удалось собрать ни одного кластера.", file=sys.stderr)
        return EXIT_NOTHING_TO_DO
    if uncovered:
        report.warn(
            f"{len(uncovered)} лексических групп не вошли в набор из "
            f"{len(clusters)} страниц"
        )

    site_name = options.settings.get("site.name", "Icons8")
    content_source = _write_content(clusters, client, options, report, use_llm, site_name)

    decisions = accounting.finalize(
        decisions,
        duplicates=duplicates,
        truncated=truncated,
        uncovered=uncovered,
        limit=options.max_queries,
        pages=len(clusters),
    )
    lost = accounting.unaccounted(len(loaded.queries), decisions, clusters)
    if lost:
        report.warn(f"{lost} запросов не учтены ни на страницах, ни в исключениях — ошибка учёта")
    for note in client.fallbacks:
        report.warn(note)

    report.input = {
        "path": str(options.input_path),
        "encoding": loaded.encoding,
        "delimiter": loaded.delimiter,
        "columns_found": loaded.columns_found,
        "columns_missing": loaded.columns_missing,
        "rows_read": loaded.rows_read,
        "rows_skipped": loaded.rows_skipped,
        "duplicates_merged": len(duplicates),
        "truncated": len(truncated),
        **describe_queries(queries),
    }
    report.mode = {
        "llm": use_llm,
        "clustering": cluster_source,
        "content": content_source,
        "models": {
            stage: model_chain(options.settings, stage)
            for stage in ("classify", "cluster", "content")
        } if use_llm else None,
        "keep_brand": options.keep_brand,
        "keep_third_party": options.keep_third_party,
        "show_metrics": options.show_metrics,
        "config": str(options.settings.path) if options.settings.path else None,
    }
    if not use_llm:
        report.warn("классификация выполнена правилами и офлайн-маркерами, без модели")
    report.add_filter_stats(decisions)
    report.llm_usage = [usage.as_dict() for usage in client.usage.values()]
    report.clusters = [_describe_cluster(item) for item in clusters]

    excluded_path = options.out_dir / "excluded.csv"
    report_path = options.out_dir / "report.json"
    dropped_count = write_excluded(decisions, excluded_path)

    meta = SiteMeta(
        site_name=site_name,
        base_url=options.settings.get("site.base_url", "https://example.com/"),
        source_file=options.input_path.name,
        content_source=content_source,
        cluster_source=cluster_source,
        language=options.settings.get("market.language", "en"),
        show_metrics=options.show_metrics,
    )
    written = Renderer(meta).render_site(
        clusters, options.out_dir, _excluded_rows(report)
    )
    report.write(report_path)

    _print_input_summary(options, loaded, len(queries), len(duplicates), len(truncated))
    _print_filter_summary(report, decisions, len(loaded.queries))
    _print_clusters(clusters)
    _print_groups(groups)
    _print_llm_usage(report, client)
    _print_warnings(report.warnings)
    print(f"\nСтраниц собрано: {len(written) - 1} + хаб → {options.out_dir}/index.html")
    print(f"Отчёт: {report_path}")
    print(f"Исключено ({dropped_count}): {excluded_path}")

    return EXIT_OK


def _write_content(
    clusters: list[Cluster],
    client: LLMClient,
    options: options_module.Options,
    report: RunReport,
    use_llm: bool,
    site_name: str,
) -> str:
    """Наполнить страницы текстом: моделью, а при неудаче — шаблоном."""
    written_by_llm = 0
    for item in clusters:
        if use_llm:
            result = llm_content.build_content(item, clusters, client, options.settings)
            for warning in result.warnings:
                report.warn(warning)
            item.content = result.content
            written_by_llm += int(result.used_llm)
        else:
            item.content = content_module.build_template_content(item, site_name)

    if not use_llm:
        return "template"
    if written_by_llm == len(clusters):
        return "llm"
    report.warn(
        f"текст {len(clusters) - written_by_llm} страниц из {len(clusters)} взят из шаблона"
    )
    return "mixed"


def _describe_cluster(cluster: Cluster) -> dict:
    content = cluster.content
    return {
        "slug": cluster.slug,
        "primary_keyword": cluster.primary_keyword,
        "intent": cluster.intent,
        "file": f"{cluster.slug}.html",
        "source": cluster.source,
        "content_source": content.source if content else None,
        "title": content.title if content else None,
        "queries": [query.text for query in cluster.queries],
        "clicks": cluster.clicks,
        "impressions": cluster.impressions,
        "avg_position": round(cluster.avg_position, 2) if cluster.avg_position else None,
        "potential": round(cluster.potential),
    }


def _excluded_rows(report: RunReport) -> list[dict]:
    """Сводка исключений для хаб-страницы."""
    return [
        {
            "label": data["label"] or reason,
            "queries": data["queries"],
            "clicks": f"{data['clicks']:,}".replace(",", " "),
            "examples": ", ".join(data["examples"]),
        }
        for reason, data in report.filters.get("by_reason", {}).items()
    ]


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


def _print_filter_summary(report: RunReport, decisions: list[Decision], total: int) -> None:
    stats = report.filters
    print(
        f"\nИз {total} строк выгрузки: на страницах {stats['kept']}, исключено {stats['dropped']}"
        f" (кликов исключено {stats['dropped_clicks']:,}, "
        f"потенциала {stats['dropped_potential']:,})".replace(",", " ")
    )
    for reason, data in stats["by_reason"].items():
        examples = ", ".join(data["examples"])
        print(
            f"  {reason:<22} {data['queries']:>3} запр.  "
            f"потенциал {data['potential']:>9,}".replace(",", " ")
            + f"  ← {examples}"
        )


def _print_clusters(clusters: list[Cluster]) -> None:
    print(f"\nЛендингов: {len(clusters)}")
    print(f"  {'страница':<28} {'запр.':>6} {'клики':>8} {'показы':>10} {'поз.':>6} {'потенциал':>11}")
    for cluster in clusters:
        position = f"{cluster.avg_position:.1f}" if cluster.avg_position else "—"
        print(
            f"  {_clip(cluster.slug + '.html', 28):<28} {len(cluster.queries):>6} "
            f"{cluster.clicks:>8} {cluster.impressions:>10} {position:>6} "
            f"{cluster.potential:>11,.0f}"
        )


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


def _print_llm_usage(report: RunReport, client: LLMClient) -> None:
    if not report.llm_usage:
        return
    print("\nРасход модели:")
    for usage in report.llm_usage:
        print(
            f"  {usage['stage']:<9} {usage['model']:<25} вызовов {usage['calls']}"
            f" (из кэша {usage['cache_hits']}), токенов вход {usage['input_tokens']},"
            f" выход {usage['output_tokens']}"
            + (f" (рассуждения {usage['reasoning_tokens']})" if usage["reasoning_tokens"] else "")
            + f", {usage['seconds']} c"
        )


def _print_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    print("\nПредупреждения:")
    for warning in warnings:
        print(f"  • {warning}")


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"
