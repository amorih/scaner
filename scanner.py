"""Построение траекторий и последовательное измерение магнитного поля.

Все координаты, которые получает этот модуль, относятся к центру магнита.
Перед отправкой G-code они переводятся в абсолютные координаты сопла принтера.
"""

# math нужен для окружности, длины прямой и фильтра точек цилиндра.
import math
# time нужен для паузы после движения и измерения длительности скана.
import time

# Импортируем геометрию, смещения и скорости из единого config.py.
from config import (
    BED_CENTER_X, BED_CENTER_Y, BED_SIZE_X, BED_SIZE_Y,
    MOVE_SPEED, POINT_SETTLE_TIME, SCAN_HEIGHT, SCAN_SPEED,
    SENSOR_OFFSET_X, SENSOR_OFFSET_Y, Z_SPEED,
)


def create_axis(length, step):
    """Строит симметричную ось от -length/2 до +length/2.

    Число интервалов всегда чётное: из-за этого в список обязательно попадает
    ноль, то есть геометрический центр магнита.
    """
    divisions = max(2, math.ceil(length / step))
    if divisions % 2:
        divisions += 1
    real_step = length / divisions  # Фактический шаг не превышает запрошенный.
    return [-length / 2 + index * real_step for index in range(divisions + 1)]


def create_scan(shape, size_x, size_y, step):
    """Создаёт строки площадного скана в порядке «змейка».

    Для цилиндра из прямоугольной сетки исключаются точки, лежащие за кругом.
    Каждая вторая строка развёрнута, чтобы головка не ездила впустую к началу.
    """
    rows = []
    radius = size_x / 2
    for row_index, y in enumerate(create_axis(size_y, step)):
        row = []
        for x in create_axis(size_x, step):
            if shape == "cylinder" and math.hypot(x, y) > radius:
                continue
            row.append((x, y))
        if row_index % 2:
            row.reverse()
        # Крайняя строка окружности может оказаться пустой — не сохраняем её,
        # иначе у скана не будет первой реальной точки.
        if row:
            rows.append(row)
    return rows


def create_circle_scan(diameter, step):
    """Создаёт замкнутую траекторию по окружности цилиндрического магнита."""
    radius = diameter / 2
    # Минимум 8 точек сохраняет форму круга даже для крупного шага.
    point_count = max(8, math.ceil(2 * math.pi * radius / step))
    return [[
        (radius * math.cos(2 * math.pi * index / point_count),
         radius * math.sin(2 * math.pi * index / point_count))
        for index in range(point_count)
    ]]


def create_line_scan(start_x, start_y, end_x, end_y, step):
    """Создаёт равномерные точки от начала до конца магнитной системы."""
    length = math.hypot(end_x - start_x, end_y - start_y)
    divisions = max(1, math.ceil(length / step))
    return [[
        (start_x + (end_x - start_x) * index / divisions,
         start_y + (end_y - start_y) * index / divisions)
        for index in range(divisions + 1)
    ]]


def sensor_to_printer(sensor_x, sensor_y):
    """Переводит координаты датчика от центра магнита в координаты сопла.

    Сначала добавляем центр стола, затем вычитаем физическое смещение датчика
    от сопла. Именно эти координаты будут отправлены в G1.
    """
    absolute_sensor_x = BED_CENTER_X + sensor_x
    absolute_sensor_y = BED_CENTER_Y + sensor_y
    return absolute_sensor_x - SENSOR_OFFSET_X, absolute_sensor_y - SENSOR_OFFSET_Y


def check_limits(rows):
    """Проверяет всю траекторию до первого движения принтера."""
    for row in rows:
        for x, y in row:
            printer_x, printer_y = sensor_to_printer(x, y)
            if not 0 <= printer_x <= BED_SIZE_X:
                raise ValueError(f"X={printer_x:.2f} выходит за пределы стола")
            if not 0 <= printer_y <= BED_SIZE_Y:
                raise ValueError(f"Y={printer_y:.2f} выходит за пределы стола")


class ScanCancelled(RuntimeError):
    """Сигнализирует GUI, что пользователь отменил сканирование кнопкой M112."""


def _trajectory(parameters):
    """Выбирает генератор пути по данным формы и режима из интерфейса."""
    mode = parameters.get("mode", "snake")
    if mode == "circle":
        return create_circle_scan(parameters["size_x"], parameters["step"])
    if mode == "line":
        return create_line_scan(
            parameters["start_x"], parameters["start_y"],
            parameters["end_x"], parameters["end_y"], parameters["step"],
        )
    return create_scan(
        parameters["shape"], parameters["size_x"], parameters["size_y"],
        parameters["step"],
    )


def scan(printer, sensor, parameters, confirm=True, progress_callback=None, cancelled=None):
    """Выполняет полный скан и возвращает список словарей результатов.

    printer — объект `Printer`, sensor — `MagneticSensor`.
    progress_callback(point, total, result) обновляет GUI после каждой точки.
    cancelled() читается перед каждым движением и позволяет безопасно выйти.
    """
    rows = _trajectory(parameters)
    check_limits(rows)
    total = sum(len(row) for row in rows)
    if not total:
        raise ValueError("Траектория не содержит точек")

    # Высота задаётся от стола: верх магнита + безопасный зазор датчика.
    work_z = parameters["height"] + SCAN_HEIGHT
    if confirm:
        input("Закрепите магнит в центре и нажмите Enter…")

    # Сначала поднимаем Z, затем едем по XY: это безопаснее для магнита и датчика.
    first_x, first_y = rows[0][0]
    first_printer_x, first_printer_y = sensor_to_printer(first_x, first_y)
    printer.move(z=work_z, speed=Z_SPEED)
    printer.move(x=first_printer_x, y=first_printer_y, speed=MOVE_SPEED)

    results = []
    started_at = time.time()
    for row in rows:
        for x, y in row:
            if cancelled and cancelled():
                raise ScanCancelled()

            printer_x, printer_y = sensor_to_printer(x, y)
            printer.move(x=printer_x, y=printer_y, speed=SCAN_SPEED)
            # M400 в Printer.move уже дождался конца движения; эта пауза даёт
            # механике окончательно успокоиться перед чувствительным измерением.
            time.sleep(POINT_SETTLE_TIME)
            value = sensor.measure_relative()
            result = {
                "x": x, "y": y, "printer_x": printer_x, "printer_y": printer_y,
                "z": work_z, "bx": value["bx"], "by": value["by"],
                "bz": value["bz"], "b": value["b"],
            }
            results.append(result)
            point = len(results)
            elapsed = time.time() - started_at
            print(f"[{point}/{total}] {point / total * 100:5.1f}% "
                  f"X={x:+7.2f} Y={y:+7.2f} |B|={value['b']:8.2f} µT "
                  f"({elapsed:.1f} c)")
            if progress_callback:
                progress_callback(point, total, result)

    # После измерений возвращаемся над центром и поднимаемся ещё на 20 мм.
    center_x, center_y = sensor_to_printer(0, 0)
    printer.move(x=center_x, y=center_y, speed=MOVE_SPEED)
    printer.move(z=work_z + 20, speed=Z_SPEED)
    return results
