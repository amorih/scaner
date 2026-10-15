"""Проверки фонового worker без настоящего принтера и датчика."""

import unittest

from desktop_app import HardwareWorker


class FakePrinter:
    def __init__(self):
        self.cancel_count = 0
        self.moves = []

    def cancel_motion(self):
        self.cancel_count += 1

    def move(self, **coordinates):
        self.moves.append(coordinates)

    def send(self, _command):
        pass


class HardwareWorkerTests(unittest.TestCase):
    def test_cancel_is_visible_without_processing_qt_event_queue(self):
        """Отмена должна сработать, пока worker занят длинным scan()."""
        worker = HardwareWorker()
        worker.printer = FakePrinter()

        worker.request_cancel(report=False)

        self.assertTrue(worker._cancel_event.is_set())
        self.assertEqual(worker.printer.cancel_count, 1)

    def test_skipped_sensor_read_always_releases_poll_guard(self):
        """Отсутствующий датчик не должен навсегда блокировать живой опрос."""
        worker = HardwareWorker()
        completed = []
        worker.read_done.connect(lambda: completed.append(True))

        worker.read_sensor()

        self.assertEqual(completed, [True])

    def test_manual_move_rejects_height_above_machine_limit(self):
        """GUI-команда не может отправить ось Z выше физического предела."""
        worker = HardwareWorker()
        worker.printer = FakePrinter()
        worker.printer_homed = True

        worker.manual_move({"x": 100, "y": 100, "z": 999})

        self.assertEqual(worker.printer.moves, [])


if __name__ == "__main__":
    unittest.main()
