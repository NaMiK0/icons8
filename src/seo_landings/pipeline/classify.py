"""Классификация запросов моделью.

Правила из filters.py отвечают на объективные вопросы: свой бренд, язык,
мусор. Здесь задаётся вопрос, требующий суждения: заслуживает ли запрос
лендинга в каталоге. Захардкодить это нельзя — на следующей выгрузке будут
другие бренды и другие формулировки.

Решения правил по own_brand и non_english имеют приоритет над моделью: они
проверяемы без неё, и спорить тут не о чем. Всё остальное модель может
изменить — и её вклад виден в отчёте по полю `source`.
"""

import logging
from dataclasses import dataclass

from ..domain.models import Decision, Intent, Query
from ..domain.reasons import LLM_REASONS, Reason
from ..domain.text import levenshtein
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import Prompt, PromptError, load_prompt
from ..settings import Settings, model_chain
from .normalize import TYPO_MIN_KEY_LENGTH, lexical_key

log = logging.getLogger(__name__)

STAGE = "classify"
DEFAULT_BATCH_SIZE = 50

#: Причины, которые остаются за правилами, что бы ни ответила модель.
RULE_OWNED = frozenset({Reason.OWN_BRAND, Reason.NON_ENGLISH})

_INTENTS = frozenset({"transactional", "informational", "navigational"})


@dataclass(slots=True)
class ClassifyResult:
    decisions: list[Decision]
    warnings: list[str]
    used_llm: bool


def classify(
    decisions: list[Decision],
    client: LLMClient,
    settings: Settings,
    *,
    batch_size: int | None = None,
    prompts_dir: str | None = None,
) -> ClassifyResult:
    """Уточнить решения правил ответами модели.

    В модель уходят только те запросы, по которым правила ничего не решили:
    гонять через неё заведомо брендовые — трата денег и лишний шанс на
    ошибку.
    """
    warnings: list[str] = []
    pending = [d for d in decisions if d.keep or d.reason not in RULE_OWNED]
    if not pending:
        return ClassifyResult(decisions, warnings, used_llm=False)

    try:
        prompt = load_prompt("classify", prompts_dir)
    except PromptError as error:
        warnings.append(f"классификация пропущена: {error}")
        return ClassifyResult(decisions, warnings, used_llm=False)

    model = model_chain(settings, "classify")
    size = batch_size or int(settings.get("llm.batch_size", DEFAULT_BATCH_SIZE))
    verdicts: dict[str, dict] = {}

    batches = list(_batched([d.query for d in pending], size))
    failed = 0
    for number, batch in enumerate(batches, start=1):
        try:
            verdicts.update(_ask(client, prompt, settings, model, batch))
        except LLMError as error:
            # Падает пачка, а не стадия: остальные запросы модель уже
            # разобрала, и терять эту работу из-за одной неудачи незачем.
            failed += 1
            warnings.append(
                f"пачка {number} из {len(batches)} не классифицирована ({error}) — "
                "для неё оставлены решения правил"
            )

    if failed == len(batches):
        warnings.append("модель не ответила ни по одной пачке — прогон идёт на правилах")
        return ClassifyResult(decisions, warnings, used_llm=False)

    updated, missing = _merge(decisions, verdicts)
    updated, rescued = _rescue_variants(updated)
    if rescued:
        warnings.append(
            "возвращены варианты написания оставленных запросов: " + ", ".join(rescued[:5])
        )
    if missing:
        warnings.append(
            f"модель не вернула решение по {len(missing)} запросам — оставлены правилами: "
            + ", ".join(missing[:3])
        )
    return ClassifyResult(updated, warnings, used_llm=True)


def _ask(
    client: LLMClient, prompt: Prompt, settings: Settings, model: list[str], batch: list[Query]
) -> dict[str, dict]:
    """Спросить модель про одну пачку запросов.

    Обмен идёт по номерам строк, а не по тексту запроса: на проверке модель
    копировала в ответ всю строку вместе с метриками, и сопоставление по
    тексту разваливалось. Номер она возвращает как число, испортить его
    труднее.
    """
    listing = "\n".join(
        f"{index}. {query.text} | clicks {query.clicks}"
        f" | impressions {query.impressions} | position {_position(query)}"
        for index, query in enumerate(batch, start=1)
    )
    system, user = prompt.render(
        site=settings.get("site.base_url", ""),
        business=settings.get("site.business", ""),
        market=settings.get("market.language", "en"),
        count=len(batch),
        queries=listing,
    )
    payload = client.complete_json(
        stage=STAGE, model=model, system=system, user=user, validate=_has_items
    )
    items = payload["items"]

    verdicts: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        query = _resolve(item, batch)
        if query is not None:
            verdicts[query.text] = item
    return verdicts


