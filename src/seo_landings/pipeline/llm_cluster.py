"""Кластеризация групп моделью.

Лексика уже свернула варианты написания. Здесь решается то, что словам не
под силу: «custom cursor» и «custom mouse pointer» — одна страница, а
«custom cursor» и «custom icons» — разные, хотя общее слово у них есть.

Всё, что после ответа модели, — детерминированно: потерянные группы
пристраиваются по пересечению слов, мелкие кластеры сливаются, набор
доводится до целевого размера и сортируется по потенциалу. Модель решает,
что с чем связано; сколько получится страниц — решает код.
"""

import logging
from dataclasses import dataclass, field

from ..domain.models import Cluster, Intent, LexicalGroup
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import PromptError, load_prompt
from ..settings import Settings
from .cluster import (
    ClusterPolicy,
    _ensure_unique_slugs,
    _make_cluster,
    _merge_by_shared_tokens,
    _rank,
    _select,
    generic_tokens,
    guess_intent,
    slugify,
)

log = logging.getLogger(__name__)

STAGE = "cluster"
SAMPLE_QUERIES = 5

_INTENTS = frozenset({"transactional", "informational", "navigational"})


@dataclass(slots=True)
class ClusterResult:
    clusters: list[Cluster]
    uncovered: list[LexicalGroup]
    warnings: list[str] = field(default_factory=list)
    used_llm: bool = False


def build_clusters(
    groups: list[LexicalGroup],
    client: LLMClient,
    settings: Settings,
    policy: ClusterPolicy,
    *,
    prompts_dir: str | None = None,
) -> ClusterResult:
    """Собрать кластеры ответом модели; при неудаче вернуть пустой результат."""
    warnings: list[str] = []
    if not groups:
        return ClusterResult([], [], warnings, used_llm=False)

    try:
        prompt = load_prompt("cluster", prompts_dir)
    except PromptError as error:
        return ClusterResult([], [], [f"кластеризация моделью пропущена: {error}"], False)

    listing = "\n".join(_describe(group) for group in groups)
    system, user = prompt.render(
        site=settings.get("site.base_url", ""),
        site_name=settings.get("site.name", ""),
        business=settings.get(
            "site.business", "a catalog of design assets: icons, illustrations, 3D graphics"
        ),
        target=policy.target,
        count=len(groups),
        groups=listing,
    )

    try:
        payload = client.complete_json(
            stage=STAGE,
            model=settings.get("models.cluster", ""),
            system=system,
            user=user,
            validate=_has_clusters,
        )
    except LLMError as error:
        return ClusterResult([], [], [f"кластеризация моделью не удалась ({error})"], False)

    clusters, warnings = _assemble(payload["clusters"], groups)
    if not clusters:
        return ClusterResult([], [], warnings + ["модель не собрала ни одного кластера"], False)

    clusters, split_notes = _split_to_target(clusters, policy)
    warnings += split_notes
    clusters.sort(key=_rank)
    selected, leftover = _select(clusters, policy)
    _ensure_unique_slugs(selected)
    return ClusterResult(selected, leftover, warnings, used_llm=True)


def _split_to_target(
    clusters: list[Cluster], policy: ClusterPolicy
) -> tuple[list[Cluster], list[str]]:
    """Разделить слишком крупные кластеры, если страниц вышло мало.

    Модель склонна собирать одну широкую страницу «про всё»: в первом же
    прогоне 21 группа из 43 уехала в кластер «icons» — вместе PNG, SVG,
    бесплатные, для сайта и для презентаций. Такая страница не ранжируется
    ни по одному своему запросу. Сколько будет страниц — решает код, и
    разделение идёт по той же лексике, что работает в офлайн-режиме.
    """
    notes: list[str] = []
    minimum = max(1, policy.target - policy.tolerance)
    unsplittable: set[str] = set()

    # Делим только то, что крупнее средней страницы. Иначе, добивая
    # количество, мы разберём обратно как раз то объединение по смыслу, ради
    # которого модель и звалась: «custom cursor» + «mouse pointer» — одна
    # страница, даже если страниц в итоге выйдет меньше целевого числа.
    total = sum(len(cluster.groups) for cluster in clusters)
    oversized = max(2, total / max(1, policy.target))

    while len(clusters) < minimum:
        candidates = [
            cluster for cluster in clusters
            if len(cluster.groups) > oversized and cluster.slug not in unsplittable
        ]
        if not candidates:
            break

        biggest = max(candidates, key=lambda cluster: (len(cluster.groups), cluster.potential))
        pieces = _split(biggest)
        if len(pieces) < 2:
            unsplittable.add(biggest.slug)
            continue

        notes.append(
            f"кластер «{biggest.primary_keyword}» из {len(biggest.groups)} групп разделён "
            f"на {len(pieces)}: слишком широкий для одной страницы"
        )
        index = clusters.index(biggest)
        clusters[index : index + 1] = pieces

    if len(clusters) < minimum:
        notes.append(
            f"собрано {len(clusters)} страниц вместо {policy.target}: "
            "запросы дальше не делятся"
        )
    return clusters, notes


