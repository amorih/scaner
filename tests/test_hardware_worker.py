"""Проверки защитных состояний HardwareWorker без реального оборудования."""

import unittest  # Стандартный модуль Python для запуска изолированных проверок.

from desktop_app import HardwareWorker  # Тестируем worker без создания окна Qt.


class FakePrinter:
    """Минимальная заглушка serial-принтера для безопасных unit-тестов."""

    def __init__(self):
        self.stop_calls = 0
        self.recover_calls = 0

    def stop(self):
        """Запоминает отправку M112 вместо записи в USB-порт."""
        self.stop_calls += 1

    def recover_after_stop(self):
        """Запоминает отправку M999 вместо обращения к прошивке."""
        self.recover_calls += 1


class HardwareSafetyTests(unittest.TestCase):
    """Проверяет логику M112/M999 и флаг калибровки принтера."""

    def test_emergency_stop_invalidates_homing_and_waits_for_recovery(self):
        """После M112 карта не может считать старые координаты достоверными."""
        worker = HardwareWorker()
        printer = FakePrinter()
        worker.printer = printer
        worker.printer_homed = True

        worker.emergency_stop_now()

        self.assertEqual(printer.stop_calls, 1)
        self.assertFalse(worker.printer_homed)
        self.assertTrue(worker.awaiting_emergency_recovery)

    def test_recovery_happens_once_after_emergency_stop(self):
        """Повторный сигнал восстановления не создаёт лишних M999-команд."""
        worker = HardwareWorker()
        printer = FakePrinter()
        worker.printer = printer
        worker.emergency_stop_now()

        worker.recover_after_emergency_stop()
        worker.recover_after_emergency_stop()

        self.assertEqual(printer.recover_calls, 1)
        self.assertFalse(worker.awaiting_emergency_recovery)


if __name__ == "__main__":
    unittest.main()
