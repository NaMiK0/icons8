"""Детерминированные правила отбора запросов.

Здесь только то, что можно проверить механически и объяснить одной строкой:
свой бренд, язык, явный мусор, порог объёма. Всё, что требует суждения
(чужая торговая марка, пригодность интента для лендинга), — работа стадии
классификации: список брендов нельзя захардкодить, на следующей выгрузке
они будут другими.

Маркеры из секции [offline] конфига — подпорка для режима без LLM, чтобы
офлайн-прогон не выглядел наивным. В обычном режиме решение уточнит модель.
"""

import re
from dataclasses import dataclass

from ..domain.models import Decision, Query
from ..domain.reasons import Reason
from ..domain.text import levenshtein, squash, strip_accents
from ..settings import Settings

_URL = re.compile(r"^(https?://|www\.)|\.(com|net|org|ru|io)\b")
_LATIN = re.compile(r"^[a-z0-9\s\-_'&/.,!?+()#@:*]+$")
_WORD = re.compile(r"[a-z0-9]+")

#: Слова, по которым язык виден без словаря целиком.
DEFAULT_NON_ENGLISH_MARKERS = (
    "pobrania", "myszki", "kursor", "darmowe",
    "descargar", "gratis", "gratuit", "telecharger", "kostenlos", "herunterladen",
    "scarica", "baixar", "bedava", "indir", "unduh", "gratuito",
)


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    """Настройки правил, снятые с конфига и флагов."""

    brand_aliases: tuple[str, ...] = ()
    brand_fuzzy_distance: int = 1
    brand_products: tuple[str, ...] = ()
    min_impressions: int = 0
    non_english_markers: tuple[str, ...] = DEFAULT_NON_ENGLISH_MARKERS
    third_party_markers: tuple[str, ...] = ()
    non_landing_markers: tuple[str, ...] = ()
    keep_brand: bool = False
    keep_third_party: bool = False
    use_offline_markers: bool = True

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        keep_brand: bool = False,
        keep_third_party: bool = False,
        use_offline_markers: bool = True,
    ) -> "FilterPolicy":
        aliases = tuple(
            alias for alias in (squash(a) for a in settings.get("brand.aliases", [])) if alias
        )
        markers = settings.get("market.non_english_markers") or DEFAULT_NON_ENGLISH_MARKERS
        products = tuple(
            " ".join(_WORD.findall(str(product).casefold()))
            for product in settings.get("brand.products", []) or []
        )
        return cls(
            brand_aliases=aliases,
            brand_fuzzy_distance=int(settings.get("brand.fuzzy_distance", 1)),
            brand_products=tuple(product for product in products if product),
            min_impressions=int(settings.get("input.min_impressions", 0)),
            non_english_markers=tuple(markers),
            third_party_markers=tuple(settings.get("offline.third_party_markers", [])),
            non_landing_markers=tuple(settings.get("offline.non_landing_markers", [])),
            keep_brand=keep_brand,
            keep_third_party=keep_third_party,
            use_offline_markers=use_offline_markers,
        )


def apply(queries: list[Query], policy: FilterPolicy) -> list[Decision]:
    """Прогнать правила по всем запросам, сохранив порядок."""
    return [decide(query, policy) for query in queries]


def decide(query: Query, policy: FilterPolicy) -> Decision:
    """Решение по одному запросу.

    Порядок проверок — от самого объективного к самому спорному.
    """
    text = query.text

    if is_junk(text):
        return _drop(query, Reason.IRRELEVANT, "пустой запрос или адрес сайта")

    if is_own_brand(text, policy):
        if policy.keep_brand:
            return _keep(query, "brand kept by --keep-brand", source="flag")
        return _drop(query, Reason.OWN_BRAND, "навигационный запрос своего бренда")

    marker = non_english_marker(text, policy)
    if marker:
        return _drop(query, Reason.NON_ENGLISH, f"неанглийское слово: {marker}")

    if policy.min_impressions and query.impressions < policy.min_impressions:
        return _drop(
            query, Reason.LOW_VOLUME, f"показов меньше порога {policy.min_impressions}"
        )

    if policy.use_offline_markers:
        marker = _first_marker(text, policy.third_party_markers)
        if marker:
            if policy.keep_third_party:
                return _keep(query, "third-party kept by --keep-third-party", source="flag")
            return _drop(query, Reason.THIRD_PARTY_BRAND, f"чужой бренд: {marker}")

        marker = _first_marker(text, policy.non_landing_markers)
        if marker:
            return _drop(query, Reason.NOT_A_LANDING_INTENT, f"интент не для лендинга: {marker}")

    return _keep(query, "")


def is_junk(text: str) -> bool:
    """Пустая строка, один символ или адрес сайта."""
    stripped = text.strip()
    return len(stripped) < 2 or bool(_URL.search(stripped))


def is_own_brand(text: str, policy: FilterPolicy) -> bool:
    """Запрос своего бренда в любом написании.

    Все варианты бренда содержат цифру («icons8», «icon 8», «8icons»), и это
    не украшение правила, а его предохранитель: без требования цифры
    «icons» отличается от «icons8» на один символ и был бы отброшен как
    брендовый — а это самый ценный общий запрос в выгрузке.
    """
    if _mentions_product(text, policy.brand_products):
        return True
    if not policy.brand_aliases:
        return False

    squashed = squash(text)
    if not squashed or not any(ch.isdigit() for ch in squashed):
        return False

    for alias in policy.brand_aliases:
        if alias in squashed:
            return True
        if levenshtein(squashed, alias, max_distance=policy.brand_fuzzy_distance) <= (
            policy.brand_fuzzy_distance
        ):
            return True
    return False


def _mentions_product(text: str, products: tuple[str, ...]) -> bool:
    """Название собственного продукта — слово целиком, без нечёткого поиска.

    Цифры в названиях продуктов нет, и нечёткое сравнение тут опасно:
    «lunacy» на одну правку от чего-нибудь общеупотребительного. Поэтому
    только точное совпадение слов.
    """
    if not products:
        return False
    words = " " + " ".join(_WORD.findall(strip_accents(text).casefold())) + " "
    return any(f" {product} " in words for product in products)


def non_english_marker(text: str, policy: FilterPolicy) -> str | None:
    """Вернуть признак того, что запрос не на английском."""
    normalized = strip_accents(text).casefold()
    if not _LATIN.match(normalized):
        return "нелатинские символы"

    tokens = set(_WORD.findall(normalized))
    for marker in policy.non_english_markers:
        if marker in tokens:
            return marker
    return None


def _first_marker(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if _contains(text, marker):
            return marker
    return None


def _contains(text: str, marker: str) -> bool:
    """Вхождение маркера по границам слов: «apple» не должен ловить «applет»."""
    pattern = r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])"
    return bool(re.search(pattern, text))


def _keep(query: Query, note: str, source: str = "rule") -> Decision:
    return Decision(query=query, keep=True, reason=Reason.OK, note=note, source=source)


def _drop(query: Query, reason: Reason, note: str) -> Decision:
    return Decision(query=query, keep=False, reason=reason, note=note, source="rule")
