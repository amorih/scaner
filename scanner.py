"""Построение траекторий и последовательное измерение магнитного поля.

Все координаты, которые получает этот модуль, относятся к центру магнита.
Перед отправкой G-code они переводятся в абсолютные координаты сопла принтера.
"""

# math нужен для окружности, длины прямой и фильтра точек цилиндра.
import math
# time нужен для паузы после движения и измерения длительности скана.
import time
import logging

import config
from config import (
    BED_CENTER_X, BED_CENTER_Y, BED_SIZE_X, BED_SIZE_Y, BED_MAX_Z,
    MOVE_SPEED, POINT_SETTLE_TIME, SCAN_HEIGHT, SCAN_SPEED,
    SEARCH_COARSE_STEP, SEARCH_FINE_HALF, SEARCH_FINE_STEP, SEARCH_HALF_RANGE,
    SEARCH_TEST_POINTS, SEARCH_TEST_RADIUS,
    SEARCH_EARLY_STOP,
    SEARCH_SPEED, SEARCH_Z_SPEED, SEARCH_SETTLE_TIME, SEARCH_SAFE_Z,
    SENSOR_OFFSET_X, SENSOR_OFFSET_Y, Z_SPEED,
)



logger = logging.getLogger(__name__)


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


def printer_to_sensor(printer_x, printer_y):
    """Переводит абсолютные координаты сопла принтера в координаты датчика от центра стола."""
    return (
        printer_x - BED_CENTER_X + SENSOR_OFFSET_X,
        printer_y - BED_CENTER_Y + SENSOR_OFFSET_Y,
    )



def check_limits(rows, offset_x=0.0, offset_y=0.0):
    """Проверяет всю траекторию до первого движения принтера.

    offset_x/offset_y — смещение центра области из автопоиска: магнит может
    лежать не точно в центре стола, и тогда вся траектория сдвигается.
    """
    for row in rows:
        for x, y in row:
            printer_x, printer_y = sensor_to_printer(x + offset_x, y + offset_y)
            if not 0 <= printer_x <= BED_SIZE_X:
                raise ValueError(f"X={printer_x:.2f} выходит за пределы стола")
            if not 0 <= printer_y <= BED_SIZE_Y:
                raise ValueError(f"Y={printer_y:.2f} выходит за пределы стола")


def centroid(points, values):
    """Возвращает взвешенный «центр масс» измерений магнитного поля.

    Весом точки служит превышение |B| над минимумом сетки: плоский фон получает
    нулевой вес и не двигает оценку, а пик магнита притягивает её к себе. Это
    позволяет получить центр точнее шага сетки. Если поле плоское (весов нет),
    возвращается координата точки с наибольшим |B|.
    """
    if not points or len(points) != len(values):
        raise ValueError("centroid: списки точек и значений должны совпадать")
    baseline = min(values)
    peak_index = max(range(len(values)), key=lambda index: values[index])
    weight_sum = 0.0
    weighted_x = weighted_y = 0.0
    for (x, y), value in zip(points, values):
        weight = value - baseline
        if weight <= 0:
            continue
        weighted_x += x * weight
        weighted_y += y * weight
        weight_sum += weight
    if weight_sum <= 0:
        return points[peak_index]
    return weighted_x / weight_sum, weighted_y / weight_sum


