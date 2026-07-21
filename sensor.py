import math
import time

from adafruit_extended_bus import ExtendedI2C
import adafruit_mlx90393

from config import (
    I2C_BUS,
    MLX90393_ADDRESS,
    SAMPLES_PER_POINT
)


class MagneticSensor:

    def __init__(self):

        print("Подключение к MLX90393...")

        self.i2c = ExtendedI2C(I2C_BUS)

        self.sensor = adafruit_mlx90393.MLX90393(
            self.i2c,
            address=MLX90393_ADDRESS
        )

        # Проверка связи
        self.sensor.magnetic

        print("MLX90393 подключен.")

    # --------------------------------------------

    def read(self):

        """
        Возвращает одно измерение

        bx
        by
        bz
        b
        """

        bx, by, bz = self.sensor.magnetic

        b = math.sqrt(
            bx * bx +
            by * by +
            bz * bz
        )

        return {

            "bx": bx,
            "by": by,
            "bz": bz,
            "b": b

        }

    # --------------------------------------------

    def measure(self):

        """
        Усреднение нескольких измерений
        """

        bx_sum = 0.0
        by_sum = 0.0
        bz_sum = 0.0

        for _ in range(SAMPLES_PER_POINT):

            value = self.read()

            bx_sum += value["bx"]
            by_sum += value["by"]
            bz_sum += value["bz"]

            time.sleep(0.003)

        bx = bx_sum / SAMPLES_PER_POINT
        by = by_sum / SAMPLES_PER_POINT
        bz = bz_sum / SAMPLES_PER_POINT

        b = math.sqrt(
            bx * bx +
            by * by +
            bz * bz
        )

        return {

            "bx": bx,
            "by": by,
            "bz": bz,
            "b": b

        }

    # --------------------------------------------

    def test(self):

        print()
        print("Поднеси магнит")
        print("Ctrl+C для выхода")
        print()

        try:

            while True:

                value = self.measure()

                print(

                    f"Bx = {value['bx']:9.2f} uT   "

                    f"By = {value['by']:9.2f} uT   "

                    f"Bz = {value['bz']:9.2f} uT   "

                    f"|B| = {value['b']:9.2f} uT"

                )

                time.sleep(0.1)

        except KeyboardInterrupt:

            print()
            print("Проверка остановлена.")

    # --------------------------------------------

    def zero(self):

        """
        Запоминает поле Земли.

        Потом будем отображать поле
        относительно него.
        """

        print("Калибровка...")

        value = self.measure()

        self.zero_bx = value["bx"]
        self.zero_by = value["by"]
        self.zero_bz = value["bz"]

        print("Готово.")

    # --------------------------------------------

    def measure_relative(self):

        value = self.measure()

        bx = value["bx"] - self.zero_bx
        by = value["by"] - self.zero_by
        bz = value["bz"] - self.zero_bz

        b = math.sqrt(

            bx * bx +

            by * by +

            bz * bz

        )

        return {

            "bx": bx,

            "by": by,

            "bz": bz,

            "b": b

        }




