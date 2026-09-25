"""Оценка потенциала роста.

Лендинг имеет смысл там, где показы уже есть, а позиция низкая. Запрос,
по которому сайт стоит в топ-3, новой страницей не улучшить — он получает
нулевой потенциал и не тянет кластер вверх.
"""

TOP_POSITION = 3.0
"""Позиция, начиная с которой роста от нового лендинга ждать не стоит."""

FLOOR_POSITION = 10.0
"""Позиция, с которой потенциал считается максимальным."""

UNKNOWN_OPPORTUNITY = 0.5
"""Нейтральный множитель, если позиции в выгрузке нет."""


def opportunity(position: float | None) -> float:
    """Множитель 0..1: насколько далеко запрос от топ-3."""
    if position is None:
        return UNKNOWN_OPPORTUNITY
    raw = (position - TOP_POSITION) / (FLOOR_POSITION - TOP_POSITION)
    return min(1.0, max(0.0, raw))


def potential(impressions: int, position: float | None) -> float:
    """Потенциал одного запроса: показы, взвешенные разрывом до топ-3."""
    return impressions * opportunity(position)
