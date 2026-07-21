from config import *

from printer import Printer
from sensor import MagneticSensor
from scanner import scan
from csv_export import save


# ------------------------------------------------------------

def positive_float(text):

    while True:

        try:

            value = float(
                input(text).replace(",", ".")
            )

            if value <= 0:

                print("Число должно быть больше нуля.")

                continue

            return value

        except ValueError:

            print("Введите число.")

# ------------------------------------------------------------

def read_parameters():

    print()
    print("Форма магнита")
    print("1 - цилиндр")
    print("2 - прямоугольник")

    while True:

        choice = input("Выбор: ").strip()

        if choice == "1":

            diameter = positive_float(
                "Диаметр (мм): "
            )

            height = positive_float(
                "Высота магнита (мм): "
            )

            step = positive_float(
                "Шаг сканирования (мм): "
            )

            return {

                "shape": "cylinder",

                "size_x": diameter,

                "size_y": diameter,

                "height": height,

                "step": step

            }

        if choice == "2":

            size_x = positive_float(
                "Размер X (мм): "
            )

            size_y = positive_float(
                "Размер Y (мм): "
            )

            height = positive_float(
                "Высота магнита (мм): "
            )

            step = positive_float(
                "Шаг сканирования (мм): "
            )

            return {

                "shape": "rectangle",

                "size_x": size_x,

                "size_y": size_y,

                "height": height,

                "step": step

            }

        print("Выберите 1 или 2.")

# ------------------------------------------------------------

def print_header():

    print()
    print("========================================")
    print(PROJECT_NAME)
    print("Версия:", PROJECT_VERSION)
    print("========================================")
    print()

# ------------------------------------------------------------

def menu():

    print()

    print("1 - Проверить датчик")

    print("2 - Домой (G28)")

    print("3 - Сканировать магнит")

    print("0 - Выход")

    return input("Выбор: ").strip()

# ------------------------------------------------------------

def main():

    printer = None
    sensor = None

    try:

        print_header()

        print("Подключение к принтеру...")

        printer = Printer(
            PRINTER_PORT,
            PRINTER_BAUDRATE
        )

        print("OK")

        print()

        print("Подключение MLX90393...")

        sensor = MagneticSensor()

        print("OK")

        while True:

            choice = menu()

            if choice == "1":

                sensor.zero()

                sensor.test()

            elif choice == "2":

                printer.home()

            elif choice == "3":

                print()

                print("Калибровка поля Земли...")

                sensor.zero()

                printer.home()

                parameters = read_parameters()

                results = scan(

                    printer,

                    sensor,

                    parameters

                )

                filename = save(results)

                print()

                print("Результаты сохранены:")

                print(filename)

            elif choice == "0":

                break

            else:

                print("Неверный пункт.")

    except KeyboardInterrupt:

        print()

        print("Остановлено пользователем.")

        if printer is not None:

            printer.stop()

    except Exception as error:

        print()

        print("Ошибка:")

        print(type(error).__name__)

        print(error)

    finally:

        if printer is not None:

            printer.close()

# ------------------------------------------------------------

if __name__ == "__main__":

    main()