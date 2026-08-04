"""Работа с трёхосевым магнитометром MLX90393 по I²C."""

# math используется для модуля вектора |B| = √(Bx² + By² + Bz²).
import math
# time задаёт короткие интервалы между чтениями датчика.
import time

# ExtendedI2C — библиотека Adafruit для открытия указанной I²C-шины Linux.
from adafruit_extended_bus import ExtendedI2C
# Драйвер конкретного чипа MLX90393; `.magnetic` отдаёт Bx, By, Bz в µT.
import adafruit_mlx90393

# Настройки вынесены в config.py, чтобы не искать номера шины и адрес в коде.
from config import I2C_BUS, MLX90393_ADDRESS, SAMPLES_PER_POINT


class MagneticSensor:
    """Датчик с усреднением измерений и вычитанием фонового поля."""

    def __init__(self):
        """Открывает I²C и делает одно чтение для проверки связи с микросхемой."""
        print("Подключение к MLX90393…")
        self.i2c = ExtendedI2C(I2C_BUS)
        self.sensor = adafruit_mlx90393.MLX90393(self.i2c, address=MLX90393_ADDRESS)
        self.sensor.magnetic  # Если адрес/провода неверны, ошибка появится здесь.
        # До первой калибровки фон считается нулевым, чтобы атрибуты существовали.
        self.zero_bx = self.zero_by = self.zero_bz = 0.0
        print("MLX90393 подключён.")

    @staticmethod
    def _vector(bx, by, bz):
        """Возвращает единый формат данных и модуль магнитного вектора."""
        return {"bx": bx, "by": by, "bz": bz, "b": math.sqrt(bx * bx + by * by + bz * bz)}

    def read(self):
        """Читает одно сырое значение чипа без фильтрации и калибровки."""
        bx, by, bz = self.sensor.magnetic
        return self._vector(bx, by, bz)

    def measure(self):
        """Усредняет несколько быстрых чтений для уменьшения случайного шума."""
        bx_sum = by_sum = bz_sum = 0.0
        for _ in range(SAMPLES_PER_POINT):
            value = self.read()
            bx_sum += value["bx"]
            by_sum += value["by"]
            bz_sum += value["bz"]
            time.sleep(0.003)
        return self._vector(
            bx_sum / SAMPLES_PER_POINT,
            by_sum / SAMPLES_PER_POINT,
            bz_sum / SAMPLES_PER_POINT,
        )

    def zero(self):
        """Запоминает окружающее поле как фон, который вычитается при скане."""
        print("Калибровка…")
        value = self.measure()
        self.zero_bx = value["bx"]
        self.zero_by = value["by"]
        self.zero_bz = value["bz"]
        print("Фоновое поле сохранено.")

    def measure_relative(self):
        """Возвращает поле магнита относительно последнего сохранённого фона."""
        value = self.measure()
        return self._vector(
            value["bx"] - self.zero_bx,
            value["by"] - self.zero_by,
            value["bz"] - self.zero_bz,
        )