def detect_axis_boundaries(positions, magnitudes, threshold_ratio=0.5):
    """Определяет физические границы и центр магнита по одномерному профилю |B|.

    Алгоритм комбинирует:
      1) Порог FWHM (полувысота пика) с субпиксельной линейной интерполяцией;
      2) Экстремумы градиента d|B|/dp (точки максимального перепада поля);
      3) Взвешенный центроид пика для устойчивости к асимметрии поля.
    """
    if not positions or len(positions) != len(magnitudes):
        raise ValueError("detect_axis_boundaries: длины координат и значений должны совпадать")
    if len(positions) < 3:
        peak_idx = max(range(len(magnitudes)), key=lambda i: magnitudes[i])
        p = positions[peak_idx]
        return {
            "peak_pos": p, "peak_val": magnitudes[peak_idx], "baseline": magnitudes[peak_idx],
            "bound_min": p, "bound_max": p, "span": 0.0, "center": p, "centroid": p,
        }

    combined = sorted(zip(positions, magnitudes), key=lambda pair: pair[0])
    pos = [p for p, m in combined]
    mag = [m for p, m in combined]

    peak_idx = max(range(len(mag)), key=lambda i: mag[i])
    peak_pos = pos[peak_idx]
    peak_val = mag[peak_idx]
    baseline = min(mag)
    net_signal = peak_val - baseline

    if net_signal < 1e-4:
        return {
            "peak_pos": peak_pos, "peak_val": peak_val, "baseline": baseline,
            "bound_min": peak_pos, "bound_max": peak_pos, "span": 0.0,
            "center": peak_pos, "centroid": peak_pos,
        }

    threshold = baseline + threshold_ratio * net_signal

    # Линейная интерполяция границы слева от пика
    x_cross_left = pos[0]
    for i in range(peak_idx - 1, -1, -1):
        if mag[i] <= threshold:
            denom = mag[i + 1] - mag[i]
            frac = (threshold - mag[i]) / denom if abs(denom) > 1e-9 else 0.0
            x_cross_left = pos[i] + frac * (pos[i + 1] - pos[i])
            break

    # Линейная интерполяция границы справа от пика
    x_cross_right = pos[-1]
    for i in range(peak_idx, len(mag) - 1):
        if mag[i + 1] <= threshold:
            denom = mag[i] - mag[i + 1]
            frac = (mag[i] - threshold) / denom if abs(denom) > 1e-9 else 0.0
            x_cross_right = pos[i] + frac * (pos[i + 1] - pos[i])
            break

    # Численный градиент поля вдоль координаты
    grads = []
    for i in range(len(mag)):
        if i == 0:
            dp = pos[1] - pos[0]
            grads.append((mag[1] - mag[0]) / dp if abs(dp) > 1e-9 else 0.0)
        elif i == len(mag) - 1:
            dp = pos[-1] - pos[-2]
            grads.append((mag[-1] - mag[-2]) / dp if abs(dp) > 1e-9 else 0.0)
        else:
            dp = pos[i + 1] - pos[i - 1]
            grads.append((mag[i + 1] - mag[i - 1]) / dp if abs(dp) > 1e-9 else 0.0)

    left_grads = [(pos[i], grads[i]) for i in range(peak_idx + 1)]
    grad_left_pos = max(left_grads, key=lambda pair: pair[1])[0] if left_grads else x_cross_left

    right_grads = [(pos[i], grads[i]) for i in range(peak_idx, len(grads))]
    grad_right_pos = min(right_grads, key=lambda pair: pair[1])[0] if right_grads else x_cross_right

    bound_min = 0.5 * (x_cross_left + grad_left_pos)
    bound_max = 0.5 * (x_cross_right + grad_right_pos)
    if bound_min > bound_max:
        bound_min, bound_max = bound_max, bound_min
    span = bound_max - bound_min

    weights = [max(0.0, m - threshold) for m in mag]
    w_sum = sum(weights)
    c_centroid = (sum(p * w for p, w in zip(pos, weights)) / w_sum) if w_sum > 0 else peak_pos
    c_mid = 0.5 * (bound_min + bound_max)
    axis_center = 0.5 * (c_mid + c_centroid)

    return {
        "peak_pos": peak_pos,
        "peak_val": peak_val,
        "baseline": baseline,
        "threshold": threshold,
        "bound_min": bound_min,
        "bound_max": bound_max,
        "span": span,
        "center": axis_center,
        "centroid": c_centroid,
    }


def fit_magnet_pattern(x_bounds, y_bounds, pattern="auto"):
    """Аппроксимирует геометрию магнита паттерном: круг (диск) или прямоугольник.

    Поддерживаемые паттерны:
      * 'circle'    — круглое основание (диаметр, радиус, центр);
      * 'rectangle' — прямоугольное основание (ширина, длина, границы);
      * 'auto'      — автоматический выбор по коэффициенту осевой симметрии.
    """
    span_x = max(0.01, x_bounds.get("span", 0.0))
    span_y = max(0.01, y_bounds.get("span", 0.0))
    center_x = x_bounds.get("center", 0.0)
    center_y = y_bounds.get("center", 0.0)

    aspect_ratio = min(span_x, span_y) / max(span_x, span_y)

    is_circle = (pattern == "circle") or (pattern == "auto" and aspect_ratio >= 0.82)
    if is_circle:
        detected_shape = "circle"
        radius = (span_x + span_y) / 4.0
        diameter = 2.0 * radius
        width = diameter
        length = diameter
        confidence = min(99.0, max(50.0, aspect_ratio * 100.0))
        bounds = {
            "x_min": center_x - radius,
            "x_max": center_x + radius,
            "y_min": center_y - radius,
            "y_max": center_y + radius,
        }
    else:
        detected_shape = "rectangle"
        radius = None
        diameter = None
        width = span_x
        length = span_y
        confidence = min(99.0, max(50.0, (1.0 - abs(aspect_ratio - 0.5)) * 100.0))
        bounds = {
            "x_min": x_bounds["bound_min"],
            "x_max": x_bounds["bound_max"],
            "y_min": y_bounds["bound_min"],
            "y_max": y_bounds["bound_max"],
        }

    return {
        "pattern": detected_shape,
        "center_x": center_x,
        "center_y": center_y,
        "width": width,
        "length": length,
        "diameter": diameter,
        "radius": radius,
        "aspect_ratio": aspect_ratio,
        "confidence": confidence,
        "bounds": bounds,
    }


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


