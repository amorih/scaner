"""Работа с трёхосевым магнитометром MLX90393 по I²C."""

# math используется для модуля вектора |B| = √(Bx² + By² + Bz²).
import math
# time задаёт короткие интервалы между чтениями датчика.
import time
import logging

# ExtendedI2C — библиотека Adafruit для открытия указанной I²C-шины Linux.
from adafruit_extended_bus import ExtendedI2C
# Драйвер конкретного чипа MLX90393; `.magnetic` отдаёт Bx, By, Bz в мТл.
import adafruit_mlx90393

# Настройки вынесены в config.py, чтобы не искать номера шины и адрес в коде.
import config
from config import (
    I2C_BUS,
    MLX90393_ADDRESS,
    SAMPLES_PER_POINT,
    SENSOR_RECONNECT_TIMEOUT,
)


logger = logging.getLogger(__name__)


class MagneticSensor:
    """Датчик с усреднением измерений и вычитанием фонового поля."""

    def __init__(self):
        """Открывает I²C и делает одно чтение для проверки связи с микросхемой."""
        logger.info("Подключение к MLX90393")
        self.i2c = ExtendedI2C(I2C_BUS)
        try:
            gain = getattr(config, "SENSOR_GAIN", getattr(adafruit_mlx90393, "GAIN_5X", 0))
            res = getattr(config, "SENSOR_RESOLUTION", getattr(adafruit_mlx90393, "RESOLUTION_16", 0))
            self.sensor = adafruit_mlx90393.MLX90393(
                self.i2c,
                address=MLX90393_ADDRESS,
                gain=gain,
                resolution=res,
            )
            self.sensor.magnetic  # Если адрес/провода неверны, ошибка появится здесь.
        except Exception:
            if hasattr(self.i2c, "deinit"):
                try:
                    self.i2c.deinit()
                except Exception:
                    pass
            self.i2c = None
            raise
        # До первой калибровки фон считается нулевым, чтобы атрибуты существовали.
        self.zero_bx = self.zero_by = self.zero_bz = 0.0
        logger.info("MLX90393 подключён (gain=%s, res=%s)", getattr(self.sensor, "gain", "?"), getattr(self.sensor, "resolution_x", "?"))

    @staticmethod
    def _vector(bx, by, bz):
        """Возвращает единый формат данных и модуль магнитного вектора."""
        return {"bx": bx, "by": by, "bz": bz, "b": math.hypot(bx, by, bz)}

    @property
    def raw_data(self):
        """Сырые отсчёты АЦП (LSB) по осям X, Y, Z."""
        if hasattr(self.sensor, "read_data"):
            return self.sensor.read_data
        return (0, 0, 0)

    @property
    def temperature(self):
        """Температура кристалла MLX90393 в градусах Цельсия."""
        if hasattr(self.sensor, "temperature"):
            try:
                return float(self.sensor.temperature)
            except Exception:
                pass
        return 25.0

    @staticmethod
    def estimate_surface_field(measured_b, distance_z_mm, radius_mm=10.0, length_mm=8.0):
        """Оценивает индукцию на поверхности дискового/цилиндрического магнита B(0)
        по измеренному полю B(z) на высоте z (мм) согласно аналитической формуле
        аксиального поля цилиндрического постоянного магнита:
        B(z) = (Br / 2) * [ (z + L)/sqrt((z+L)^2 + R^2) - z/sqrt(z^2 + R^2) ]
        """
        if distance_z_mm <= 0:
            return measured_b
        r = float(radius_mm)
        l = float(length_mm)
        z = float(distance_z_mm)
        denom = (z + l) / math.sqrt((z + l) ** 2 + r ** 2) - z / math.sqrt(z ** 2 + r ** 2)
        if denom <= 1e-9:
            return measured_b
        surf_factor = l / math.sqrt(l ** 2 + r ** 2)
        return measured_b * (surf_factor / denom)

    def read(self):
        """Читает одно сырое значение чипа, переживая кратковременные сбои I²C.

        MLX90393 иногда пропадает на доли секунды: моторы принтера создают
        помехи, дребезжит контакт или проседает питание, и Linux отдаёт
        `Remote I/O error`. Вместо мгновенного падения скана метод повторяет
        чтение с нарастающей паузой до SENSOR_RECONNECT_TIMEOUT. Как только чип
        снова ответил, значение возвращается и измерение точки продолжается с
        того же места. Если за отведённое время связь не вернулась, поднимается
        последняя ошибка OSError, и скан останавливается.
        """
        deadline = time.monotonic() + SENSOR_RECONNECT_TIMEOUT
        delay = 0.05
        was_offline = False
        while True:
            try:
                bx, by, bz = self.sensor.magnetic
                if was_offline:
                    logger.info("Датчик восстановился, измерение продолжается")

                # Проверка флага насыщения (бит 4 в статусном байте чипа):
                status = getattr(self.sensor, "_status_last", 0)
                if status & 0x10:
                    current_gain = getattr(self.sensor, "gain", None)
                    if current_gain is not None and current_gain > 0 and getattr(config, "SENSOR_AUTO_GAIN", True):
                        logger.warning("MLX90393 насыщен при gain=%s, переключение на широкий диапазон GAIN_5X (0)", current_gain)
                        self.sensor.gain = 0
                        time.sleep(0.01)
                        continue
                    logger.warning("Поле превышает предел измерения MLX90393 (status=0x%02X)", status)

                # Чип MLX90393 возвращает мкТл (uT). Переводим в миллитеслы (мТл): 1 мТл = 1000 мкТл.
                return self._vector(bx / 1000.0, by / 1000.0, bz / 1000.0)
            except OSError as error:
                was_offline = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise  # Датчик не вернулся — скан остановится с этой ошибкой.
                logger.info(
                    "Датчик не отвечает (%s: %s); повтор через %.2f с (осталось %.1f с)",
                    type(error).__name__, error, delay, remaining,
                )
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 1.0)

    def measure(self, samples=None):
        """Усредняет несколько быстрых чтений для уменьшения случайного шума."""
        count = max(1, int(samples)) if samples is not None else getattr(config, "SAMPLES_PER_POINT", SAMPLES_PER_POINT)
        bx_sum = by_sum = bz_sum = 0.0
        for sample_index in range(count):
            value = self.read()
            bx_sum += value["bx"]
            by_sum += value["by"]
            bz_sum += value["bz"]
            if sample_index + 1 < count:
                time.sleep(0.003)
        return self._vector(
            bx_sum / count,
            by_sum / count,
            bz_sum / count,
        )

    def zero(self):
        """Запоминает окружающее поле как фон, который вычитается при скане."""
        logger.info("Калибровка датчика")
        value = self.measure()
        self.zero_bx = value["bx"]
        self.zero_by = value["by"]
        self.zero_bz = value["bz"]
        logger.info("Фоновое поле сохранено")

    def measure_relative(self, samples=None):
        """Возвращает поле магнита относительно последнего сохранённого фона."""
        value = self.measure(samples=samples)
        return self._vector(
            value["bx"] - self.zero_bx,
            value["by"] - self.zero_by,
            value["bz"] - self.zero_bz,
        )

    def close(self):
        """Освобождает I²C-шину, если драйвер её поддерживает."""
        i2c = getattr(self, "i2c", None)
        self.i2c = None
        if i2c is not None and hasattr(i2c, "deinit"):
            i2c.deinit()
