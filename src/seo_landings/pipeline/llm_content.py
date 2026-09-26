"""Текст страницы, написанный моделью.

Ответ проверяется теми же функциями, что ограничивают шаблон: лимиты
title и description, отсутствие выдуманных чисел. Недостающие блоки
добираются из шаблона — страница со структурной дырой хуже, чем страница
с одним суховатым абзацем.
"""

import logging
from dataclasses import dataclass, field

from ..domain.models import Cluster, FaqItem, PageContent, Section
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import PromptError, load_prompt
from ..settings import Settings, model_chain
from .content import (
    DESCRIPTION_LIMIT,
    TITLE_LIMIT,
    build_template_content,
    clip,
    risky_claims,
    suspicious_numbers,
)

log = logging.getLogger(__name__)

STAGE = "content"
MIN_SECTIONS = 3
MIN_FAQ = 4
MAX_FAQ = 6
QUERIES_SHOWN = 15


@dataclass(slots=True)
class ContentResult:
    content: PageContent
    warnings: list[str] = field(default_factory=list)
    used_llm: bool = False


def build_content(
    cluster: Cluster,
    siblings: list[Cluster],
    client: LLMClient,
    settings: Settings,
    *,
    prompts_dir: str | None = None,
) -> ContentResult:
    """Написать страницу моделью, откатившись к шаблону при неудаче."""
    site_name = settings.get("site.name", "")
    fallback = build_template_content(cluster, site_name)

    try:
        prompt = load_prompt("content", prompts_dir)
    except PromptError as error:
        return ContentResult(fallback, [f"текст {cluster.slug}: {error}"], used_llm=False)

    system, user = prompt.render(
        site=settings.get("site.base_url", ""),
        site_name=site_name,
        business=settings.get("site.business", ""),
        keyword=cluster.primary_keyword,
        intent=cluster.intent,
        queries=_queries(cluster),
        siblings=_siblings(cluster, siblings) or "—",
        title_limit=TITLE_LIMIT,
        description_limit=DESCRIPTION_LIMIT,
    )

    try:
        payload = client.complete_json(
            stage=STAGE,
            model=model_chain(settings, "content"),
            system=system,
            user=user,
            validate=_looks_like_a_page,
        )
    except LLMError as error:
        return ContentResult(
            fallback, [f"текст {cluster.slug} взят из шаблона ({error})"], used_llm=False
        )

    return _validate(payload, cluster, fallback)


def _queries(cluster: Cluster) -> str:
    return "\n".join(
        f"- {query.text} | clicks {query.clicks} | impressions {query.impressions}"
        for query in cluster.queries[:QUERIES_SHOWN]
    )


def _siblings(cluster: Cluster, siblings: list[Cluster]) -> str:
    return "\n".join(
        f"- {other.primary_keyword}" for other in siblings if other.slug != cluster.slug
    )


def _looks_like_a_page(payload: dict) -> bool:
    return bool(payload.get("h1")) and bool(payload.get("sections"))


def _validate(payload: dict, cluster: Cluster, fallback: PageContent) -> ContentResult:
    """Привести ответ модели к годному виду, отметив всё поправленное."""
    warnings: list[str] = []

    title = _text(payload.get("title")) or fallback.title
    description = _text(payload.get("meta_description")) or fallback.meta_description
    if len(title) > TITLE_LIMIT:
        warnings.append(f"{cluster.slug}: title обрезан с {len(title)} символов")
        title = clip(title, TITLE_LIMIT)
    if len(description) > DESCRIPTION_LIMIT:
        warnings.append(f"{cluster.slug}: description обрезан с {len(description)} символов")
        description = clip(description, DESCRIPTION_LIMIT)

    sections = _sections(payload.get("sections"))
    if len(sections) < MIN_SECTIONS:
        warnings.append(f"{cluster.slug}: секций {len(sections)}, добраны шаблонными")
        sections += fallback.sections[: MIN_SECTIONS - len(sections)]

    faq = _faq(payload.get("faq"))
    if len(faq) < MIN_FAQ:
        warnings.append(f"{cluster.slug}: вопросов {len(faq)}, добраны шаблонными")
        faq += fallback.faq[: MIN_FAQ - len(faq)]
    faq = faq[:MAX_FAQ]

    content = PageContent(
        title=title,
        meta_description=description,
        h1=_text(payload.get("h1")) or fallback.h1,
        intro=_text(payload.get("intro")) or fallback.intro,
        sections=sections,
        faq=faq,
        anchor_text=_text(payload.get("anchor_text")) or cluster.primary_keyword,
        source="llm",
    )

    text = _all_text(content)

    invented = suspicious_numbers(text)
    if invented:
        # Не правим: цифра может быть уместной («24x24 px»). Но в отчёте она
        # должна быть видна — выдуманное количество ассетов выглядит так же.
        warnings.append(
            f"{cluster.slug}: в тексте есть числа, проверьте их — {', '.join(invented[:5])}"
        )

    claims = risky_claims(text)
    if claims:
        warnings.append(
            f"{cluster.slug}: утверждения о лицензиях, доступе или составе каталога "
            f"({len(claims)}) — проверьте перед публикацией: " + " | ".join(claims[:2])
        )

    return ContentResult(content, warnings, used_llm=True)


def _sections(raw: object) -> list[Section]:
    sections: list[Section] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        heading = _text(item.get("heading"))
        paragraphs = [
            _text(p) for p in (item.get("paragraphs") or []) if _text(p)
        ]
        if heading and paragraphs:
            sections.append(Section(heading=heading, paragraphs=paragraphs))
    return sections


def _faq(raw: object) -> list[FaqItem]:
    items: list[FaqItem] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        question, answer = _text(item.get("question")), _text(item.get("answer"))
        if question and answer:
            items.append(FaqItem(question=question, answer=answer))
    return items


def _text(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _all_text(content: PageContent) -> str:
    parts = [content.title, content.meta_description, content.h1, content.intro]
    parts += [p for section in content.sections for p in section.paragraphs]
    parts += [item.question for item in content.faq]
    parts += [item.answer for item in content.faq]
    return " ".join(parts)
