"""Проверки восстановления чтения MLX90393 после кратковременного сбоя I²C.

Тесты не трогают реальную шину: вместо микросхемы используется заглушка,
которая поднимает OSError (Remote I/O error) заданное число раз, а затем
возвращает показания. Это проверяет поведение MagneticSensor.read(): ожидание
восстановления до SENSOR_RECONNECT_TIMEOUT и повторное чтение.
"""

import unittest  # Стандартный модуль Python для запуска изолированных проверок.
from unittest import mock  # mock подменяет таймаут, чтобы тест не ждал 5 секунд.

import sensor as sensor_module  # Модуль целиком нужен для подмены константы.
from sensor import MagneticSensor  # Тестируем метод восстановления чтения.


class FakeChip:
    """Эмуляция MLX90393: поднимает OSError несколько раз, затем отвечает."""

    def __init__(self, failures=0, magnetic=(1000.0, 2000.0, 3000.0)):
        self.failures = failures
        self.magnetic_value = magnetic

    @property
    def magnetic(self):
        """Свойство драйвера: сбой столько раз, сколько задано в failures."""
        if self.failures > 0:
            self.failures -= 1
            raise OSError("Remote I/O error")
        return self.magnetic_value


class SensorRecoveryTests(unittest.TestCase):
    """Проверяет паузу и продолжение измерения при кратковременном сбое."""

    @staticmethod
    def _sensor_with(chip):
        """Создаёт объект в обход __init__, чтобы не открывать реальную шину."""
        instance = MagneticSensor.__new__(MagneticSensor)
        instance.sensor = chip
        instance.zero_bx = instance.zero_by = instance.zero_bz = 0.0
        return instance

    def test_read_waits_and_recovers_after_transient_failure(self):
        """Одиночный сбой не роняет чтение: после повтора возвращается значение."""
        chip = FakeChip(failures=2)
        instance = self._sensor_with(chip)

        value = instance.read()

        self.assertEqual(chip.failures, 0)
        self.assertEqual(value["bx"], 1.0)
        self.assertEqual(value["by"], 2.0)
        self.assertEqual(value["bz"], 3.0)

    def test_read_raises_when_sensor_stays_offline_beyond_timeout(self):
        """Если датчик не вернулся за таймаут, чтение поднимает OSError."""
        chip = FakeChip(failures=9999)
        instance = self._sensor_with(chip)

        # Подменяем таймаут на 0.2 с, чтобы тест не ждал реальные 5 секунд.
        with mock.patch.object(sensor_module, "SENSOR_RECONNECT_TIMEOUT", 0.2):
            with self.assertRaises(OSError):
                instance.read()

    def test_estimate_surface_field(self):
        """Проверяет аналитический пересчёт поля на поверхности постоянного магнита."""
        # На нулевой высоте поверхностное поле равно измеренному
        self.assertEqual(MagneticSensor.estimate_surface_field(100.0, 0.0), 100.0)

        # Для магнита R=10 мм, L=8 мм поле на z=5 мм составляет ~55% от поверхностного:
        # B(z) ~ 55 мТл -> B(0) ~ 100 мТл
        est = MagneticSensor.estimate_surface_field(55.29, 5.0, radius_mm=10.0, length_mm=8.0)
        self.assertAlmostEqual(est, 100.0, places=1)

    def test_raw_data_and_temperature_properties(self):
        """Проверяет чтение LSB и температуры."""
        chip = FakeChip()
        chip.read_data = (100, 200, 300)
        chip.temperature = 24.5
        instance = self._sensor_with(chip)

        self.assertEqual(instance.raw_data, (100, 200, 300))
        self.assertAlmostEqual(instance.temperature, 24.5)


if __name__ == "__main__":
    unittest.main()
