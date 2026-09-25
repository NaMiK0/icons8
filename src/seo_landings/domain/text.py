"""Мелкие текстовые утилиты, общие для нескольких стадий."""

import re
import unicodedata

_SPACES = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def collapse_spaces(value: str) -> str:
    """Свернуть любые пробельные последовательности в один пробел."""
    return _SPACES.sub(" ", value).strip()


def squash(value: str) -> str:
    """Оставить только латинские буквы и цифры в нижнем регистре.

    Нужна, чтобы «icons 8», «icon8. com» и «8icons» сравнивались как строки
    без пробелов и пунктуации.
    """
    return _NON_ALNUM.sub("", value.casefold())


def strip_accents(value: str) -> str:
    """Убрать диакритику: «favîcon» должен склеиваться с «favicon»."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def levenshtein(left: str, right: str, max_distance: int = 2) -> int:
    """Расстояние Левенштейна с ранним выходом.

    Возвращает `max_distance + 1`, если расстояние заведомо больше порога, —
    считать точное значение в этом случае незачем.
    """
    if left == right:
        return 0
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    if len(left) < len(right):
        left, right = right, left

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        if min(current) > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]
