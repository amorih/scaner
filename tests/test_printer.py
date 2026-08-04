"""Проверки разбора ответа Marlin M114 без открытия USB-порта."""

import unittest  # Стандартный тестовый раннер, не требующий pytest.

from printer import Printer  # Нужен только статический разбор строки M114.


class PrinterProtocolTests(unittest.TestCase):
    """Проверяет, что нижняя строка интерфейса получит реальные XYZ."""

    def test_parse_marlin_position(self):
        """Стандартный формат Marlin преобразуется в числовой словарь."""
        self.assertEqual(
            Printer._parse_position("X:12.30 Y:45.60 Z:7.00 E:0.00 Count X:0 Y:0 Z:0"),
            {"x": 12.3, "y": 45.6, "z": 7.0},
        )

    def test_parse_marlin_position_accepts_negative_values(self):
        """Относительные и парковочные координаты тоже разбираются корректно."""
        self.assertEqual(
            Printer._parse_position("echo: X:-1.25 Y:0.00 Z:10.50"),
            {"x": -1.25, "y": 0.0, "z": 10.5},
        )

    def test_parse_position_ignores_unrelated_messages(self):
        """Обычные ответы прошивки не могут случайно подменить координаты."""
        self.assertIsNone(Printer._parse_position("echo:busy: processing"))


if __name__ == "__main__":
    unittest.main()