def _has_items(payload: dict) -> bool:
    """Годным считается только объект с непустым списком items."""
    return isinstance(payload.get("items"), list) and bool(payload["items"])


def _resolve(item: dict, batch: list[Query]) -> Query | None:
    """Сопоставить ответ с запросом: сначала по номеру, затем по тексту."""
    try:
        index = int(item["id"])
        if 1 <= index <= len(batch):
            return batch[index - 1]
    except (KeyError, TypeError, ValueError):
        pass

    text = str(item.get("query", "")).split("|")[0].strip().casefold()
    return next((query for query in batch if query.text == text), None)


def _merge(
    decisions: list[Decision], verdicts: dict[str, dict]
) -> tuple[list[Decision], list[str]]:
    """Наложить ответы модели на решения правил."""
    merged: list[Decision] = []
    missing: list[str] = []

    for decision in decisions:
        if not decision.keep and decision.reason in RULE_OWNED:
            merged.append(decision)
            continue

        verdict = verdicts.get(decision.query.text)
        if verdict is None:
            missing.append(decision.query.text)
            merged.append(decision)
            continue

        merged.append(_apply(decision, verdict))

    return merged, missing


def _rescue_variants(decisions: list[Decision]) -> tuple[list[Decision], list[str]]:
    """Вернуть запросы, отброшенные как чужой бренд, но являющиеся вариантом
    написания того, что мы оставляем.

    «freeicon» — это «free icon» без пробела, а не торговая марка. Правило
    общее: если запрос отличается от оставленного максимум на одну правку,
    брендом он быть не может, что бы ни решила модель.
    """
    kept_keys = {lexical_key(d.query.text) for d in decisions if d.keep}
    if not kept_keys:
        return decisions, []

    result: list[Decision] = []
    rescued: list[str] = []
    for decision in decisions:
        if decision.keep or decision.reason is not Reason.THIRD_PARTY_BRAND:
            result.append(decision)
            continue

        key = lexical_key(decision.query.text)
        near = len(key) >= TYPO_MIN_KEY_LENGTH and any(
            levenshtein(key, kept, max_distance=1) <= 1 for kept in kept_keys
        )
        if not near:
            result.append(decision)
            continue

        rescued.append(decision.query.text)
        result.append(
            Decision(query=decision.query, keep=True, reason=Reason.OK,
                     note="вариант написания оставленного запроса",
                     source="rule", intent=decision.intent)
        )
    return result, rescued


def _apply(decision: Decision, verdict: dict) -> Decision:
    """Собрать решение из ответа модели, починив очевидные огрехи."""
    keep = bool(verdict.get("keep", True))
    reason = _reason(verdict.get("reason"), keep)
    intent = _intent(verdict.get("intent"))
    note = str(verdict.get("note", ""))[:120]

    if decision.source == "flag":
        # Флаг --keep-* сильнее и правил, и модели.
        return Decision(query=decision.query, keep=True, reason=Reason.OK,
                        note=decision.note, source="flag", intent=intent)

    return Decision(
        query=decision.query,
        keep=keep,
        reason=reason,
        note=note,
        source="llm",
        intent=intent,
    )


def _reason(value: object, keep: bool) -> Reason:
    """Причина из закрытого перечня; «keep=false, reason=ok» бессмысленно."""
    try:
        reason = Reason(str(value))
    except ValueError:
        reason = Reason.OK if keep else Reason.IRRELEVANT
    if reason not in LLM_REASONS:
        reason = Reason.OK if keep else Reason.IRRELEVANT
    if keep:
        return Reason.OK
    return Reason.IRRELEVANT if reason is Reason.OK else reason


def _intent(value: object) -> Intent | None:
    text = str(value).strip().casefold()
    return text if text in _INTENTS else None  # type: ignore[return-value]


def _position(query: Query) -> str:
    return "unknown" if query.position is None else f"{query.position:.1f}"


def _batched(items: list[Query], size: int):
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]
