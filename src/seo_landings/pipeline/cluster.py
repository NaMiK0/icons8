"""Сборка лендингов из лексических групп — без LLM.

Группа это ещё не страница: «custom cursor», «cursor download» и «custom
mouse cursor» живут в трёх группах, а лендинг им нужен один. Здесь группы
сливаются по общим значимым словам.

Правило простое и заведомо грубее смыслового: «custom cursor» и «custom
mouse pointer» общих слов не имеют и в один кластер не попадут. Это
осознанно — на фоне такого фолбэка видно, что именно добавляет модель на
стадии кластеризации, и есть чем работать, когда API недоступен.
"""

import re
from collections import Counter
from dataclasses import dataclass

from ..domain.models import Cluster, Intent, LexicalGroup
from ..settings import Settings

#: Доля групп, выше которой слово считается слишком общим для склейки.
#: «icon» встречается почти везде — склеивать по нему значит собрать
#: один кластер на всю выгрузку.
GENERIC_TOKEN_SHARE = 0.3

#: Доля общих слов, начиная с которой группы считаются одной страницей.
MERGE_THRESHOLD = 0.3

_TRANSACTIONAL = ("download", "free", "buy", "get", "generator", "maker", "creator")
_INFORMATIONAL = ("what", "how", "why", "best", "trends", "ideas", "meaning")

_SLUG_TRASH = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ClusterPolicy:
    target: int = 10
    tolerance: int = 2
    min_queries_per_cluster: int = 2

    @classmethod
    def from_settings(
        cls, settings: Settings, *, target: int | None = None, tolerance: int | None = None
    ) -> "ClusterPolicy":
        return cls(
            target=int(target if target is not None else settings.get("pages.target", 10)),
            tolerance=int(
                tolerance if tolerance is not None else settings.get("pages.tolerance", 2)
            ),
            min_queries_per_cluster=int(settings.get("pages.min_queries_per_cluster", 2)),
        )


def build_clusters(
    groups: list[LexicalGroup], policy: ClusterPolicy
) -> tuple[list[Cluster], list[LexicalGroup]]:
    """Собрать кластеры и вернуть (отобранные, не попавшие в набор)."""
    if not groups:
        return [], []

    generic = generic_tokens(groups)
    merged = _merge_by_shared_tokens(groups, generic)
    clusters = [_make_cluster(bundle) for bundle in merged]
    clusters.sort(key=_rank)

    selected, leftover = _select(clusters, policy)
    _ensure_unique_slugs(selected)
    return selected, leftover


def generic_tokens(groups: list[LexicalGroup]) -> set[str]:
    """Слова, встречающиеся слишком часто, чтобы что-то о них говорить."""
    counts = Counter(token for group in groups for token in _tokens(group))
    limit = max(2, len(groups) * GENERIC_TOKEN_SHARE)
    return {token for token, count in counts.items() if count > limit}


def _tokens(group: LexicalGroup) -> set[str]:
    return {token for token in group.key.split("-") if token}


def _merge_by_shared_tokens(
    groups: list[LexicalGroup], generic: set[str]
) -> list[list[LexicalGroup]]:
    """Слить группы вокруг затравок — самых весомых групп набора.

    Сравнение идёт с исходной затравкой, а не с накопленным набором слов.
    Иначе кластеры сцепляются цепочкой: «cursor download» подтягивает
    «icon download» через слово download, тот — «free icons download», и
    половина выгрузки оказывается на одной странице.
    """
    bundles: list[list[LexicalGroup]] = []
    seeds: list[set[str]] = []

    for group in groups:
        tokens = _tokens(group) - generic
        target = _closest_seed(tokens, seeds)
        if target is None:
            bundles.append([group])
            seeds.append(set(tokens))
        else:
            bundles[target].append(group)

    return bundles


def _closest_seed(tokens: set[str], seeds: list[set[str]]) -> int | None:
    """Найти затравку, с которой группа пересекается достаточно сильно."""
    if not tokens:
        return None

    best_index = None
    best_score = 0.0
    for index, seed in enumerate(seeds):
        shared = tokens & seed
        if not shared:
            continue
        score = len(shared) / len(tokens | seed)
        if score >= MERGE_THRESHOLD and score > best_score:
            best_index, best_score = index, score
    return best_index


def _make_cluster(bundle: list[LexicalGroup]) -> Cluster:
    bundle.sort(key=lambda group: (-group.potential, -group.clicks, group.key))
    primary = bundle[0].canonical
    return Cluster(
        slug=slugify(primary),
        primary_keyword=primary,
        groups=bundle,
        intent=guess_intent(bundle),
        source="lexical",
        rationale="склеено по общим значимым словам",
    )


def guess_intent(bundle: list[LexicalGroup]) -> Intent:
    """Грубая догадка об интенте по словам запросов."""
    text = " ".join(query.text for group in bundle for query in group.queries)
    if any(word in text for word in _INFORMATIONAL):
        return "informational"
    if any(word in text for word in _TRANSACTIONAL):
        return "transactional"
    return "transactional"


def _rank(cluster: Cluster) -> tuple:
    """Порядок страниц: потенциал, затем объём, затем алфавит (§5 SPEC)."""
    return (-cluster.potential, -cluster.impressions, -cluster.clicks, cluster.slug)


def _select(
    clusters: list[Cluster], policy: ClusterPolicy
) -> tuple[list[Cluster], list[LexicalGroup]]:
    """Отобрать целевое число страниц, мелочь вернуть как непокрытую.

    Кластеры из одного-двух запросов сначала пробуем не выбрасывать, а
    добрать ими набор: лучше страница по узкому спросу, чем пустое место.
    """
    strong = [c for c in clusters if len(c.queries) >= policy.min_queries_per_cluster]
    weak = [c for c in clusters if len(c.queries) < policy.min_queries_per_cluster]

    selected = strong[: policy.target]
    if len(selected) < policy.target - policy.tolerance:
        selected += weak[: policy.target - len(selected)]

    chosen = {id(cluster) for cluster in selected}
    leftover = [
        group
        for cluster in clusters
        if id(cluster) not in chosen
        for group in cluster.groups
    ]
    return selected, leftover


def _ensure_unique_slugs(clusters: list[Cluster]) -> None:
    seen: dict[str, int] = {}
    for cluster in clusters:
        base = cluster.slug or "landing"
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            cluster.slug = f"{base}-{seen[base]}"


def slugify(value: str, max_length: int = 60) -> str:
    """Адрес страницы из ключевого запроса: стабильный и читаемый."""
    slug = _SLUG_TRASH.sub("-", value.casefold()).strip("-")
    if len(slug) <= max_length:
        return slug or "landing"
    trimmed = slug[:max_length].rsplit("-", 1)[0]
    return trimmed or slug[:max_length]
