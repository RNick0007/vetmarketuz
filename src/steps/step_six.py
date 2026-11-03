import sqlite3
from pathlib import Path

from src.databases.database import DEFAULT_DATABASE_PATH, connect


class DatabaseOptimizationError(RuntimeError):
    pass


def delete_medicine(
    medicine_id: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> str:
    with connect(database_path) as connection:
        medicine = connection.execute(
            "SELECT Name FROM Medicines WHERE ID = ?",
            (medicine_id,),
        ).fetchone()
        if medicine is None:
            raise LookupError(f"Препарат с ID {medicine_id} не найден.")

        connection.execute("DELETE FROM Medicines WHERE ID = ?", (medicine_id,))

    try:
        with connect(database_path) as connection:
            connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise DatabaseOptimizationError(
            f"Препарат {medicine['Name']} удален, но базу данных не удалось оптимизировать."
        ) from error

    return medicine["Name"]