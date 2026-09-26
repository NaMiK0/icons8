"""Машинный отчёт о прогоне.

report.json — единственный источник цифр для README. Пункт задания «какие
запросы отбросили и почему» должен пересказываться из файла, а не из
памяти: так он останется верным и на той выгрузке, которую мы не видели.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.models import Decision, Query
from ..domain.reasons import LABELS, Reason


@dataclass(slots=True)
class RunReport:
    """Накопитель фактов о прогоне. Стадии дописывают свои разделы."""

    input: dict[str, Any] = field(default_factory=dict)
    mode: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    llm_usage: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        """Любая деградация обязана быть видимой (§16 SPEC)."""
        if message not in self.warnings:
            self.warnings.append(message)

    def add_filter_stats(self, decisions: list[Decision]) -> None:
        """Сводка по всем запросам выгрузки: что на страницах, что исключено.

        Клики и потенциал склеенных дублей в итоги не входят: они уже
        прибавлены к запросу, с которым склеены, и посчитались бы дважды.
        """
        kept = [d for d in decisions if d.keep]
        dropped = [d for d in decisions if not d.keep]
        countable = [d for d in dropped if d.reason is not Reason.DUPLICATE]
        by_reason = Counter(d.reason.value for d in dropped)

        self.filters = {
            "kept": len(kept),
            "dropped": len(dropped),
            "kept_clicks": sum(d.query.clicks for d in kept),
            "dropped_clicks": sum(d.query.clicks for d in countable),
            "kept_potential": round(sum(d.query.potential for d in kept)),
            "dropped_potential": round(sum(d.query.potential for d in countable)),
            "by_reason": {
                reason: {
                    "queries": count,
                    "clicks": sum(
                        d.query.clicks for d in dropped if d.reason.value == reason
                    ),
                    "potential": round(
                        sum(d.query.potential for d in dropped if d.reason.value == reason)
                    ),
                    "label": LABELS.get(reason, ""),
                    "examples": [
                        d.query.text for d in _top(dropped, reason)
                    ],
                }
                for reason, count in by_reason.most_common()
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "input": self.input,
            "mode": self.mode,
            "filters": self.filters,
            "clusters": self.clusters,
            "llm_usage": self.llm_usage,
            "warnings": self.warnings,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _top(dropped: list[Decision], reason: str, limit: int = 3) -> list[Decision]:
    """Самые заметные примеры причины — чтобы отчёт читался без исходника."""
    same = [d for d in dropped if d.reason.value == reason]
    same.sort(key=lambda d: (-d.query.clicks, -d.query.impressions))
    return same[:limit]


def describe_queries(queries: list[Query]) -> dict[str, Any]:
    """Сводные цифры по набору запросов."""
    return {
        "queries": len(queries),
        "clicks": sum(q.clicks for q in queries),
        "impressions": sum(q.impressions for q in queries),
        "potential": round(sum(q.potential for q in queries)),
    }
