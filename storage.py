"""Локальный SQLite-архив завершённых сканирований."""

from __future__ import annotations  # Позволяет писать современные аннотации типов.

# sqlite3 встроен в Python и не требует отдельной установки сервера БД.
import sqlite3
import threading
# datetime ставит фактическое время завершения скана в таблицу scans.
from datetime import datetime
# Path определяет путь к БД рядом с исходным кодом независимо от текущей папки.
from pathlib import Path


# Один файл БД хранит метаданные сканов и все их точки для страницы «Архив».
DATABASE_PATH = Path(__file__).with_name("scanner_results.db")
_INITIALIZED_DATABASES: set[Path] = set()
_INITIALIZE_LOCK = threading.Lock()


def _connection() -> sqlite3.Connection:
    """Открывает короткое соединение и создаёт таблицы при первом запуске."""
    database_path = Path(DATABASE_PATH).resolve()
    database_exists = database_path.exists()
    connection = sqlite3.connect(database_path, timeout=5)
    # Row позволяет обращаться к столбцу по имени: row["point_no"].
    connection.row_factory = sqlite3.Row
    # Включаем связь points.scan_id → scans.id и каскадное удаление.
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")

    # Схема и миграции раньше проверялись при каждом чтении архива. Теперь это
    # выполняется один раз для каждого файла БД за запуск приложения.
    with _INITIALIZE_LOCK:
        needs_initialization = not database_exists or database_path not in _INITIALIZED_DATABASES
        if not needs_initialization:
            return connection
        # WAL уменьшает блокировки, когда GUI читает архив после завершения скана.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
        -- scans: одна строка на завершённый проход сканера. Размеры и шаг
        -- сохраняются отдельно, чтобы архив можно было читать без CSV.
        CREATE TABLE IF NOT EXISTS scans (
            -- SQLite сам выдаёт целочисленный идентификатор каждого скана.
            id INTEGER PRIMARY KEY,
            -- Время, имя CSV и описание параметров одного прохода.
            created_at TEXT NOT NULL,
            filename TEXT NOT NULL,
            shape TEXT NOT NULL,
            -- mode отличает площадную «змейку» от контура и линии. Это
            -- необходимо, чтобы визуализация не строила ложную тепловую карту
            -- из одномерного измерения.
            mode TEXT NOT NULL DEFAULT 'snake',
            size_x REAL NOT NULL,
            size_y REAL NOT NULL,
            height REAL NOT NULL,
            step REAL NOT NULL,
            point_count INTEGER NOT NULL
        );
        -- points: измеренные точки. scan_id связывает их с родительским сканом.
        CREATE TABLE IF NOT EXISTS points (
            -- ON DELETE CASCADE удалит точки, если когда-нибудь будет удалён скан.
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            -- point_no определяет исходный порядок движения по траектории.
            point_no INTEGER NOT NULL,
            -- x/y — координаты датчика относительно центра; printer_x/y/z —
            -- фактические координаты G-code; bx/by/bz/b — измеренный вектор.
            x REAL NOT NULL, y REAL NOT NULL,
            printer_x REAL NOT NULL, printer_y REAL NOT NULL, z REAL NOT NULL,
            bx REAL NOT NULL, by REAL NOT NULL, bz REAL NOT NULL, b REAL NOT NULL,
            PRIMARY KEY (scan_id, point_no)
        );
            """
        )
        # Базы, созданные ранними версиями приложения, ещё не имели mode.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(scans)")}
        if "mode" not in columns:
            connection.execute("ALTER TABLE scans ADD COLUMN mode TEXT NOT NULL DEFAULT 'snake'")
        _INITIALIZED_DATABASES.add(database_path)
    return connection


def save_scan(filename: str, parameters: dict, results: list[dict]) -> int:
    """Сохраняет метаданные скана и все точки одной транзакцией SQLite."""
    with _connection() as connection:
        # Сначала создаём «шапку» скана и запоминаем её первичный ключ.
        cursor = connection.execute(
            """INSERT INTO scans(created_at, filename, shape, mode, size_x, size_y, height, step, point_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(timespec="seconds"), filename, parameters["shape"],
             parameters.get("mode", "snake"), parameters["size_x"], parameters["size_y"],
             parameters["height"], parameters["step"], len(results)),
        )
        scan_id = cursor.lastrowid  # Этот id станет внешним ключом всех точек.
        # executemany быстрее вставляет большой список точек, чем отдельный
        # INSERT в цикле, и остаётся в той же транзакции with-блока.
        connection.executemany(
            """INSERT INTO points(scan_id, point_no, x, y, printer_x, printer_y, z, bx, by, bz, b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(scan_id, index, item["x"], item["y"], item["printer_x"], item["printer_y"],
              item["z"], item["bx"], item["by"], item["bz"], item["b"])
             # enumerate(start=1) делает номер точки привычным для человека.
             for index, item in enumerate(results, start=1)],
        )
    return scan_id


def list_scans() -> list[sqlite3.Row]:
    """Возвращает краткий список сканов, сначала самые новые."""
    with _connection() as connection:
        return connection.execute(
            "SELECT id, created_at, filename, shape, mode, size_x, size_y, height, step, point_count "
            "FROM scans ORDER BY id DESC"
        ).fetchall()


def get_points(scan_id: int) -> list[sqlite3.Row]:
    """Возвращает все точки одного скана для таблицы предпросмотра."""
    with _connection() as connection:
        return connection.execute(
            "SELECT point_no, x, y, printer_x, printer_y, z, bx, by, bz, b "
            "FROM points WHERE scan_id = ? ORDER BY point_no", (scan_id,)
        ).fetchall()


def delete_scan(scan_id: int) -> str | None:
    """Удаляет скан и его точки; возвращает имя CSV для зачистки файла.

    FOREIGN KEY points.scan_id настроен с ON DELETE CASCADE, поэтому одной
    команды DELETE достаточно: точки родительского скана исчезнут сами.
    Возвращает имя файла, которое хранил удалённый скан, чтобы вызывающий
    код мог аккуратно удалить и сам CSV. Для несуществующего id вернёт None.
    """
    with _connection() as connection:
        row = connection.execute(
            "SELECT filename FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
        return row["filename"]
