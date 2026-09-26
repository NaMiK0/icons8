"""Учёт каждого запроса выгрузки.

Каждая строка выгрузки обязана оказаться в одном из двух мест: на странице
или в excluded.csv с причиной. Без этого «какие запросы отброшены и почему»
отвечает только про те, что отсеяли фильтры, а усечённые лимитом, склеенные
как дубли и не попавшие в набор страниц пропадают молча. На выгрузке из
3000 строк так терялось 2811 запросов из 3000.
"""

from ..domain.models import Cluster, Decision, LexicalGroup, MergedDuplicate, Query
from ..domain.reasons import Reason


def finalize(
    decisions: list[Decision],
    *,
    duplicates: list[MergedDuplicate],
    truncated: list[Query],
    uncovered: list[LexicalGroup],
    limit: int,
    pages: int,
) -> list[Decision]:
    """Дописать решения по запросам, отсеянным вне стадии фильтров."""
    left_out = {query.text for group in uncovered for query in group.queries}

    result: list[Decision] = []
    for decision in decisions:
        if decision.keep and decision.query.text in left_out:
            result.append(
                Decision(
                    query=decision.query,
                    keep=False,
                    reason=Reason.UNCLUSTERED,
                    note=f"прошёл фильтры, но не вошёл в набор из {pages} страниц",
                    source="rule",
                    intent=decision.intent,
                )
            )
        else:
            result.append(decision)

    result += [
        Decision(
            query=query,
            keep=False,
            reason=Reason.TRUNCATED,
            note=f"за пределами топ-{limit} запросов по потенциалу роста",
            source="rule",
        )
        for query in truncated
    ]
    result += [
        Decision(
            query=duplicate.absorbed,
            keep=False,
            reason=Reason.DUPLICATE,
            note=f"«{duplicate.absorbed.raw}» склеен с «{duplicate.kept}», метрики сложены",
            source="rule",
        )
        for duplicate in duplicates
    ]
    return result


def unaccounted(total_rows: int, decisions: list[Decision], clusters: list[Cluster]) -> int:
    """Сколько строк выгрузки не оказалось ни на странице, ни в исключениях.

    Должно быть ноль. Не ноль — ошибка в коде, и она попадёт в отчёт, а не
    потеряется.
    """
    on_pages = sum(len(cluster.queries) for cluster in clusters)
    excluded = sum(1 for decision in decisions if not decision.keep)
    return total_rows - on_pages - excluded
