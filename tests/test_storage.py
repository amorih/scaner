"""Проверки SQLite-архива без обращения к рабочему файлу пользователя."""

# tempfile создаёт временную изолированную папку для каждой проверки.
import tempfile
# Path удобно передать во временный DATABASE_PATH модуля storage.
from pathlib import Path
# unittest запускает тест штатной командой Python.
import unittest

# Импортируем модуль целиком, чтобы на время теста подменить путь к БД.
import storage


class StorageTests(unittest.TestCase):
    """Проверяет метаданные, нужные для честного выбора тепловой карты."""

    def _save_sample(self, filename="scan.csv"):
        """Сохраняет один тестовый скан и возвращает его id."""
        return storage.save_scan(
            filename,
            {"shape": "rectangle", "mode": "snake", "size_x": 20,
             "size_y": 20, "height": 5, "step": 2},
            [{"x": 0, "y": 0, "printer_x": 96, "printer_y": 110,
              "z": 10, "bx": 1, "by": 2, "bz": 3, "b": 3.74},
             {"x": 10, "y": 0, "printer_x": 116, "printer_y": 110,
              "z": 10, "bx": 4, "by": 5, "bz": 6, "b": 8.77}],
        )

    def _temp_database(self):
        """Контекст временной БД: меняет путь и всегда возвращает его назад."""
        original_path = storage.DATABASE_PATH
        directory = tempfile.TemporaryDirectory()
        storage.DATABASE_PATH = Path(directory.name) / "test_results.db"
        return original_path, directory

    def test_archive_preserves_scan_mode(self):
        """Контур цилиндра не должен потерять mode='circle' в SQLite."""
        original_path, directory = self._temp_database()
        try:
            scan_id = storage.save_scan(
                "contour.csv",
                {"shape": "cylinder", "mode": "circle", "size_x": 20,
                 "size_y": 20, "height": 5, "step": 2},
                [{"x": 0, "y": 10, "printer_x": 96, "printer_y": 140,
                  "z": 10, "bx": 1, "by": 2, "bz": 3, "b": 3.74}],
            )
            scan = storage.list_scans()[0]
            self.assertEqual(scan["id"], scan_id)
            self.assertEqual(scan["mode"], "circle")
        finally:
            storage.DATABASE_PATH = original_path
            directory.cleanup()

    def test_delete_scan_removes_rows_and_points(self):
        """Каскад удаляет и сам скан, и все его точки из архива."""
        original_path, directory = self._temp_database()
        try:
            scan_id = self._save_sample("delete-me.csv")
            filename = storage.delete_scan(scan_id)
            self.assertEqual(filename, "delete-me.csv")
            # После удаления скан и его точки исчезли из обоих запросов.
            self.assertEqual(storage.list_scans(), [])
            self.assertEqual(storage.get_points(scan_id), [])
        finally:
            storage.DATABASE_PATH = original_path
            directory.cleanup()

    def test_delete_scan_keeps_other_scans(self):
        """Удаление одного скана не трогает соседние записи архива."""
        original_path, directory = self._temp_database()
        try:
            first_id = self._save_sample("first.csv")
            second_id = self._save_sample("second.csv")
            storage.delete_scan(first_id)
            remaining = storage.list_scans()
            self.assertEqual([row["id"] for row in remaining], [second_id])
        finally:
            storage.DATABASE_PATH = original_path
            directory.cleanup()

    def test_delete_missing_scan_returns_none(self):
        """Несуществующий id не считается ошибкой и возвращает None."""
        original_path, directory = self._temp_database()
        try:
            self.assertIsNone(storage.delete_scan(999_999))
        finally:
            storage.DATABASE_PATH = original_path
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
