from contextlib import contextmanager
import re
import sqlite3
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "vetmarket.db"


def _unicode_nocase(left: str, right: str) -> int:
    folded_left = left.casefold()
    folded_right = right.casefold()
    return (folded_left > folded_right) - (folded_left < folded_right)


def _migrate_remainder_to_real(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'Medicines'"
    ).fetchone()
    if table is None:
        return

    columns = connection.execute("PRAGMA table_info(Medicines)").fetchall()
    remainder_column = next(
        (column for column in columns if column["name"].casefold() == "remainder"),
        None,
    )
    if remainder_column is None or remainder_column["type"].upper() == "REAL":
        return
    if remainder_column["type"].upper() != "INTEGER":
        raise RuntimeError(
            "Невозможно автоматически изменить тип Medicines.Remainder "
            f"из {remainder_column['type']!r} на REAL."
        )

    create_sql = table["sql"]
    create_sql, type_replacements = re.subn(
        r'((?:"Remainder"|`Remainder`|\[Remainder\]|Remainder)\s+)INTEGER\b',
        r"\1REAL",
        create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if type_replacements != 1:
        raise RuntimeError("Не удалось найти определение колонки Medicines.Remainder.")

    create_sql, table_replacements = re.subn(
        r'^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:"Medicines"|`Medicines`|\[Medicines\]|Medicines)(?=\s|\()',
        r'\1"Medicines__remainder_migration"',
        create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if table_replacements != 1:
        raise RuntimeError("Не удалось подготовить миграцию таблицы Medicines.")

    schema_objects = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE tbl_name = 'Medicines' AND type IN ('index', 'trigger') AND sql IS NOT NULL
        """
    ).fetchall()
    quoted_columns = ", ".join(
        '"' + column["name"].replace('"', '""') + '"' for column in columns
    )
    connection.execute("BEGIN")
    connection.execute(create_sql)
    connection.execute(
        f'INSERT INTO "Medicines__remainder_migration" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "Medicines"'
    )
    connection.execute('DROP TABLE "Medicines"')
    connection.execute(
        'ALTER TABLE "Medicines__remainder_migration" RENAME TO "Medicines"'
    )
    for schema_object in schema_objects:
        connection.execute(schema_object["sql"])


@contextmanager
def connect(database_path: str | Path = DEFAULT_DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.create_collation("UNICODE_NOCASE", _unicode_nocase)
        with connection:
            _migrate_remainder_to_real(connection)
            yield connection
    finally:
        connection.close()
