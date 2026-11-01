from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from src.databases.database import DEFAULT_DATABASE_PATH, connect
from src.databases.steps.step_one import SUPPORTED_EXTENSIONS


SALESMAN_NAME = "Vetco"


@dataclass(frozen=True)
class RemainderImportResult:
    changed: int
    not_found: tuple[str, ...]
    errors: tuple[str, ...]


def _parse_remainder(value: object, row_number: int) -> float:
    if isinstance(value, bool):
        raise ValueError(f"строка {row_number}: остаток должен быть целым числом")

    normalized = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        quantity = Decimal(normalized)
        if not quantity.is_finite() or quantity != quantity.quantize(Decimal("0.01")):
            raise ValueError(
                f"строка {row_number}: остаток {value!r} должен содержать не более "
                "двух знаков после запятой"
            )
    except InvalidOperation as error:
        raise ValueError(
            f"строка {row_number}: некорректное значение остатка {value!r}"
        ) from error
    return float(quantity)


def import_remainders_workbook(
    workbook_path: str | Path,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> RemainderImportResult:
    path = Path(workbook_path)
    engine = SUPPORTED_EXTENSIONS.get(path.suffix.lower())
    if engine is None:
        raise ValueError("Поддерживаются только файлы .xlsx, .xls и .ods.")

    try:
        rows = pd.read_excel(
            path,
            header=None,
            usecols=[0, 1],
            engine=engine,
            dtype=object,
        )
    except ImportError as error:
        raise RuntimeError(f"Не установлен обработчик формата {path.suffix}.") from error
    except ValueError as error:
        raise ValueError("В таблице должны присутствовать столбцы A и B без заголовков.") from error

    changed = 0
    not_found: dict[str, str] = {}
    errors: list[str] = []
    with connect(database_path) as connection:
        for row_number, (raw_name, raw_remainder) in enumerate(
            rows.itertuples(index=False, name=None),
            start=1,
        ):
            if raw_name is None or pd.isna(raw_name) or not str(raw_name).strip():
                continue
            name = str(raw_name).strip()

            medicine = connection.execute(
                """
                SELECT ID, Remainder
                FROM Medicines
                WHERE trim(Name) = trim(?) COLLATE UNICODE_NOCASE
                ORDER BY ID LIMIT 1
                """,
                (name,),
            ).fetchone()
            if medicine is None:
                not_found.setdefault(name.casefold(), name)
                continue

            if raw_remainder is None or pd.isna(raw_remainder):
                errors.append(f"строка {row_number}: не указано значение остатка для {name!r}")
                continue
            try:
                remainder = _parse_remainder(raw_remainder, row_number)
            except ValueError as error:
                errors.append(f"{error} ({name!r})")
                continue

            if medicine["Remainder"] == remainder:
                continue

            connection.execute(
                "UPDATE Medicines SET Remainder = ? WHERE ID = ?",
                (remainder, medicine["ID"]),
            )
            changed += 1

        connection.execute(
            "UPDATE Medicines SET Salesman = ? WHERE Salesman IS NULL OR Salesman != ?",
            (SALESMAN_NAME, SALESMAN_NAME),
        )

    return RemainderImportResult(
        changed=changed,
        not_found=tuple(not_found.values()),
        errors=tuple(errors),
    )
