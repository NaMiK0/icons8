"""Выгрузка отброшенных запросов в CSV.

Отдельный файл, а не раздел отчёта, потому что его открывают в таблице и
сортируют: «покажи всё, что выкинули, по убыванию кликов» — обычный вопрос
на обсуждении результата.
"""

import csv
from pathlib import Path

from ..domain.models import Decision
from ..domain.reasons import LABELS

FIELDNAMES = (
    "query",
    "clicks",
    "impressions",
    "ctr",
    "position",
    "reason",
    "reason_label",
    "note",
    "decided_by",
)


def write_excluded(decisions: list[Decision], path: Path) -> int:
    """Записать отброшенные запросы, самые дорогие — сверху."""
    dropped = [decision for decision in decisions if not decision.keep]
    dropped.sort(key=lambda d: (-d.query.clicks, -d.query.impressions, d.query.text))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for decision in dropped:
            query = decision.query
            writer.writerow(
                {
                    "query": query.text,
                    "clicks": query.clicks,
                    "impressions": query.impressions,
                    "ctr": round(query.ctr, 4),
                    "position": "" if query.position is None else round(query.position, 1),
                    "reason": decision.reason.value,
                    "reason_label": LABELS.get(decision.reason, ""),
                    "note": decision.note,
                    "decided_by": decision.source,
                }
            )
    return len(dropped)
