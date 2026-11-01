import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from ..database import DEFAULT_DATABASE_PATH, connect
from .medicine_lookup import MedicineInfo, lookup_medicine_info


LOGGER = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".xlsx": "openpyxl", ".xls": "xlrd", ".ods": "odf"}


@dataclass(frozen=True)
class ImportResult:
    added: int
    updated: int
    skipped: int
    errors: tuple[str, ...]
    pending_enrichment: tuple[str, ...]


@dataclass(frozen=True)
class EnrichmentResult:
    instructions: int
    photos: int
    manufacturers: int
    no_results: int
    errors: int


def _clean_cell(value: object) -> object | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _parse_price(value: object | None, row_number: int) -> float | None:
    value = _clean_cell(value)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        price = float(value)
        if math.isfinite(price):
            return price
        raise ValueError(f"строка {row_number}: некорректная цена {value!r}")

    normalized = str(value).replace(" ", "")
    if "," in normalized and "." in normalized:
        decimal_separator = "," if normalized.rfind(",") > normalized.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        normalized = normalized.replace(thousands_separator, "").replace(decimal_separator, ".")
    else:
        normalized = normalized.replace(",", ".")
    try:
        price = float(normalized)
        if math.isfinite(price):
            return price
        raise ValueError
    except ValueError as error:
        raise ValueError(f"строка {row_number}: некорректная цена {value!r}") from error


def _parse_before(value: object | None, row_number: int) -> str | None:
    value = _clean_cell(value)
    if value is None:
        return None

    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for date_format in ("%d.%m.%Y", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                f"строка {row_number}: некорректная дата {value!r}; "
                "используйте формат dd.mm.yyyy"
            )
    return parsed.strftime("%d.%m.%Y")


def import_medicine_workbook(
    workbook_path: str | Path,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ImportResult:
    path = Path(workbook_path)
    engine = SUPPORTED_EXTENSIONS.get(path.suffix.lower())
    if engine is None:
        raise ValueError("Поддерживаются только файлы .xlsx, .xls и .ods.")

    try:
        rows = pd.read_excel(
            path,
            header=None,
            usecols=[0, 1, 3, 4],
            engine=engine,
            dtype=object,
        )
    except ImportError as error:
        raise RuntimeError(f"Не установлен обработчик формата {path.suffix}.") from error
    except ValueError as error:
        raise ValueError(
            "В таблице должны присутствовать столбцы A, B, D и E без заголовков."
        ) from error

    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    prepared_rows: list[tuple[str, str | None, str | None, float | None]] = []
    pending_names: dict[str, str] = {}

    for row_number, row in enumerate(rows.itertuples(index=False, name=None), start=1):
        name, before, unit, raw_price = (_clean_cell(value) for value in row)
        if name is None or not str(name).strip():
            skipped += 1
            continue

        name = str(name).strip()
        try:
            before_value = _parse_before(before, row_number)
            price = _parse_price(raw_price, row_number)
        except ValueError as error:
            errors.append(str(error))
            skipped += 1
            continue

        unit_value = str(unit) if unit is not None else None
        prepared_rows.append((name, before_value, unit_value, price))

    with connect(database_path) as connection:
        for name, before_value, unit_value, price in prepared_rows:
            existing = connection.execute(
                """
                SELECT ID, Instruction, Photo, Manufacturer
                FROM Medicines
                WHERE trim(Name) = trim(?) COLLATE UNICODE_NOCASE
                ORDER BY ID LIMIT 1
                """,
                (name,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO Medicines
                        (Name, Unit, "Before", Price)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, unit_value, before_value, price),
                )
                added += 1
                pending_names.setdefault(name.casefold(), name)
            else:
                connection.execute(
                    """
                    UPDATE Medicines
                    SET Unit = ?, "Before" = ?, Price = ?
                    WHERE ID = ?
                    """,
                    (unit_value, before_value, price, existing["ID"]),
                )
                updated += 1
                if any(
                    not existing[field] or not str(existing[field]).strip()
                    for field in ("Instruction", "Photo", "Manufacturer")
                ):
                    pending_names.setdefault(name.casefold(), name)

    return ImportResult(
        added=added,
        updated=updated,
        skipped=skipped,
        errors=tuple(errors),
        pending_enrichment=tuple(pending_names.values()),
    )


def medicine_names_missing_info(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> tuple[str, ...]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT Name
            FROM Medicines
            WHERE Name IS NOT NULL AND trim(Name) != ''
              AND (
                    Instruction IS NULL OR trim(Instruction) = ''
                 OR Photo IS NULL OR trim(Photo) = ''
                 OR Manufacturer IS NULL OR trim(Manufacturer) = ''
              )
            ORDER BY ID
            """
        ).fetchall()

    unique_names: dict[str, str] = {}
    for row in rows:
        name = str(row["Name"]).strip()
        unique_names.setdefault(name.casefold(), name)
    return tuple(unique_names.values())


def enrich_medicine_info(
    medicine_names: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    info_lookup: Callable[[str], MedicineInfo] = lookup_medicine_info,
) -> EnrichmentResult:
    instructions = 0
    photos = 0
    manufacturers = 0
    no_results = 0
    errors = 0
    unique_names = {name.casefold(): name for name in medicine_names}
    if info_lookup is lookup_medicine_info and not os.getenv("BRAVE_SEARCH_API_KEY"):
        raise RuntimeError("Не задан BRAVE_SEARCH_API_KEY.")

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_names = {
            executor.submit(info_lookup, name): name
            for name in unique_names.values()
        }
        with connect(database_path) as connection:
            for future in as_completed(future_names):
                name = future_names[future]
                try:
                    info = future.result()
                except Exception:
                    LOGGER.exception("Ошибка поиска информации о препарате %r", name)
                    errors += 1
                    continue

                errors += info.errors
                if not any((info.instruction, info.photo, info.manufacturer)):
                    no_results += 1
                    continue

                connection.execute(
                    """
                    UPDATE Medicines
                    SET Instruction = COALESCE(NULLIF(trim(Instruction), ''), ?),
                        Photo = COALESCE(NULLIF(trim(Photo), ''), ?),
                        Manufacturer = COALESCE(NULLIF(trim(Manufacturer), ''), ?)
                    WHERE trim(Name) = trim(?) COLLATE UNICODE_NOCASE
                    """,
                    (info.instruction, info.photo, info.manufacturer, name),
                )
                instructions += bool(info.instruction)
                photos += bool(info.photo)
                manufacturers += bool(info.manufacturer)

    return EnrichmentResult(
        instructions=instructions,
        photos=photos,
        manufacturers=manufacturers,
        no_results=no_results,
        errors=errors,
    )