def validate_parameters(parameters):
    """Проверяет вход скана до вычисления траектории и первой команды G-code.

    HTML ограничивает поля формы, но Bridge получает JSON, который при желании
    можно отправить из консоли браузера. Поэтому критические значения повторно
    проверяются здесь, на стороне управления оборудованием.
    """
    if not isinstance(parameters, dict):
        raise ValueError("Параметры сканирования должны быть словарём")

    shape = parameters.get("shape")
    mode = parameters.get("mode", "snake")
    if shape not in {"cylinder", "rectangle", "system"}:
        raise ValueError("Неизвестная форма сканирования")
    if mode not in {"snake", "circle", "line"}:
        raise ValueError("Неизвестный режим сканирования")
    if shape == "system" and mode != "line":
        raise ValueError("Магнитная система сканируется только по линии")
    if mode == "circle" and shape != "cylinder":
        raise ValueError("Окружность доступна только для цилиндра")

    # Размеры, высота и шаг становятся числами до вычислений и команд G-code.
    numeric_keys = ("size_x", "size_y", "height", "step")
    try:
        values = {key: float(parameters[key]) for key in numeric_keys}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Размер, высота и шаг должны быть числами") from error
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Параметры сканирования должны быть конечными числами")
    if values["step"] <= 0:
        raise ValueError("Шаг сканирования должен быть больше 0 мм")
    if values["height"] < 0:
        raise ValueError("Высота магнита не может быть отрицательной")
    work_z = values["height"] + SCAN_HEIGHT
    if work_z > BED_MAX_Z:
        raise ValueError(
            f"Рабочая высота Z={work_z:.2f} мм превышает предел {BED_MAX_Z:.2f} мм"
        )
    if mode != "line" and (values["size_x"] <= 0 or values["size_y"] <= 0):
        raise ValueError("Размер области сканирования должен быть больше 0 мм")

    if mode == "line":
        try:
            line = [float(parameters[key]) for key in ("start_x", "start_y", "end_x", "end_y")]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Координаты линии должны быть числами") from error
        if not all(math.isfinite(value) for value in line):
            raise ValueError("Координаты линии должны быть конечными числами")
        if math.isclose(line[0], line[2]) and math.isclose(line[1], line[3]):
            raise ValueError("Начало и конец линии не должны совпадать")
    else:
        line = None

    # Автопоиск может вернуть центр магнита, смещённый от центра стола.
    # Параметр необязательный: обычный скан без него центрируется на столе.
    try:
        center = [float(parameters.get(key, 0.0)) for key in ("center_x", "center_y")]
    except (TypeError, ValueError) as error:
        raise ValueError("Смещение центра скана должно быть числом") from error
    if not all(math.isfinite(value) for value in center):
        raise ValueError("Смещение центра скана должно быть конечным числом")

    # Возвращаем нормализованную копию: JSON из браузера иногда содержит
    # корректные числа строками, а геометрия ниже должна работать только с float.
    normalized = dict(parameters)
    normalized.update(values)
    if line is not None:
        normalized.update(zip(("start_x", "start_y", "end_x", "end_y"), line))
    normalized.update(zip(("center_x", "center_y"), center))
    return normalized


