"""Загрузка промптов из config/prompts.

Промпты живут в текстовых файлах, а не в коде, по практической причине:
доработка формулировки — самая частая правка при работе с моделью, и она
не должна требовать чтения Python. Поправить «не считай X мусором» можно
прямо во время разговора.

Формат файла: markdown, где первый раздел «## System» — системная часть,
второй «## User» — шаблон пользовательского сообщения с подстановками
вида {queries}.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path("config/prompts")

_SECTION = re.compile(r"^##\s+(system|user)\s*$", re.IGNORECASE | re.MULTILINE)


class PromptError(Exception):
    """Промпт не найден или составлен неправильно."""


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    system: str
    user_template: str

    def render(self, **values: object) -> tuple[str, str]:
        """Подставить значения в пользовательскую часть."""
        try:
            return self.system, self.user_template.format(**values)
        except KeyError as error:
            raise PromptError(
                f"в промпте {self.name} нет подстановки {error}"
            ) from error


def load_prompt(name: str, directory: str | Path | None = None) -> Prompt:
    """Прочитать промпт по имени файла без расширения."""
    base = Path(directory) if directory else DEFAULT_PROMPTS_DIR
    path = base / f"{name}.md"
    if not path.exists():
        raise PromptError(f"промпт не найден: {path}")

    text = path.read_text(encoding="utf-8")
    parts = _SECTION.split(text)
    if len(parts) < 5:
        raise PromptError(
            f"в {path} нужны разделы «## System» и «## User»"
        )

    sections = {parts[i].casefold(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if "system" not in sections or "user" not in sections:
        raise PromptError(f"в {path} нужны разделы «## System» и «## User»")

    return Prompt(name=name, system=sections["system"], user_template=sections["user"])
