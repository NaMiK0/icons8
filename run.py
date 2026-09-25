#!/usr/bin/env python3
"""Точка входа: python run.py --input data/gsc_queries.csv

Пакет лежит в src/, но ставить его через pip не нужно — путь добавляется
здесь. Одной командой меньше при запуске на чужой машине.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from seo_landings.cli.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