def _split(cluster: Cluster) -> list[Cluster]:
    """Разложить кластер обратно на подгруппы по общим словам."""
    generic = generic_tokens(cluster.groups)
    bundles = _merge_by_shared_tokens(cluster.groups, generic)
    if len(bundles) < 2:
        return [cluster]

    pieces = [_make_cluster(bundle) for bundle in bundles]
    for piece in pieces:
        piece.source = "llm"
        piece.rationale = f"часть кластера «{cluster.primary_keyword}»"
    return pieces


def _describe(group: LexicalGroup) -> str:
    position = "—" if group.avg_position is None else f"{group.avg_position:.1f}"
    samples = ", ".join(query.text for query in group.queries[:SAMPLE_QUERIES])
    return (
        f"{group.key} | {group.canonical} | {samples} | {group.clicks} | "
        f"{group.impressions} | {position} | {group.potential:.0f}"
    )


def _has_clusters(payload: dict) -> bool:
    return isinstance(payload.get("clusters"), list) and bool(payload["clusters"])


def _assemble(
    raw_clusters: list, groups: list[LexicalGroup]
) -> tuple[list[Cluster], list[str]]:
    """Собрать кластеры из ответа модели, ничего не потеряв по дороге."""
    by_key = {group.key: group for group in groups}
    warnings: list[str] = []
    clusters: list[Cluster] = []
    taken: set[str] = set()
    unknown: list[str] = []

    for raw in raw_clusters:
        if not isinstance(raw, dict):
            continue
        members: list[LexicalGroup] = []
        for key in raw.get("group_keys") or []:
            name = str(key).strip()
            if name in taken:
                continue  # модель повторила группу в двух кластерах
            group = by_key.get(name)
            if group is None:
                unknown.append(name)
                continue
            members.append(group)
            taken.add(name)

        if not members:
            continue
        clusters.append(_make(raw, members))

    if unknown:
        warnings.append(
            f"модель назвала {len(unknown)} несуществующих групп, они пропущены: "
            + ", ".join(unknown[:3])
        )

    orphans = [group for group in groups if group.key not in taken]
    if orphans:
        placed = _place_orphans(orphans, clusters)
        warnings.append(
            f"модель забыла {len(orphans)} групп; {placed} пристроены по общим словам, "
            f"{len(orphans) - placed} стали отдельными страницами"
        )

    return clusters, warnings


def _make(raw: dict, members: list[LexicalGroup]) -> Cluster:
    members.sort(key=lambda group: (-group.potential, -group.clicks, group.key))
    keyword = str(raw.get("primary_keyword") or members[0].canonical).strip()
    slug = slugify(str(raw.get("slug") or keyword))
    intent = str(raw.get("intent", "")).strip().casefold()

    return Cluster(
        slug=slug,
        primary_keyword=keyword or members[0].canonical,
        groups=members,
        intent=intent if intent in _INTENTS else guess_intent(members),  # type: ignore[arg-type]
        source="llm",
        rationale=str(raw.get("rationale", ""))[:200],
    )


def _place_orphans(orphans: list[LexicalGroup], clusters: list[Cluster]) -> int:
    """Пристроить забытые группы к ближайшему кластеру по общим словам."""
    placed = 0
    for group in orphans:
        tokens = set(group.key.split("-"))
        best: Cluster | None = None
        best_score = 0
        for cluster in clusters:
            score = len(tokens & _cluster_tokens(cluster))
            if score > best_score:
                best, best_score = cluster, score

        if best is not None:
            best.groups.append(group)
            placed += 1
        else:
            clusters.append(
                Cluster(
                    slug=slugify(group.canonical),
                    primary_keyword=group.canonical,
                    groups=[group],
                    intent=guess_intent([group]),
                    source="lexical",
                    rationale="группа не попала в ответ модели",
                )
            )
    return placed


def _cluster_tokens(cluster: Cluster) -> set[str]:
    return {token for group in cluster.groups for token in group.key.split("-")}
