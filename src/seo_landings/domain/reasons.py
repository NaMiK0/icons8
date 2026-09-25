"""Закрытый перечень причин, по которым запрос не доходит до лендинга.

Перечень закрытый намеренно: это одновременно значения колонки `reason`
в out/excluded.csv и ключи сводки в out/report.json, из которой потом
пересказывается пункт README «какие запросы отбросили и почему».
"""

from enum import StrEnum


class Reason(StrEnum):
    OK = "ok"
    OWN_BRAND = "own_brand"
    THIRD_PARTY_BRAND = "third_party_brand"
    NON_ENGLISH = "non_english"
    NOT_A_LANDING_INTENT = "not_a_landing_intent"
    IRRELEVANT = "irrelevant"
    DUPLICATE = "duplicate"
    LOW_VOLUME = "low_volume"
    TRUNCATED = "truncated"
    UNCLUSTERED = "unclustered"


#: Человекочитаемые подписи для отчёта и хаб-страницы.
LABELS: dict[Reason, str] = {
    Reason.OK: "kept",
    Reason.OWN_BRAND: "navigational query for our own brand",
    Reason.THIRD_PARTY_BRAND: "third-party trademark",
    Reason.NON_ENGLISH: "not English",
    Reason.NOT_A_LANDING_INTENT: "intent does not fit a catalog landing page",
    Reason.IRRELEVANT: "unrelated to the design asset catalog",
    Reason.DUPLICATE: "merged into another query",
    Reason.LOW_VOLUME: "below the impressions threshold",
    Reason.TRUNCATED: "outside the processed top of the export",
    Reason.UNCLUSTERED: "did not make the target number of pages",
}

#: Причины, которые может назначить LLM-классификатор (§9 SPEC).
LLM_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.OK,
        Reason.OWN_BRAND,
        Reason.THIRD_PARTY_BRAND,
        Reason.NON_ENGLISH,
        Reason.NOT_A_LANDING_INTENT,
        Reason.IRRELEVANT,
    }
)