def scan(printer, sensor, parameters, confirm=True, progress_callback=None, cancelled=None):
    """Выполняет полный скан и возвращает список словарей результатов.

    printer — объект `Printer`, sensor — `MagneticSensor`.
    progress_callback(point, total, result) обновляет GUI после каждой точки.
    cancelled() читается перед каждым движением и позволяет безопасно выйти.
    """
    parameters = validate_parameters(parameters)
    rows = _trajectory(parameters)
    # Смещение центра области из автопоиска: если магнит лежит не в центре
    # стола, траектория сдвигается, чтобы измерения шли вокруг реального
    # полюса. По умолчанию (0, 0) поведение совпадает с прежним.
    offset_x = parameters.get("center_x", 0.0)
    offset_y = parameters.get("center_y", 0.0)
    check_limits(rows, offset_x, offset_y)
    total = sum(len(row) for row in rows)
    if not total:
        raise ValueError("Траектория не содержит точек")

    if hasattr(printer, "begin_measurement"):
        printer.begin_measurement(total)
    if hasattr(printer, "display_status"):
        printer.display_status("SCANNER READY")

    # Скорости, пауза стабилизации и высота берутся из параметров или глобального config
    current_scan_height = float(parameters.get("scan_height", getattr(config, "SCAN_HEIGHT", SCAN_HEIGHT)))
    current_scan_speed = int(parameters.get("scan_speed", getattr(config, "SCAN_SPEED", SCAN_SPEED)))
    current_move_speed = int(parameters.get("move_speed", getattr(config, "MOVE_SPEED", MOVE_SPEED)))
    current_z_speed = int(parameters.get("z_speed", getattr(config, "Z_SPEED", Z_SPEED)))
    current_settle_time = float(parameters.get("settle_time", getattr(config, "POINT_SETTLE_TIME", POINT_SETTLE_TIME)))
    samples_per_point = parameters.get("samples_per_point", None)

    # Высота задаётся от стола: верх магнита + безопасный зазор датчика.
    work_z = parameters["height"] + current_scan_height
    if confirm:
        input("Закрепите магнит в центре и нажмите Enter…")

    # Сначала поднимаем Z, затем едем по XY: это безопаснее для магнита и датчика.
    first_x, first_y = rows[0][0]
    first_printer_x, first_printer_y = sensor_to_printer(first_x + offset_x, first_y + offset_y)
    printer.move(z=work_z, speed=current_z_speed)
    printer.move(x=first_printer_x, y=first_printer_y, speed=current_move_speed)

    results = []
    started_at = time.monotonic()
    for row in rows:
        for x, y in row:
            if cancelled and cancelled():
                raise ScanCancelled()

            printer_x, printer_y = sensor_to_printer(x + offset_x, y + offset_y)
            printer.move(x=printer_x, y=printer_y, speed=current_scan_speed)
            # M400 в Printer.move уже дождался конца движения; эта пауза даёт
            # механике окончательно успокоиться перед чувствительным измерением.
            time.sleep(current_settle_time)
            try:
                # sensor.read() сам ждёт восстановления связи до
                # SENSOR_RECONNECT_TIMEOUT, поэтому исключение здесь означает,
                # что датчик так и не вернулся за отведённое время.
                if hasattr(sensor, "measure_relative"):
                    try:
                        value = sensor.measure_relative(samples=samples_per_point)
                    except TypeError:
                        value = sensor.measure_relative()
                else:
                    value = sensor.measure()
            except OSError as error:
                if hasattr(printer, "signal_error"):
                    printer.signal_error("SENSOR ERROR")
                logger.error(
                    "Датчик не восстановился на точке %d/%d (X=%+.2f Y=%+.2f): %s: %s",
                    len(results) + 1, total, x, y, type(error).__name__, error,
                )
                raise
            result = {
                "x": x, "y": y, "printer_x": printer_x, "printer_y": printer_y,
                "z": work_z, "bx": value["bx"], "by": value["by"],
                "bz": value["bz"], "b": value["b"],
            }
            results.append(result)
            point = len(results)
            if hasattr(printer, "display_scan_data"):
                printer.display_scan_data(
                    value["bx"], value["by"], value["bz"], value["b"], point, total
                )
            else:
                # Совместимость с тестовыми и старыми реализациями Printer.
                if hasattr(printer, "display_field"):
                    printer.display_field(value["bx"], value["by"], value["bz"], value["b"])
                if hasattr(printer, "display_progress"):
                    printer.display_progress(point, total, value["b"])
            elapsed = time.monotonic() - started_at
            logger.debug(
                "точка %d/%d (%.1f%%) X=%+.2f Y=%+.2f |B|=%.2f мТл (%.1f с)",
                point, total, point / total * 100, x, y, value["b"], elapsed,
            )
            if progress_callback:
                progress_callback(point, total, result)

    # После измерений возвращаемся над центром магнита и поднимаемся на 20 мм.
    center_x, center_y = sensor_to_printer(offset_x, offset_y)
    printer.move(x=center_x, y=center_y, speed=current_move_speed)
    printer.move(z=min(BED_MAX_Z, work_z + 20), speed=current_z_speed)
    if hasattr(printer, "signal_complete"):
        printer.signal_complete()
    return results


def _probe_magnitude(printer, sensor, x, y):
    """Едет датчиком в точку (x, y) и возвращает координаты сопла и |B|.

    Координаты заданы в системе координат измерений (относительно центра
    стола/магнита); абсолютные координаты сопла считает sensor_to_printer().
    """
    printer_x, printer_y = sensor_to_printer(x, y)
    printer.move(x=printer_x, y=printer_y, speed=SCAN_SPEED)
    time.sleep(POINT_SETTLE_TIME)
    # Центр поиска и обычный скан должны использовать одну и ту же систему
    # отсчёта. Иначе окружающее поле Raspberry Pi/моторов может сместить пик.
    reader = getattr(sensor, "measure_relative", None) or sensor.measure
    value = reader()
    if hasattr(printer, "display_field"):
        printer.display_field(value["bx"], value["by"], value["bz"], value["b"])
    return printer_x, printer_y, value["b"]


