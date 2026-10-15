"""Проверки разбора ответа Marlin M114 без открытия USB-порта."""

import termios  # Константы скоростей Linux, используемые драйвером Printer.
import unittest  # Стандартный тестовый раннер, не требующий pytest.
from unittest.mock import Mock

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

    def test_baudrate_is_validated_and_converted_for_termios(self):
        """Аргумент baudrate действительно влияет на настройку USB-порта."""
        self.assertEqual(Printer._baudrate_constant(115200), termios.B115200)
        with self.assertRaisesRegex(ValueError, "Неподдерживаемая скорость"):
            Printer._baudrate_constant(12345)

    def test_scanner_packet_contains_field_progress_and_sensor_state(self):
        """Одного M790 достаточно для обновления всех виджетов принтера."""
        command = Printer._scanner_data_command(
            bx=-12.345, by=6.789, bz=100.0, magnitude=101.25,
            point=5, total=20,
        )
        self.assertEqual(
            command,
            "M790 X-12.35 Y6.79 Z100.00 B101.25 P25 N5 T20 S1",
        )
        self.assertLess(len(command), 96)

    def test_measurement_session_and_report_commands(self):
        """LCD получает отдельные события начала и завершения для отчёта."""
        printer = Printer.__new__(Printer)
        printer.send = Mock()

        printer.begin_measurement(25)
        printer.signal_complete()

        self.assertEqual(printer.send.call_args_list[0].args[0], "M790 R P0 N0 T25 S1")
        self.assertEqual(printer.send.call_args_list[1].args[0], "M790 P100 S1 C1")


if __name__ == "__main__":
    unittest.main()
