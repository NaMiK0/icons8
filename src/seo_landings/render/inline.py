"""Графика страницы без единого внешнего запроса.

Требование «самодостаточные страницы» понимается буквально: файл должен
открываться по file:// без сети. Поэтому превью ассетов — инлайновые SVG,
сгенерированные из текста запроса, а не картинки из каталога.
"""

import hashlib

#: Геометрия плейсхолдеров: у разных запросов должны быть разные фигуры,
#: иначе сетка выглядит как ошибка рендера, а не как набор ассетов.
_SHAPES = (
    '<rect x="8" y="8" width="32" height="32" rx="8"/>',
    '<circle cx="24" cy="24" r="16"/>',
    '<path d="M24 6 42 40H6z"/>',
    '<rect x="8" y="8" width="32" height="32" rx="2" transform="rotate(45 24 24)"/>',
    '<path d="M24 8l5 11 12 1-9 8 3 12-11-6-11 6 3-12-9-8 12-1z"/>',
    '<rect x="6" y="14" width="36" height="20" rx="4"/>',
)


def asset_svg(label: str, index: int) -> str:
    """Инлайновый плейсхолдер ассета с подписью для скринридера."""
    digest = hashlib.sha1(f"{label}:{index}".encode()).digest()
    shape = _SHAPES[digest[0] % len(_SHAPES)]
    hue = digest[1] % 360
    return (
        '<svg role="img" aria-label="{label} preview" width="48" height="48" '
        'viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">'
        "<title>{label} preview</title>"
        '<g fill="hsl({hue} 55% 55%)">{shape}</g>'
        "</svg>"
    ).format(label=_escape(label), hue=hue, shape=shape)


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