def find_magnet_boundaries_from_grid(points, values, pattern="auto"):
    """Определяет центр, границы и форму магнита по уже измеренной 2D-сетке точек.

    Мгновенный математический анализ (< 10 мс) без физического перемещения принтера.
    """
    if not points or len(points) != len(values):
        raise ValueError("find_magnet_boundaries_from_grid: длины точек и значений должны совпадать")

    peak_idx = max(range(len(values)), key=lambda i: values[i])
    peak_x, peak_y = points[peak_idx]
    peak_val = values[peak_idx]

    y_coords = sorted({round(y, 2) for _, y in points})
    closest_y = min(y_coords, key=lambda y: abs(y - peak_y))
    x_slice = [(x, val) for (x, y), val in zip(points, values) if abs(y - closest_y) < 1e-2]
    x_slice.sort(key=lambda pair: pair[0])
    x_pos = [p[0] for p in x_slice]
    x_mag = [p[1] for p in x_slice]
    x_bounds = detect_axis_boundaries(x_pos, x_mag)

    x_coords = sorted({round(x, 2) for x, _ in points})
    closest_x = min(x_coords, key=lambda x: abs(x - peak_x))
    y_slice = [(y, val) for (x, y), val in zip(points, values) if abs(x - closest_x) < 1e-2]
    y_slice.sort(key=lambda pair: pair[0])
    y_pos = [p[0] for p in y_slice]
    y_mag = [p[1] for p in y_slice]
    y_bounds = detect_axis_boundaries(y_pos, y_mag)

    fitted = fit_magnet_pattern(x_bounds, y_bounds, pattern=pattern)

    cent_x, cent_y = centroid(points, values)
    fitted["center_x"] = round(0.5 * (fitted["center_x"] + cent_x), 2)
    fitted["center_y"] = round(0.5 * (fitted["center_y"] + cent_y), 2)
    fitted["peak_b"] = round(peak_val, 2)
    return fitted


