import sqlite3
from pathlib import Path

from src.databases.database import DEFAULT_DATABASE_PATH, connect


class DatabaseOptimizationError(RuntimeError):
    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count
        super().__init__(
            f"Удалено препаратов: {deleted_count}, но базу данных не удалось оптимизировать."
        )


def delete_all_medicines(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute("DELETE FROM Medicines")
        deleted_count = cursor.rowcount

    try:
        with connect(database_path) as connection:
            connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise DatabaseOptimizationError(deleted_count) from error

    return deleted_count
