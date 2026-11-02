import sqlite3
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from src.databases.database import DEFAULT_DATABASE_PATH, connect


@dataclass(frozen=True)
class MedicineRecord:
    id: int
    name: str
    unit: str | None
    before: str | None
    instruction: str | None
    photo: str | None
    price: float | None
    manufacturer: str | None
    remainder: float | None
    salesman: str | None


def _medicine_record(row: sqlite3.Row) -> MedicineRecord:
    return MedicineRecord(
        id=row["ID"],
        name=row["Name"],
        unit=row["Unit"],
        before=row["Before"],
        instruction=row["Instruction"],
        photo=row["Photo"],
        price=row["Price"],
        manufacturer=row["Manufacturer"],
        remainder=row["Remainder"],
        salesman=row["Salesman"],
    )


def search_medicines_by_prefix(
    prefix: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    limit: int = 20,
) -> tuple[MedicineRecord, ...]:
    normalized_prefix = prefix.strip()
    if not normalized_prefix:
        return ()

    with connect(database_path) as connection:
        connection.create_function("UNICODE_CASEFOLD", 1, str.casefold)
        rows = connection.execute(
            """
            SELECT ID, Name, Unit, "Before", Instruction, Photo, Price,
                   Manufacturer, Remainder, Salesman
            FROM Medicines
            WHERE Name IS NOT NULL AND trim(Name) != ''
              AND substr(UNICODE_CASEFOLD(trim(Name)), 1, length(UNICODE_CASEFOLD(?)))
                  = UNICODE_CASEFOLD(?)
            ORDER BY Name COLLATE UNICODE_NOCASE, ID
            LIMIT ?
            """,
            (normalized_prefix, normalized_prefix, limit),
        ).fetchall()
    return tuple(_medicine_record(row) for row in rows)


def get_medicine(
    medicine_id: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> MedicineRecord | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT ID, Name, Unit, "Before", Instruction, Photo, Price,
                   Manufacturer, Remainder, Salesman
            FROM Medicines
            WHERE ID = ?
            """,
            (medicine_id,),
        ).fetchone()
    if row is None or row["Name"] is None:
        return None
    return _medicine_record(row)


def update_medicine_remainder(
    medicine_id: int,
    remainder: float,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> str:
    if not isfinite(remainder) or remainder < 0:
        raise ValueError("Остаток должен быть неотрицательным конечным числом.")

    with connect(database_path) as connection:
        medicine = connection.execute(
            "SELECT Name FROM Medicines WHERE ID = ?",
            (medicine_id,),
        ).fetchone()
        if medicine is None or medicine["Name"] is None:
            raise LookupError(f"Препарат с ID {medicine_id} не найден.")
        connection.execute(
            "UPDATE Medicines SET Remainder = ? WHERE ID = ?",
            (remainder, medicine_id),
        )
    return medicine["Name"]
