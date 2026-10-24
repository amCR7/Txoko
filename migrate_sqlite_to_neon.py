#!/usr/bin/env python3
"""Copy the local Txoko SQLite database into an empty Neon PostgreSQL database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from server import TxokoStore


TABLES = (
    "users",
    "sessions",
    "products",
    "inventory_events",
    "guests",
    "transactions",
    "expenses",
    "login_attempts",
)


def migrate(source_path: Path) -> None:
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("Define DATABASE_URL con la URL de Neon antes de ejecutar la migración.")
    if not source_path.is_file():
        raise SystemExit(f"No existe la base SQLite: {source_path}")

    store = TxokoStore()
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute("BEGIN")
        source_tables = {
            row["name"]
            for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing_tables = set(TABLES) - source_tables
        if missing_tables - {"expenses", "inventory_events"}:
            raise RuntimeError(f"La base SQLite no contiene estas tablas: {', '.join(sorted(missing_tables))}")
        counts = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in source_tables else 0
            for table in TABLES
        }
        with store.connect() as destination:
            destination.execute("BEGIN")
            destination_counts = {
                table: destination.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                for table in TABLES
            }
            if any(destination_counts.values()):
                raise SystemExit(
                    "Neon ya contiene registros. La migración se detuvo sin modificar datos."
                )

            for table in TABLES:
                if table not in source_tables:
                    continue
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    continue
                columns = list(rows[0].keys())
                names = ", ".join(columns)
                placeholders = ", ".join("?" for _ in columns)
                statement = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"
                for row in rows:
                    destination.execute(statement, tuple(row[column] for column in columns))
            destination.commit()

        with store.connect() as destination:
            migrated_counts = {
                table: destination.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                for table in TABLES
            }
        if counts != migrated_counts:
            raise RuntimeError(f"La comprobación posterior no coincide: origen={counts}, destino={migrated_counts}")
        print("Migración completada y verificada:")
        for table in TABLES:
            print(f"  {table}: {counts[table]} filas")
        print("La base SQLite original no se ha modificado.")
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_database", type=Path, help="Ruta al archivo txoko.sqlite3")
    args = parser.parse_args()
    try:
        migrate(args.sqlite_database)
    except Exception as error:
        print(f"Error al migrar la base: {error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
