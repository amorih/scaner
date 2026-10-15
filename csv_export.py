"""Экспорт списка измеренных точек в CSV, удобный для Excel и LibreOffice."""

# csv создаёт таблицу без ручной обработки кавычек и разделителей.
import csv
# datetime формирует уникальное имя файла по текущему времени.
from datetime import datetime
from pathlib import Path

# RESULTS_FOLDER — папка экспорта из общего файла настроек.
from config import RESULTS_FOLDER


def save(results):
    """Записывает метаданные и все точки скана; возвращает путь к CSV-файлу."""
    configured_directory = Path(RESULTS_FOLDER)
    results_directory = configured_directory
    if not results_directory.is_absolute():
        results_directory = Path(__file__).resolve().parent / results_directory
    results_directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = now.strftime("magnetic_map_%Y-%m-%d_%H-%M-%S.csv")
    filepath = results_directory / filename

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
            "№", "X относительно центра (мм)", "Y относительно центра (мм)",
            "X принтера (мм)", "Y принтера (мм)", "Z (мм)", "Bx (мТл)", "By (мТл)",
            "Bz (мТл)", "|B| (мТл)",
        ])

        # Каждая запись results была собрана scanner.scan() после измерения.
        for index, item in enumerate(results, start=1):
            writer.writerow([
                index, f"{item['x']:.2f}", f"{item['y']:.2f}",
                f"{item['printer_x']:.2f}", f"{item['printer_y']:.2f}",
                f"{item['z']:.2f}", f"{item['bx']:.3f}",
                f"{item['by']:.3f}", f"{item['bz']:.3f}", f"{item['b']:.3f}",
            ])

    # В архиве оставляем короткий относительный путь, если он задан в config.
    return str(filepath if configured_directory.is_absolute()
               else configured_directory / filename)
