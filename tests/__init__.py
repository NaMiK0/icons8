"""Тесты запускаются без установки пакета: `python3 -m unittest discover -s tests`."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Стадии намеренно предупреждают о деградации через logging; в прогоне тестов
# эти сообщения — шум, проверяются они через возвращаемые warnings.
logging.disable(logging.WARNING)
