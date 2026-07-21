# ============================================================
# csv_export.py
# ============================================================

import csv
import os

from datetime import datetime

from config import RESULTS_FOLDER


def save(results):

    os.makedirs(
        RESULTS_FOLDER,
        exist_ok=True
    )

    now = datetime.now()

    filename = now.strftime(
        "magnetic_map_%Y-%m-%d_%H-%M-%S.csv"
    )

    filepath = os.path.join(
        RESULTS_FOLDER,
        filename
    )

    with open(
        filepath,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.writer(
            file,
            delimiter=";"
        )

        writer.writerow([
            "КАРТА МАГНИТНОГО ПОЛЯ"
        ])

        writer.writerow([
            "Дата",
            now.strftime("%d.%m.%Y")
        ])

        writer.writerow([
            "Время",
            now.strftime("%H:%M:%S")
        ])

        writer.writerow([
            "Количество точек",
            len(results)
        ])

        writer.writerow([])

        writer.writerow([

            "№",

            "X относительно центра",

            "Y относительно центра",

            "X принтера",

            "Y принтера",

            "Z",

            "Bx (uT)",

            "By (uT)",

            "Bz (uT)",

            "|B| (uT)"

        ])

        for i, item in enumerate(
            results,
            start=1
        ):

            writer.writerow([

                i,

                f"{item['x']:.2f}",

                f"{item['y']:.2f}",

                f"{item['printer_x']:.2f}",

                f"{item['printer_y']:.2f}",

                f"{item['z']:.2f}",

                f"{item['bx']:.3f}",

                f"{item['by']:.3f}",

                f"{item['bz']:.3f}",

                f"{item['b']:.3f}"

            ])

    print()
    print("CSV сохранён")
    print(filepath)

    return filepath