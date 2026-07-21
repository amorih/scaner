"""Small local SQLite archive for completed scans."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


DATABASE_PATH = Path(__file__).with_name("scanner_results.db")


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            filename TEXT NOT NULL,
            shape TEXT NOT NULL,
            size_x REAL NOT NULL,
            size_y REAL NOT NULL,
            height REAL NOT NULL,
            step REAL NOT NULL,
            point_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS points (
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            point_no INTEGER NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL,
            printer_x REAL NOT NULL, printer_y REAL NOT NULL, z REAL NOT NULL,
            bx REAL NOT NULL, by REAL NOT NULL, bz REAL NOT NULL, b REAL NOT NULL,
            PRIMARY KEY (scan_id, point_no)
        );
        """
    )
    return connection


def save_scan(filename: str, parameters: dict, results: list[dict]) -> int:
    """Persist the scan index and every measured point in one transaction."""
    with _connection() as connection:
        cursor = connection.execute(
            """INSERT INTO scans(created_at, filename, shape, size_x, size_y, height, step, point_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(timespec="seconds"), filename, parameters["shape"],
             parameters["size_x"], parameters["size_y"], parameters["height"],
             parameters["step"], len(results)),
        )
        scan_id = cursor.lastrowid
        connection.executemany(
            """INSERT INTO points(scan_id, point_no, x, y, printer_x, printer_y, z, bx, by, bz, b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(scan_id, index, item["x"], item["y"], item["printer_x"], item["printer_y"],
              item["z"], item["bx"], item["by"], item["bz"], item["b"])
             for index, item in enumerate(results, start=1)],
        )
    return scan_id


def list_scans() -> list[sqlite3.Row]:
    with _connection() as connection:
        return connection.execute(
            "SELECT id, created_at, filename, shape, size_x, size_y, height, step, point_count "
            "FROM scans ORDER BY id DESC"
        ).fetchall()


def get_points(scan_id: int) -> list[sqlite3.Row]:
    with _connection() as connection:
        return connection.execute(
            "SELECT point_no, x, y, printer_x, printer_y, z, bx, by, bz, b "
            "FROM points WHERE scan_id = ? ORDER BY point_no", (scan_id,)
        ).fetchall()