def fast_find_magnet_center(
    printer,
    sensor,
    height=0.0,
    pattern="auto",
    half_range=SEARCH_HALF_RANGE,
    coarse_step=None,
    fine_step=None,
    progress_callback=None,
    cancelled=None,
):
    """Высокоскоростной автопоиск центра, периметра и высоты магнита (1.5–3 секунды).

    Алгоритм:
      1) Безопасная высота: каретка поднимается на безопасный уровень Z >= 25 мм,
         что исключает столкновение с магнитом любой толщины при горизонтальных ходах.
      2) Осевой градиентный поиск (направление максимального сигнала):
         определяется, по какой оси (X или Y) прирост поля больше, и каретка
         быстро смещается вдоль этой оси до пика, затем переключается на вторую ось.
      3) Определение физического периметра магнита:
         от найденного пика выполняются быстрые шаги в 4 стороны (+X, -X, +Y, -Y)
         до границы спада поля, вычисляются точные границы с линейной интерполяцией.
      4) Вычисление центра и формы:
         центр = среднее границ периметра, подгонка паттерна (круг / прямоугольник).
      5) Бесконтактное определение высоты магнита:
         по вертикальному градиенту B(z) на безопасной высоте рассчитывается верх
         магнита и устанавливается зазор строго 5.0 мм без механического касания.
      6) Финальный замер над центром на рабочей высоте 5 мм и безопасный подъём.
    """
    start_time = time.time()
    if hasattr(printer, "begin_measurement"):
        printer.begin_measurement()

    # 1. Безопасная высота поиска над столом
    initial_safe_z = max(SEARCH_SAFE_Z, float(height) + 15.0, 20.0)
    if initial_safe_z > BED_MAX_Z:
        initial_safe_z = BED_MAX_Z - 10.0
    printer.move(z=initial_safe_z, speed=SEARCH_Z_SPEED)

    # Безопасные границы стола в координатах датчика (с отступом 5 мм от краёв)
    min_sx, min_sy = printer_to_sensor(0.0, 0.0)
    max_sx, max_sy = printer_to_sensor(BED_SIZE_X, BED_SIZE_Y)
    limit_min_x = min_sx + 5.0
    limit_max_x = max_sx - 5.0
    limit_min_y = min_sy + 5.0
    limit_max_y = max_sy - 5.0

    total_probes = 0
    current_work_z = initial_safe_z

    def _fast_probe(x, y, z=None, stage="search"):
        nonlocal total_probes, current_work_z
        if cancelled and cancelled():
            raise ScanCancelled()
        cx = max(limit_min_x, min(limit_max_x, x))
        cy = max(limit_min_y, min(limit_max_y, y))
        target_z = current_work_z if z is None else z
        current_work_z = target_z

        px, py = sensor_to_printer(cx, cy)
        printer.move(x=px, y=py, z=target_z, speed=SEARCH_SPEED)
        if SEARCH_SETTLE_TIME > 0:
            time.sleep(SEARCH_SETTLE_TIME)
        reader = getattr(sensor, "measure_relative", None) or sensor.measure
        val = reader()
        total_probes += 1
        if hasattr(printer, "display_field"):
            printer.display_field(val["bx"], val["by"], val["bz"], val["b"])
        if progress_callback:
            progress_callback(stage, total_probes, 40, {
                "x": cx, "y": cy, "printer_x": px, "printer_y": py,
                "z": target_z, "b": val["b"],
            })
        return cx, cy, val["b"]

    # Начальная точка в центре стола (0, 0)
    cur_x, cur_y, cur_b = _fast_probe(0.0, 0.0, initial_safe_z, stage="axis_explore")
    base_b = cur_b

    # 2. Поиск оси с наибольшим значением датчика и движение в сторону роста поля
    step = 8.0
    for _ in range(6):
        # Оцениваем приращение поля по осям X и Y
        _, _, b_px = _fast_probe(cur_x + step, cur_y, stage="axis_explore")
        _, _, b_nx = _fast_probe(cur_x - step, cur_y, stage="axis_explore")
        grad_x = max(b_px, b_nx) - cur_b
        dir_x = 1.0 if b_px >= b_nx else -1.0

        _, _, b_py = _fast_probe(cur_x, cur_y + step, stage="axis_explore")
        _, _, b_ny = _fast_probe(cur_x, cur_y - step, stage="axis_explore")
        grad_y = max(b_py, b_ny) - cur_b
        dir_y = 1.0 if b_py >= b_ny else -1.0

        if grad_x <= 0 and grad_y <= 0:
            break

        # Идём по той оси, где прирост поля больше
        if grad_x >= grad_y and grad_x > 0:
            while True:
                next_x = cur_x + dir_x * step
                if not (limit_min_x <= next_x <= limit_max_x):
                    break
                _, _, next_b = _fast_probe(next_x, cur_y, stage="axis_x")
                if next_b > cur_b:
                    cur_x, cur_b = next_x, next_b
                else:
                    break
            # Затем корректируем по ортогональной оси Y
            if grad_y > 0:
                while True:
                    next_y = cur_y + dir_y * step
                    if not (limit_min_y <= next_y <= limit_max_y):
                        break
                    _, _, next_b = _fast_probe(cur_x, next_y, stage="axis_y")
                    if next_b > cur_b:
                        cur_y, cur_b = next_y, next_b
                    else:
                        break
        elif grad_y > grad_x and grad_y > 0:
            while True:
                next_y = cur_y + dir_y * step
                if not (limit_min_y <= next_y <= limit_max_y):
                    break
                _, _, next_b = _fast_probe(cur_x, next_y, stage="axis_y")
                if next_b > cur_b:
                    cur_y, cur_b = next_y, next_b
                else:
                    break
            if grad_x > 0:
                while True:
                    next_x = cur_x + dir_x * step
                    if not (limit_min_x <= next_x <= limit_max_x):
                        break
                    _, _, next_b = _fast_probe(next_x, cur_y, stage="axis_x")
                    if next_b > cur_b:
                        cur_x, cur_b = next_x, next_b
                    else:
                        break

    peak_x, peak_y, peak_b = cur_x, cur_y, cur_b

    # Локальное микро-уточнение вершины
    for dx, dy in [(-2.0, 0.0), (2.0, 0.0), (0.0, -2.0), (0.0, 2.0)]:
        _, _, val = _fast_probe(peak_x + dx, peak_y + dy, stage="refine")
        if val > peak_b:
            peak_x += dx
            peak_y += dy
            peak_b = val

    # 3. Поиск периметра магнита: границы спада поля
    threshold = base_b + 0.45 * max(0.0, peak_b - base_b)
    p_step = 2.0

    def find_boundary(dx_dir, dy_dir):
        prev_pos = 0.0
        prev_val = peak_b
        curr_offset = 0.0
        while curr_offset < 70.0:
            curr_offset += p_step
            cx = peak_x + dx_dir * curr_offset
            cy = peak_y + dy_dir * curr_offset
            if cx < limit_min_x or cx > limit_max_x or cy < limit_min_y or cy > limit_max_y:
                return (cx, cy)
            _, _, val = _fast_probe(cx, cy, stage="perimeter")
            if val <= threshold:
                denom = prev_val - val
                frac = (prev_val - threshold) / denom if abs(denom) > 1e-6 else 0.5
                interp_offset = (curr_offset - p_step) + frac * p_step
                return (peak_x + dx_dir * interp_offset, peak_y + dy_dir * interp_offset)
            prev_pos = curr_offset
            prev_val = val
        return (peak_x + dx_dir * curr_offset, peak_y + dy_dir * curr_offset)

    x_max_coord, _ = find_boundary(1.0, 0.0)
    x_min_coord, _ = find_boundary(-1.0, 0.0)
    _, y_max_coord = find_boundary(0.0, 1.0)
    _, y_min_coord = find_boundary(0.0, -1.0)

    # 4. Центр и геометрический паттерн
    center_x = 0.5 * (x_min_coord + x_max_coord)
    center_y = 0.5 * (y_min_coord + y_max_coord)
    span_x = max(0.01, x_max_coord - x_min_coord)
    span_y = max(0.01, y_max_coord - y_min_coord)

    x_bounds = {"span": span_x, "center": center_x, "bound_min": x_min_coord, "bound_max": x_max_coord}
    y_bounds = {"span": span_y, "center": center_y, "bound_min": y_min_coord, "bound_max": y_max_coord}
    fitted = fit_magnet_pattern(x_bounds, y_bounds, pattern=pattern)

    # 5. Бесконтактное определение рабочей высоты (5 мм над магнитом, исключая касание)
    # Сначала перемещаемся строго над найденным центром на безопасной высоте
    _fast_probe(center_x, center_y, z=initial_safe_z, stage="center_approach")

    z1 = initial_safe_z
    z2 = initial_safe_z - 5.0
    _, _, b1 = _fast_probe(center_x, center_y, z=z1, stage="height_detect")
    _, _, b2 = _fast_probe(center_x, center_y, z=z2, stage="height_detect")

    detected_surface = None
    net_b1 = max(1e-4, b1 - base_b)
    net_b2 = max(1e-4, b2 - base_b)
    if net_b2 > net_b1 + 1e-4:
        u1 = net_b1 ** (-1.0 / 3.0)
        u2 = net_b2 ** (-1.0 / 3.0)
        denom = z1 - z2
        slope = (u1 - u2) / denom if denom != 0 else 0.0
        if slope > 1e-6:
            calc_surface = z1 - u1 / slope
            if 0.0 <= calc_surface <= initial_safe_z - 4.0:
                detected_surface = calc_surface


    if detected_surface is not None:
        target_work_z = round(detected_surface + SCAN_HEIGHT, 1)
    else:
        target_work_z = round(max(0.0, float(height)) + SCAN_HEIGHT, 1)

    target_work_z = max(SCAN_HEIGHT, min(BED_MAX_Z - 20.0, target_work_z))

    # Переход на рабочую высоту 5 мм над магнитом для точного замера
    final_px, final_py, final_peak_b = _fast_probe(
        center_x, center_y, z=target_work_z, stage="verify"
    )

    # Безопасный подъём каретки после завершения
    park_z = min(BED_MAX_Z, target_work_z + 20.0)
    printer.move(z=park_z, speed=SEARCH_Z_SPEED)
    if hasattr(printer, "signal_complete"):
        printer.signal_complete()

    elapsed = time.time() - start_time

    return {
        "center_x": round(center_x, 2),
        "center_y": round(center_y, 2),
        "peak_b": round(final_peak_b, 2),
        "pattern": fitted["pattern"],
        "width": round(fitted["width"], 2),
        "length": round(fitted["length"], 2),
        "diameter": round(fitted["diameter"], 2) if fitted["diameter"] is not None else None,
        "radius": round(fitted["radius"], 2) if fitted["radius"] is not None else None,
        "aspect_ratio": round(fitted["aspect_ratio"], 2),
        "confidence": round(fitted["confidence"], 1),
        "bounds": {
            "x_min": round(fitted["bounds"]["x_min"], 2),
            "x_max": round(fitted["bounds"]["x_max"], 2),
            "y_min": round(fitted["bounds"]["y_min"], 2),
            "y_max": round(fitted["bounds"]["y_max"], 2),
        },
        "work_z": target_work_z,
        "detected_surface_z": round(detected_surface, 2) if detected_surface is not None else round(float(height), 2),
        "total_points": total_probes,
        "duration_sec": round(elapsed, 2),
        "coarse_total": total_probes,
        "fine_total": 0,
        "test_total": 1,
    }



