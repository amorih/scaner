import math
import time

from config import *

# ------------------------------------------------------------

def create_axis(length, step):

    divisions = max(
        2,
        math.ceil(length / step)
    )

    # Keep the centre of the magnet on the grid.  Without an even number of
    # intervals, a 15 mm diameter with a 5 mm requested step has no X=0/Y=0
    # point and can produce empty outer rows for a cylinder.
    if divisions % 2:
        divisions += 1

    real_step = length / divisions

    return [

        -length / 2 +

        i * real_step

        for i in range(divisions + 1)

    ]

# ------------------------------------------------------------

def create_scan(shape, sx, sy, step):

    x_axis = create_axis(sx, step)
    y_axis = create_axis(sy, step)

    rows = []

    for row_id, y in enumerate(y_axis):

        row = []

        for x in x_axis:

            if shape == "cylinder":

                radius = sx / 2

                if math.sqrt(
                    x * x +
                    y * y
                ) > radius:

                    continue

            row.append(
                (x, y)
            )

        if row_id % 2:

            row.reverse()

        # A circular outline may leave the first/last Cartesian rows empty.
        # Do not retain them: scan() needs a real first point to start from.
        if row:
            rows.append(row)

    return rows

# ------------------------------------------------------------

def sensor_to_printer(x, y):

    sensor_x = BED_CENTER_X + x
    sensor_y = BED_CENTER_Y + y

    printer_x = sensor_x - SENSOR_OFFSET_X
    printer_y = sensor_y - SENSOR_OFFSET_Y

    return printer_x, printer_y

# ------------------------------------------------------------

def check_limits(rows):

    for row in rows:

        for x, y in row:

            px, py = sensor_to_printer(
                x,
                y
            )

            if px < 0 or px > BED_SIZE_X:

                raise Exception(
                    f"X={px:.2f} выходит за пределы"
                )

            if py < 0 or py > BED_SIZE_Y:

                raise Exception(
                    f"Y={py:.2f} выходит за пределы"
                )

# ------------------------------------------------------------

class ScanCancelled(RuntimeError):
    """Raised when a scan is interrupted from the desktop application."""


def scan(printer, sensor, parameters, confirm=True, progress_callback=None, cancelled=None):

    shape = parameters["shape"]
    sx = parameters["size_x"]
    sy = parameters["size_y"]
    magnet_height = parameters["height"]
    step = parameters["step"]

    work_z = magnet_height + SCAN_HEIGHT

    rows = create_scan(
        shape,
        sx,
        sy,
        step
    )

    check_limits(rows)

    total = sum(
        len(r)
        for r in rows
    )

    print()
    print("==============================")
    print("Начало сканирования")
    print("==============================")
    print()

    print("Всего точек:", total)

    if confirm:
        input("Поставь магнит в центр стола и нажми Enter...")

    first_x, first_y = rows[0][0]

    px, py = sensor_to_printer(
        first_x,
        first_y
    )

    printer.move(
        z=work_z,
        speed=Z_SPEED
    )

    printer.move(
        x=px,
        y=py,
        speed=MOVE_SPEED
    )

    results = []

    point = 0

    start = time.time()

    for row in rows:

        for x, y in row:

            if cancelled and cancelled():
                raise ScanCancelled()

            point += 1

            px, py = sensor_to_printer(
                x,
                y
            )

            printer.move(
                x=px,
                y=py,
                speed=SCAN_SPEED
            )

            time.sleep(
                POINT_SETTLE_TIME
            )

            value = sensor.measure_relative()

            elapsed = time.time() - start

            percent = point / total * 100

            print(

                f"[{point}/{total}] "

                f"{percent:5.1f}% "

                f"X={x:+7.2f} "

                f"Y={y:+7.2f} "

                f"Bx={value['bx']:8.2f} "

                f"By={value['by']:8.2f} "

                f"Bz={value['bz']:8.2f} "

                f"|B|={value['b']:8.2f}"

            )

            results.append({

                "x": x,

                "y": y,

                "printer_x": px,

                "printer_y": py,

                "z": work_z,

                "bx": value["bx"],

                "by": value["by"],

                "bz": value["bz"],

                "b": value["b"]

            })

            if progress_callback:
                progress_callback(point, total, results[-1])

    print()
    print("Сканирование завершено.")

    center_x, center_y = sensor_to_printer(
        0,
        0
    )

    printer.move(
        x=center_x,
        y=center_y,
        speed=MOVE_SPEED
    )

    printer.move(
        z=work_z + 20,
        speed=Z_SPEED
    )

    return results
