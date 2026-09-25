"""Чтение выгрузки Search Console в список Query.

Загрузчик ничего не выбрасывает и ничего не решает: его задача — прочитать
файл в любом виде, в котором консоль его отдала, и честно сообщить, что
именно он нашёл. Все решения о запросах принимают следующие стадии.
"""

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import Query
from ..domain.text import collapse_spaces
from . import headers as headers_module
from .numbers import parse_count, parse_number

log = logging.getLogger(__name__)

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "latin-1")
_DELIMITERS = ",;\t|"
_MAX_SAME_WARNINGS = 3


class InputError(Exception):
    """Выгрузку прочитать нельзя — прогон останавливается с кодом 1."""


@dataclass(slots=True)
class LoadResult:
    queries: list[Query] = field(default_factory=list)
    rows_read: int = 0
    rows_skipped: int = 0
    columns_found: dict[str, str] = field(default_factory=dict)
    columns_missing: list[str] = field(default_factory=list)
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    warnings: list[str] = field(default_factory=list)


def load_export(path: str | Path) -> LoadResult:
    """Прочитать `.csv` или `.zip` с выгрузкой."""
    source = Path(path)
    if not source.exists():
        raise InputError(f"файл не найден: {source}")

    raw = _read_bytes(source)
    text, encoding = _decode(raw, source)
    result = LoadResult(encoding=encoding)

    rows = _parse_rows(text, result)
    if not rows:
        raise InputError(f"файл пустой: {source}")

    header_row, *data_rows = rows
    indexes, found, missing = headers_module.map_columns(header_row)
    result.columns_found = found
    result.columns_missing = missing

    if headers_module.QUERY not in indexes:
        raise InputError(
            "в выгрузке не найдена колонка с запросом. "
            f"Найденные заголовки: {', '.join(h.strip() for h in header_row if h.strip()) or '—'}. "
            "Ожидается одна из: " + ", ".join(headers_module.SYNONYMS[headers_module.QUERY][:4])
        )

    for column in missing:
        result.warnings.append(
            f"в выгрузке нет колонки «{column}» — значения приняты нулевыми"
        )

    _read_rows(data_rows, indexes, result)
    return result


def _read_bytes(source: Path) -> bytes:
    if source.suffix.casefold() == ".zip":
        return _read_from_zip(source)
    return source.read_bytes()


def _read_from_zip(source: Path) -> bytes:
    """Search Console часто отдаёт архив — берём первый csv внутри."""
    with zipfile.ZipFile(source) as archive:
        names = [n for n in archive.namelist() if n.casefold().endswith(".csv")]
        if not names:
            raise InputError(f"в архиве {source.name} нет ни одного csv-файла")
        preferred = [n for n in names if "quer" in n.casefold()]
        return archive.read(preferred[0] if preferred else names[0])


def _decode(raw: bytes, source: Path) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise InputError(f"не удалось определить кодировку файла {source.name}")


def _parse_rows(text: str, result: LoadResult) -> list[list[str]]:
    sample = "\n".join(line for line in text.splitlines()[:5] if line.strip())
    if not sample:
        return []

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = max(_DELIMITERS, key=sample.count)
        if sample.count(delimiter) == 0:
            delimiter = ","
    result.delimiter = delimiter

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any(cell.strip() for cell in row)]


def _read_rows(rows: list[list[str]], indexes: dict[str, int], result: LoadResult) -> None:
    unparsed: dict[str, int] = {}

    for row in rows:
        result.rows_read += 1
        raw_query = _cell(row, indexes.get(headers_module.QUERY))
        text = collapse_spaces(raw_query)
        if not text:
            result.rows_skipped += 1
            continue

        clicks = _number(row, indexes, headers_module.CLICKS, unparsed, count=True)
        impressions = _number(row, indexes, headers_module.IMPRESSIONS, unparsed, count=True)
        ctr = _number(row, indexes, headers_module.CTR, unparsed)
        position = _number(row, indexes, headers_module.POSITION, unparsed)

        if ctr > 1:  # «54.95» вместо «0.5495»
            ctr /= 100.0
        if not ctr and impressions:
            ctr = clicks / impressions

        result.queries.append(
            Query(
                raw=raw_query.strip(),
                text=text.casefold(),
                clicks=int(round(clicks)),
                impressions=int(round(impressions)),
                ctr=ctr,
                position=position if position > 0 else None,
            )
        )

    for column, count in unparsed.items():
        result.warnings.append(
            f"в колонке «{column}» не разобрано значений: {count} — приняты нулевыми"
        )


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _number(
    row: list[str],
    indexes: dict[str, int],
    column: str,
    unparsed: dict[str, int],
    *,
    count: bool = False,
) -> float:
    raw = _cell(row, indexes.get(column))
    value, ok = parse_count(raw) if count else parse_number(raw)
    if not ok:
        unparsed[column] = unparsed.get(column, 0) + 1
        if unparsed[column] <= _MAX_SAME_WARNINGS:
            log.debug("не разобрано значение %r в колонке %s", raw, column)
    return value