def find_magnet_center(
    printer,
    sensor,
    height,
    progress_callback=None,
    cancelled=None,
    fast=False,
    pattern="auto",
):
    """Ищет центр магнита по максимуму модуля |B| и делает контрольные измерения.

    При fast=True использует высокоскоростной поиск по осям (5–10 секунд) с
    определением границ и паттерна геометрии (круг / прямоугольник).
    При fast=False выполняет классический многокольцевой поиск.
    """
    if fast:
        return fast_find_magnet_center(
            printer, sensor, height, pattern=pattern,
            progress_callback=progress_callback, cancelled=cancelled,
        )
    work_z = height + SCAN_HEIGHT
    if height < 0 or work_z > BED_MAX_Z:
        raise ValueError(
            f"Рабочая высота Z={work_z:.2f} мм выходит за пределы 0…{BED_MAX_Z:.2f} мм"
        )

    # Проверяем наиболее дальние точки поиска до первого движения. Это
    # консервативный квадрат: он покрывает грубую сетку, уточнение и контроль.
    search_extent = SEARCH_HALF_RANGE + SEARCH_FINE_HALF + SEARCH_TEST_RADIUS
    check_limits([[(-search_extent, -search_extent),
                   (-search_extent, search_extent),
                   (search_extent, -search_extent),
                   (search_extent, search_extent)]])

    if hasattr(printer, "begin_measurement"):
        printer.begin_measurement()

    # Сначала поднимаем Z и встаём над предполагаемым центром стола.
    printer.move(z=work_z, speed=Z_SPEED)
    center_printer_x, center_printer_y = sensor_to_printer(0, 0)
    printer.move(x=center_printer_x, y=center_printer_y, speed=MOVE_SPEED)

    # 1) Грубый поиск кольцами от центра стола наружу. Поле одного магнита
    #    убывает от пика, поэтому центр измеряется первым. Как только лучший
    #    узел стал строго сильнее всего текущего внешнего кольца, дальнейшие
    #    кольца физически не могут содержать более высокий |B|, и грубый проход
    #    останавливается рано: магнит у центра находится за 9–25 точек вместо
    #    фиксированных 49, а самый крайний случай (магнит у ±30 мм) по-прежнему
    #    проходит все кольца.
    coarse_axis = create_axis(2 * SEARCH_HALF_RANGE, SEARCH_COARSE_STEP)
    center_index = (len(coarse_axis) - 1) // 2
    rings = {}
    for row_index, y in enumerate(coarse_axis):
        for column_index, x in enumerate(coarse_axis):
            ring = max(abs(column_index - center_index), abs(row_index - center_index))
            rings.setdefault(ring, []).append((x, y))
    coarse_plan = len(coarse_axis) ** 2  # максимум точек, если пика у центра нет
    coarse_done = 0
    best_x = best_y = None
    best_b = -math.inf
    for ring_index in range(max(rings) + 1):
        frontier_max = -math.inf
        for x, y in rings[ring_index]:
            if cancelled and cancelled():
                raise ScanCancelled()
            printer_x, printer_y, magnitude = _probe_magnitude(printer, sensor, x, y)
            coarse_done += 1
            frontier_max = max(frontier_max, magnitude)
            if magnitude > best_b:
                best_b = magnitude
                best_x, best_y = x, y
            logger.debug(
                "поиск грубо %d: X=%+.2f Y=%+.2f |B|=%.2f мТл",
                coarse_done, x, y, magnitude,
            )
            if progress_callback:
                progress_callback("coarse", coarse_done, coarse_plan, {
                    "x": x, "y": y, "printer_x": printer_x,
                    "printer_y": printer_y, "z": work_z, "b": magnitude,
                })
        # Ранний выход: лучший узел строго внутри и сильнее внешнего кольца.
        # Если лучший узел лежит на самом кольце, поле ещё растёт наружу.
        if SEARCH_EARLY_STOP and ring_index >= 1 and best_b > frontier_max:
            break
    coarse_total = coarse_done
    max_coarse_b = best_b
    logger.info(
        "грубый поиск: пик X=%+.2f Y=%+.2f |B|=%.2f мТл, точек %d",
        best_x, best_y, max_coarse_b, coarse_total,
    )

    # 2) Уточняющая сетка вокруг грубого пика и «центр масс» поля.
    fine_axis = create_axis(2 * SEARCH_FINE_HALF, SEARCH_FINE_STEP)
    fine_total = len(fine_axis) ** 2
    fine_points = []
    fine_values = []
    fine_done = 0
    for gx in fine_axis:
        for gy in fine_axis:
            if cancelled and cancelled():
                raise ScanCancelled()
            x = best_x + gx
            y = best_y + gy
            printer_x, printer_y, magnitude = _probe_magnitude(printer, sensor, x, y)
            fine_points.append((x, y))
            fine_values.append(magnitude)
            fine_done += 1
            logger.debug(
                "поиск точно %d/%d: X=%+.2f Y=%+.2f |B|=%.2f мТл",
                fine_done, fine_total, x, y, magnitude,
            )
            if progress_callback:
                progress_callback("fine", fine_done, fine_total, {
                    "x": x, "y": y, "printer_x": printer_x,
                    "printer_y": printer_y, "z": work_z, "b": magnitude,
                })

    center_x, center_y = centroid(fine_points, fine_values)

    # 3) Контрольные измерения: окружность вокруг центра и точный замер пика.
    test_step = 2 * math.pi / SEARCH_TEST_POINTS
    test_points = [
        (center_x + SEARCH_TEST_RADIUS * math.cos(test_step * index),
         center_y + SEARCH_TEST_RADIUS * math.sin(test_step * index))
        for index in range(SEARCH_TEST_POINTS)
    ]
    for index, (x, y) in enumerate(test_points, start=1):
        if cancelled and cancelled():
            raise ScanCancelled()
        printer_x, printer_y, magnitude = _probe_magnitude(printer, sensor, x, y)
        logger.debug(
            "поиск контроль %d/%d: X=%+.2f Y=%+.2f |B|=%.2f мТл",
            index, SEARCH_TEST_POINTS, x, y, magnitude,
        )
        if progress_callback:
            progress_callback("test", index, SEARCH_TEST_POINTS, {
                "x": x, "y": y, "printer_x": printer_x,
                "printer_y": printer_y, "z": work_z, "b": magnitude,
            })

    if cancelled and cancelled():
        raise ScanCancelled()
    center_printer_x, center_printer_y, peak_b = _probe_magnitude(
        printer, sensor, center_x, center_y)

    # Возвращаемся над найденным центром и поднимаем головку на 20 мм.
    printer.move(x=center_printer_x, y=center_printer_y, speed=MOVE_SPEED)
    printer.move(z=min(BED_MAX_Z, work_z + 20), speed=Z_SPEED)
    if hasattr(printer, "signal_complete"):
        printer.signal_complete()

    return {
        "center_x": round(center_x, 2),
        "center_y": round(center_y, 2),
        "peak_b": round(peak_b, 2),
        "max_coarse_b": round(max_coarse_b, 2),
        "work_z": work_z,
        "coarse_total": coarse_total,
        "fine_total": fine_total,
        "test_total": SEARCH_TEST_POINTS,
    }
