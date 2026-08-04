"""Экспорт списка измеренных точек в CSV, удобный для Excel и LibreOffice."""

# csv создаёт таблицу без ручной обработки кавычек и разделителей.
import csv
# os создаёт каталог results и соединяет части пути независимо от ОС.
import os
# datetime формирует уникальное имя файла по текущему времени.
from datetime import datetime

# RESULTS_FOLDER — папка экспорта из общего файла настроек.
from config import RESULTS_FOLDER


def save(results):
    """Записывает метаданные и все точки скана; возвращает путь к CSV-файлу."""
    os.makedirs(RESULTS_FOLDER, exist_ok=True)
    now = datetime.now()
    filename = now.strftime("magnetic_map_%Y-%m-%d_%H-%M-%S.csv")
    filepath = os.path.join(RESULTS_FOLDER, filename)

    # utf-8-sig добавляет BOM: Excel корректно распознаёт русские заголовки.
    with open(filepath, "w", newline="", encoding="utf-8-sig") as file:
        # `;` выбран вместо `,`, так как русская локаль часто использует запятую
        # как десятичный разделитель.
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["КАРТА МАГНИТНОГО ПОЛЯ"])
        writer.writerow(["Дата", now.strftime("%d.%m.%Y")])
        writer.writerow(["Время", now.strftime("%H:%M:%S")])
        writer.writerow(["Количество точек", len(results)])
        writer.writerow([])
        writer.writerow([
            "№", "X относительно центра", "Y относительно центра",
            "X принтера", "Y принтера", "Z", "Bx (uT)", "By (uT)",
            "Bz (uT)", "|B| (uT)",
        ])

        # Каждая запись results была собрана scanner.scan() после измерения.
        for index, item in enumerate(results, start=1):
            writer.writerow([
                index, f"{item['x']:.2f}", f"{item['y']:.2f}",
                f"{item['printer_x']:.2f}", f"{item['printer_y']:.2f}",
                f"{item['z']:.2f}", f"{item['bx']:.3f}",
                f"{item['by']:.3f}", f"{item['bz']:.3f}", f"{item['b']:.3f}",
            ])

    print("CSV сохранён:", filepath)
    return filepath
