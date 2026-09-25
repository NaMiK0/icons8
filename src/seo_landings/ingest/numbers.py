"""Разбор чисел в том виде, в каком их отдаёт Search Console.

Одна и та же метрика приезжает как `26306`, `26,306`, `26 306`, `26.306`
или `54.95%` в зависимости от локали интерфейса. Отдельный модуль — потому
что именно здесь скрипты ломаются на чужих выгрузках.

Разбор зависит от того, что за колонка: клики и показы — целые счётчики, у
них точка и запятая могут быть только разделителями тысяч; CTR и позиция —
дробные, у них точка всегда десятичная. Без этого различия `0.083` (CTR)
превращается в `83`.
"""

from typing import Literal

Kind = Literal["count", "decimal"]

_BLANKS = {"", "-", "--", "—", "–", "n/a", "na", "null", "none"}
_SPACE_CHARS = (" ", " ", " ", " ")


def parse_number(raw: object, kind: Kind = "decimal") -> tuple[float, bool]:
    """Вернуть (значение, распознано).

    Нераспознанное значение даёт `(0.0, False)`: загрузчик превратит это в
    предупреждение, но прогон не остановит.
    """
    if raw is None:
        return 0.0, True

    value = str(raw).strip()
    for space in _SPACE_CHARS:
        value = value.replace(space, "")
    if value.casefold() in _BLANKS:
        return 0.0, True

    value = value.replace("−", "-")  # типографский минус

    percent = value.endswith("%")
    if percent:
        value = value[:-1].strip()

    value = _strip_group_separators(value, kind)

    try:
        number = float(value)
    except ValueError:
        return 0.0, False

    if percent:
        number /= 100.0
    return number, True


def parse_count(raw: object) -> tuple[float, bool]:
    """Разбор целочисленной метрики: клики, показы."""
    return parse_number(raw, kind="count")


def _strip_group_separators(value: str, kind: Kind) -> str:
    """Убрать разделители тысяч, сохранив десятичную часть."""
    has_dot = "." in value
    has_comma = "," in value

    if has_dot and has_comma:
        # Десятичным считается разделитель, стоящий правее: 26,306.5 и 1.234,56.
        if value.rfind(",") > value.rfind("."):
            return value.replace(".", "").replace(",", ".")
        return value.replace(",", "")

    if has_comma:
        if kind == "count" or _looks_like_thousands(value, ","):
            return value.replace(",", "")
        return value.replace(",", ".")

    if has_dot and kind == "count" and _looks_like_thousands(value, "."):
        return value.replace(".", "")

    return value


def _looks_like_thousands(value: str, separator: str) -> bool:
    """Отличить `26,306` (тысячи) от `0,5495` (десятичная запятая).

    Признак разделителя тысяч: после него каждая группа — ровно три цифры,
    а перед ним стоит от одной до трёх цифр.
    """
    head, *tail = value.lstrip("+-").split(separator)
    if not tail or not head.isdigit() or not 1 <= len(head) <= 3:
        return False
    return all(part.isdigit() and len(part) == 3 for part in tail)
