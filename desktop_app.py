#!/usr/bin/env python3
"""Главный запуск desktop-приложения NEXUS Studio.

Здесь нет формул магнитного поля и G-code: модуль связывает готовые модули
оборудования с HTML-интерфейсом через Qt WebEngine и QWebChannel.
"""

from __future__ import annotations  # Разрешает использовать типы классов до их объявления.

import os
# Безопасный запуск встроенного Chromium QtWebEngine на Linux / Raspberry Pi:
# предотвращает сбой контекста GPU (Context lost during MakeCurrent) на чипах Broadcom/Mesa
if "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

# json превращает словари Python в безопасные данные для JavaScript и обратно.
import json
import math
import threading
# sys передаёт Qt аргументы запуска и возвращает код завершения приложения.
import sys
# Path формирует абсолютный путь к локальным файлам UI рядом с этим модулем.
from pathlib import Path

# Базовые объекты Qt: сигналы, поток, таймер, URL и флаги окна.
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QThread, QTimer, QUrl, Signal, Slot, Qt
# QWebChannel публикует объект Bridge в JavaScript как `bridge`.
from PySide6.QtWebChannel import QWebChannel
# Настройки и виджет встроенного Chromium-движка для HTML-интерфейса.
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
# Нативные Qt-виджеты нужны только для оболочки, шапки и размещения WebEngine.
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMainWindow, QPushButton, QVBoxLayout, QWidget

# Настройки оборудования, общие для GUI и остальных Python-модулей.
import config
from config import (
    BED_MAX_Z,
    BED_SIZE_X,
    BED_SIZE_Y,
    MOVE_SPEED,
    PRINTER_BAUDRATE,
    PRINTER_PORT,
    PROJECT_NAME,
    RESULTS_FOLDER,
    SCAN_HEIGHT,
    SENSOR_OFFSET_X,
    SENSOR_OFFSET_Y,
    Z_SPEED,
)
# Экспорт CSV и классы физического оборудования.
from csv_export import save
from printer import Printer
from scanner import (
    ScanCancelled,
    fast_find_magnet_center,
    find_magnet_boundaries_from_grid,
    find_magnet_center,
    fit_magnet_pattern,
    scan,
    sensor_to_printer,
    validate_parameters,
)
from sensor import MagneticSensor
# SQLite-функции для страницы архива в HTML.
from storage import delete_scan, get_points, list_scans, save_scan


class HardwareWorker(QObject):
    """Фоновый поток для I²C и serial: GUI здесь никогда не блокируется."""
    # Сигналы не меняют HTML сами. Они передаются StudioWindow в GUI-поток.
    log = Signal(str, str)
    status = Signal(str, bool, str)
    measurement = Signal(dict)
    position = Signal(dict)
    read_done = Signal()
    position_done = Signal()
    progress = Signal(int, int, dict)
    scan_finished = Signal(str)
    scan_failed = Signal(str)
    # Автопоиск центра магнита идёт отдельным набором событий, чтобы не
    # переопределять обработчики обычного сканирования в HTML.
    search_progress = Signal(str, int, int, dict)
    search_finished = Signal(str)
    search_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.printer: Printer | None = None
        self.sensor: MagneticSensor | None = None
        # Event можно безопасно установить из GUI-потока, даже когда QThread
        # занят длинным scan(). Обычный queued-slot в этот момент не исполняется.
        self._cancel_event = threading.Event()
        self.scanning = False
        # До G28 прошивка не знает абсолютную точку отсчёта. Это состояние
        # защищает карту и ручные перемещения от случайного движения.
        self.printer_homed = False
        # Флаг отделяет немедленный M112 от безопасного M999. Во время скана
        # поток может быть занят ожиданием ответа на G-code, поэтому M999
        # нельзя посылать одновременно с ним.
        self.awaiting_emergency_recovery = False
        # Последние известные координаты головки принтера в абсолютной системе
        # координат Marlin. Интерфейс отображает именно их внизу окна.
        self.current_position = {"x": 0.0, "y": 0.0, "z": 0.0}

    @Slot()
    def connect_printer(self) -> None:
        """Создаёт объект Printer один раз и публикует его состояние в UI."""
        if self.printer:
            state = "откалиброван" if self.printer_homed else "подключён"
            self.status.emit("printer", True, f"{PRINTER_PORT} · {state}")
            self.position.emit(dict(self.current_position))
            return
        try:
            self.printer = Printer(PRINTER_PORT, PRINTER_BAUDRATE)
            self.printer_homed = False
            self.status.emit("printer", True, f"{PRINTER_PORT} · готов")
            self.position.emit(dict(self.current_position))
            self.log.emit("SUCCESS", "Последовательное соединение со сканером установлено")
        except Exception as error:
            self.printer_homed = False
            self.status.emit("printer", False, "Не подключён")
            self.log.emit("ERROR", f"Сканер: {error}")

    @Slot()
    def connect_sensor(self) -> None:
        """Создаёт объект MagneticSensor один раз и проверяет I²C-связь."""
        if self.sensor:
            self.status.emit("sensor", True, "MLX90393 · подключён")
            return
        try:
            self.sensor = MagneticSensor()
            self.status.emit("sensor", True, "MLX90393 · готов")
            self.log.emit("SUCCESS", "Датчик MLX90393 инициализирован")
        except Exception as error:
            self.status.emit("sensor", False, "Не подключён")
            self.log.emit("ERROR", f"MLX90393: {error}")

    @Slot()
    def home(self) -> None:
        """Вызывает G28 только после успешного подключения сканера."""
        if not self.printer:
            self.log.emit("WARNING", "Перед поиском дома подключите сканер")
            return
        try:
            self.log.emit("INFO", "Запущен поиск домашнего положения G28")
            self.printer.home()
            self.printer_homed = True
            self.current_position = {"x": 0.0, "y": 0.0, "z": 0.0}
            self.position.emit(dict(self.current_position))
            self.status.emit("printer", True, f"{PRINTER_PORT} · откалиброван")
            self.log.emit("SUCCESS", "Сканер в домашнем положении")
        except Exception as error:
            self.log.emit("ERROR", f"G28: {error}")

    @Slot()
    def motors_off(self) -> None:
        """Передаёт M84: снимает удерживающий ток, но не меняет координаты."""
        if not self.printer:
            self.log.emit("WARNING", "Перед M84 подключите сканер")
            return
        try:
            self.printer.motors_off()
            self.log.emit("INFO", "Двигатели отключены: M84")
        except Exception as error:
            self.log.emit("ERROR", f"M84: {error}")

    @Slot()
    def motors_on(self) -> None:
        """Передаёт M17: включает удержание осей, но не запускает движение."""
        if not self.printer:
            self.log.emit("WARNING", "Перед M17 подключите сканер")
            return
        try:
            self.printer.motors_on()
            self.log.emit("INFO", "Двигатели включены: M17")
        except Exception as error:
            self.log.emit("ERROR", f"M17: {error}")

    @Slot(dict)
    def manual_move(self, target: dict) -> None:
        """Ручное перемещение сканера в абсолютные координаты из HTML-карты.

        Интерфейс передаёт X/Y/Z в системе координат самого сканера, а не
        координаты центра магнита. Это делает карту прямой и понятной:
        клик в рабочей зоне сразу означает точку стола.
        """
        if not self.printer:
            self.log.emit("WARNING", "Сначала подключите сканер")
            return
        if self.scanning:
            self.log.emit("WARNING", "Во время сканирования ручное перемещение отключено")
            return
        if not self.printer_homed:
            self.log.emit("WARNING", "Перед ручным перемещением выполните G28")
            return
        try:
            x = float(target["x"])
            y = float(target["y"])
            z = float(target["z"])
        except (TypeError, ValueError, KeyError):
            self.log.emit("ERROR", "Неверные координаты ручного перемещения")
            return

        if not all(math.isfinite(value) for value in (x, y, z)):
            self.log.emit("ERROR", "Координаты должны быть конечными числами")
            return
        if not 0.0 <= x <= BED_SIZE_X:
            self.log.emit("ERROR", f"X={x:.2f} вне стола 0…{BED_SIZE_X:.0f} мм")
            return
        if not 0.0 <= y <= BED_SIZE_Y:
            self.log.emit("ERROR", f"Y={y:.2f} вне стола 0…{BED_SIZE_Y:.0f} мм")
            return
        if not 0.0 <= z <= BED_MAX_Z:
            self.log.emit("ERROR", f"Z={z:.2f} вне диапазона 0…{BED_MAX_Z:.0f} мм")
            return

        try:
            # Сначала поднимаем/опускаем Z, затем двигаем XY. Так безопаснее
            # для датчика и объекта на столе при ручном позиционировании.
            self.printer.send("G90")
            self.printer.move(z=z, speed=Z_SPEED)
            self.printer.move(x=x, y=y, speed=MOVE_SPEED)
            self.current_position = {"x": x, "y": y, "z": z}
            self.position.emit(dict(self.current_position))
            self.log.emit("SUCCESS", f"Перемещение к X{x:.2f} Y{y:.2f} Z{z:.2f}")
        except Exception as error:
            self.log.emit("ERROR", f"Перемещение: {error}")

    def request_cancel(self, *, report=True) -> None:
        """Потокобезопасно отменяет операцию, не дожидаясь очереди QThread."""
        self._cancel_event.set()
        if self.printer:
            self.printer.cancel_motion()
        if report:
            self.log.emit("WARNING", "Запрошена остановка текущего сканирования")

    def prepare_operation(self) -> None:
        """Сбрасывает отмену перед постановкой новой операции в очередь."""
        self._cancel_event.clear()

    @Slot()
    def calibrate(self) -> None:
        """Сохраняет текущий магнитный фон в MagneticSensor.zero()."""
        if not self.sensor:
            self.log.emit("WARNING", "Перед калибровкой подключите MLX90393")
            return
        try:
            self.sensor.zero()
            self.log.emit("SUCCESS", "Фоновое поле сохранено как нулевое")
        except Exception as error:
            self._sensor_failed(error, "Калибровка")

    @Slot()
    def read_sensor(self) -> None:
        """Делает одно усреднённое измерение для живых чисел интерфейса."""
        if not self.sensor or self.scanning:
            self.read_done.emit()
            return
        try:
            self.measurement.emit(self.sensor.measure())
        except Exception as error:
            self._sensor_failed(error, "Измерение")
        finally:
            self.read_done.emit()

    @Slot()
    def read_position(self) -> None:
        """Запрашивает реальные координаты сканера у прошивки через M114."""
        if not self.printer or self.scanning:
            self.position_done.emit()
            return
        try:
            self.current_position = self.printer.query_position()
            self.position.emit(dict(self.current_position))
        except TimeoutError:
            # Если один запрос позиции потерялся, не засоряем консоль и просто
            # оставляем последнее известное положение до следующего опроса.
            pass
        except Exception as error:
            self.log.emit("ERROR", f"Позиция сканера: {error}")
        finally:
            self.position_done.emit()

    def _sensor_failed(self, error: Exception, operation: str) -> None:
        """Помечает датчик недоступным после ошибки I²C и останавливает опрос.

        `Remote I/O error` означает, что Linux не получил ответ от MLX90393.
        Обнуление self.sensor не даёт таймеру 100 мс бесконечно писать ошибки.
        """
        failed_sensor, self.sensor = self.sensor, None
        if failed_sensor:
            try:
                failed_sensor.close()
            except Exception:
                pass
        self.status.emit("sensor", False, f"Ошибка I²C: {error}")
        self.log.emit("ERROR", f"{operation}: {error}")

    @Slot(dict)
    def start_scan(self, parameters: dict) -> None:
        # Этот слот выполняется в QThread. Поэтому длительный scan() не
        # замораживает HTML-интерфейс и позволяет отображать прогресс.
        if not self.printer or not self.sensor:
            self.scan_failed.emit("Перед сканированием подключите сканер и MLX90393")
            return
        try:
            parameters = validate_parameters(parameters)
        except (TypeError, ValueError) as error:
            self.scan_failed.emit(str(error))
            return
        self.scanning = True
        try:
            self.log.emit("INFO", "Поиск дома сканера и калибровка датчика")
            self.printer.home()
            self.printer_homed = True
            # Фон измеряем после homing: датчик находится в известной точке,
            # а не в случайной позиции над магнитом.
            self.sensor.zero()
            self.status.emit("printer", True, f"{PRINTER_PORT} · откалиброван")
            results = scan(
                self.printer, self.sensor, parameters, confirm=False,
                progress_callback=lambda point, total, value: self.progress.emit(point, total, value),
                cancelled=self._cancel_event.is_set,
            )
            filename = save(results)
            save_scan(filename, parameters, results)
            center_x, center_y = sensor_to_printer(
                parameters.get("center_x", 0.0), parameters.get("center_y", 0.0)
            )
            self.current_position = {
                "x": center_x,
                "y": center_y,
                "z": min(BED_MAX_Z, parameters["height"] + SCAN_HEIGHT + 20),
            }
            self.position.emit(dict(self.current_position))
            self.scan_finished.emit(filename)
            self.log.emit("SUCCESS", f"Сканирование сохранено: {filename}")
        except ScanCancelled:
            self.scan_failed.emit("Сканирование отменено оператором")
            self.log.emit("WARNING", "Сканирование отменено")
        except Exception as error:
            # Ошибка I²C во время скана также должна погасить зелёный индикатор.
            if isinstance(error, OSError):
                self._sensor_failed(error, "Сканирование")
            self.scan_failed.emit(str(error))
            self.log.emit("ERROR", f"Сканирование: {error}")
        finally:
            self.scanning = False
            self._cancel_event.clear()

    @Slot(dict)
    def start_search(self, parameters: dict) -> None:
        """Тестовая функция: сканер сам ищет центр магнита по максимуму |B|.

        Как и обычный скан, выполняется в QThread. Во время поиска flag
        self.scanning выключает живой опрос датчика и M114, чтобы они не
        мешали движению сканера. Найденный центр публикуется событием
        search_finished в системе координат измерений (0, 0 — центр стола).
        """
        if not self.printer or not self.sensor:
            self.search_failed.emit("Перед поиском центра подключите сканер и MLX90393")
            return
        try:
            height = float(parameters.get("height", 0.0))
        except (TypeError, ValueError):
            self.search_failed.emit("Высота поиска должна быть числом")
            return
        if not math.isfinite(height):
            self.search_failed.emit("Высота поиска должна быть конечным числом")
            return
        if height < 0 or height + SCAN_HEIGHT > BED_MAX_Z:
            self.search_failed.emit(
                f"Рабочая высота должна быть в диапазоне 0…{BED_MAX_Z:.0f} мм"
            )
            return
        pattern = str(parameters.get("pattern", "auto"))
        fast = bool(parameters.get("fast", True))
        self.scanning = True
        try:
            mode_desc = f"быстрый поиск (5–10 с), паттерн: {pattern}" if fast else f"полный скан колец, паттерн: {pattern}"
            self.log.emit("INFO", f"Калибровка дома и фона датчика · {mode_desc}")
            self.printer.home()
            self.printer_homed = True
            self.sensor.zero()
            self.status.emit("printer", True, f"{PRINTER_PORT} · откалиброван")
            result = find_magnet_center(
                self.printer, self.sensor, height,
                progress_callback=lambda stage, point, total, value:
                    self.search_progress.emit(stage, point, total, value),
                cancelled=self._cancel_event.is_set,
                fast=fast,
                pattern=pattern,
            )
            center_printer_x, center_printer_y = sensor_to_printer(
                result["center_x"], result["center_y"])
            self.current_position = {
                "x": center_printer_x,
                "y": center_printer_y,
                "z": min(BED_MAX_Z, result["work_z"] + 20),
            }
            self.position.emit(dict(self.current_position))
            self.search_finished.emit(json.dumps(result))
            shape_str = f", круг Ø{result.get('diameter', 0):.1f} мм" if result.get("pattern") == "circle" else (
                f", прямоугольник {result.get('width', 0):.1f}×{result.get('length', 0):.1f} мм" if result.get("pattern") == "rectangle" else ""
            )
            h_str = f", высота магнита H={result.get('detected_surface_z', 0):.1f} мм (зазор Z={result['work_z']:.1f} мм)" if "detected_surface_z" in result else ""
            dur_str = f" за {result['duration_sec']:.1f} с" if "duration_sec" in result else ""
            self.log.emit(
                "SUCCESS",
                f"Центр магнита: X{result['center_x']:+.2f} "
                f"Y{result['center_y']:+.2f} мм{shape_str}{h_str}, |B| над центром "
                f"{result['peak_b']:.2f} мТл{dur_str}",
            )

        except ScanCancelled:
            self.search_failed.emit("Автопоиск центра отменён оператором")
            self.log.emit("WARNING", "Автопоиск центра отменён")
        except Exception as error:
            if isinstance(error, OSError):
                self._sensor_failed(error, "Автопоиск центра")
            self.search_failed.emit(str(error))
            self.log.emit("ERROR", f"Автопоиск центра: {error}")
        finally:
            self.scanning = False
            self._cancel_event.clear()

    def emergency_stop_now(self) -> None:
        """Немедленно шлёт M112; восстановление M999 выполняется отдельно.

        Этот метод намеренно вызывается напрямую из GUI-потока: M112 должен
        попасть в USB-порт даже тогда, когда worker занят долгим сканом.
        Он делает только неблокирующую запись, не читает порт и не отправляет
        другие команды, поэтому не конкурирует с ожидающим `send()`.
        """
        self._cancel_event.set()
        self.printer_homed = False
        self.awaiting_emergency_recovery = True
        if self.printer:
            try:
                self.printer.stop()
                self.status.emit("printer", True, f"{PRINTER_PORT} · требуется G28")
                self.log.emit("WARNING", "Аварийная остановка M112 отправлена")
            except Exception as error:
                self.log.emit("ERROR", f"Аварийная остановка: {error}")
                return
        else:
            self.log.emit("ERROR", "Передана аварийная остановка M112")

    @Slot()
    def recover_after_emergency_stop(self) -> None:
        """Снимает emergency-state после завершения или прерывания операции."""
        if not self.awaiting_emergency_recovery or not self.printer:
            return
        try:
            self.printer.recover_after_stop()
            self.awaiting_emergency_recovery = False
            self.status.emit("printer", True, f"{PRINTER_PORT} · требуется G28")
            self.log.emit("SUCCESS", "Сканер восстановлен после M112; выполните G28")
        except Exception as error:
            self.log.emit("ERROR", f"Восстановление после M112: {error}")

    @Slot()
    def shutdown(self) -> None:
        """Закрывает serial и I²C при штатном выходе из приложения."""
        if self.printer:
            try:
                self.printer.close()
            except OSError:
                pass
            self.printer = None
        if self.sensor:
            try:
                self.sensor.close()
            except OSError:
                pass
            self.sensor = None


class Bridge(QObject):
    """Явный и небольшой API, доступный JavaScript через QWebChannel."""

    def __init__(self, window: "StudioWindow") -> None:
        super().__init__()
        self.window = window

    def _parse_json_object(self, payload: str, action: str) -> dict | None:
        """Разбирает JSON-объект или показывает понятную ошибку в консоли."""
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("ожидался JSON-объект")
            return value
        except (TypeError, ValueError) as error:
            self.window.push(
                "log", {"level": "ERROR", "message": f"Invalid {action} request: {error}"}
            )
            return None

    # Имена слотов camelCase совпадают с вызовами JavaScript в studio.html.
    # Однострочные методы ниже не делают работу сами: они только передают
    # запрос в сигнальную систему Qt, чтобы UI не ждал порт или I²C.
    @Slot()
    def connectPrinter(self) -> None: self.window.connect_printer_requested.emit()
    @Slot()
    def connectSensor(self) -> None: self.window.connect_sensor_requested.emit()
    @Slot()
    def home(self) -> None: self.window.home_requested.emit()
    @Slot()
    def motorsOff(self) -> None: self.window.motors_off_requested.emit()
    @Slot()
    def motorsOn(self) -> None: self.window.motors_on_requested.emit()
    @Slot(str)
    def moveAxes(self, payload: str) -> None:
        """Получает JSON координат и запускает ручное перемещение принтера."""
        coordinates = self._parse_json_object(payload, "move")
        if coordinates is not None:
            self.window.move_requested.emit(coordinates)
    @Slot()
    def calibrate(self) -> None: self.window.calibrate_requested.emit()
    @Slot()
    def cancelScan(self) -> None: self.window.cancel_scan()
    @Slot()
    def emergencyStop(self) -> None: self.window.emergency_stop()

    @Slot(str)
    def startScan(self, payload: str) -> None:
        """Получает JSON параметров из HTML и отправляет словарь в worker-поток."""
        parameters = self._parse_json_object(payload, "scan")
        if parameters is not None:
            self.window.start_scan(parameters)

    @Slot(str)
    def startSearch(self, payload: str) -> None:
        """Получает JSON параметров автопоиска и запускает его в worker-потоке."""
        parameters = self._parse_json_object(payload, "search")
        if parameters is not None:
            self.window.start_search(parameters)

    @Slot(float, float)
    def moveToCenter(self, center_x: float, center_y: float) -> None:
        """Перемещает сопло и датчик над найденным центром магнита."""
        printer_x, printer_y = sensor_to_printer(center_x, center_y)
        self.window.move_requested.emit({"x": printer_x, "y": printer_y, "z": 25.0})

    @Slot(str, str, result=str)
    def analyzeGrid(self, payload: str, pattern: str) -> str:
        """Мгновенно определяет границы и форму магнита по переданной сетке точек."""
        try:
            pts_data = json.loads(payload)
            points = [(float(p["x"]), float(p["y"])) for p in pts_data]
            values = [float(p["b"]) for p in pts_data]
            res = find_magnet_boundaries_from_grid(points, values, pattern=pattern)
            return json.dumps(res)
        except Exception as error:
            return json.dumps({"error": str(error)})

    @Slot(result=str)
    def archive(self) -> str:
        """Возвращает JS строку JSON со списком сканов SQLite."""
        return json.dumps([dict(item) for item in list_scans()])

    @Slot(int, result=str)
    def points(self, scan_id: int) -> str:
        """Возвращает JS строку JSON с точками одного выбранного скана."""
        return json.dumps([dict(item) for item in get_points(scan_id)])

    @Slot(int, result=str)
    def deleteScan(self, scan_id: int) -> str:
        """Удаляет скан и его точки; CSV убирается только из папки results.

        Удаление идёт синхронно: SQLite-запись маленькая, а HTML просто
        перечитает список. Возврат JSON сообщает интерфейсу, что файл тоже
        был зачищен, чтобы список не показывал «пустые» удалённые файлы.
        """
        filename = delete_scan(scan_id)
        if filename is None:
            return json.dumps({"ok": False, "message": "Скан не найден"})
        # CSV удаляем только если он действительно лежит в папке результатов.
        # Проверка родительской папки защищает от запроса на удаление
        # произвольного файла, присланного из JavaScript.
        removed_file = False
        project_root = Path(__file__).resolve().parent
        results_root = (project_root / RESULTS_FOLDER).resolve()
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(results_root)
        except ValueError:
            pass
        else:
            try:
                removed_file = candidate.is_file()
                candidate.unlink(missing_ok=True)
            except OSError:
                removed_file = False
        return json.dumps({"ok": True, "removed_file": removed_file})

    @Slot(result=str)
    def configuration(self) -> str:
        """Возвращает геометрию из config.py для точной рамки карты.

        HTML не хранит вторую копию смещений датчика: пользователь меняет
        SENSOR_OFFSET_X/Y в одном месте, а UI получает их при каждом запуске.
        """
        return json.dumps({
            "bed_x": BED_SIZE_X,
            "bed_y": BED_SIZE_Y,
            "sensor_offset_x": SENSOR_OFFSET_X,
            "sensor_offset_y": SENSOR_OFFSET_Y,
        })

    @Slot(result=str)
    def getSettings(self) -> str:
        """Возвращает текущие настройки оборудования и скорости сканера."""
        return json.dumps(self.window.get_current_settings())

    @Slot(str, result=str)
    def saveSettings(self, payload: str) -> str:
        """Сохраняет и динамически применяет настройки сканера."""
        return json.dumps(self.window.apply_settings(payload))


class TitleBar(QFrame):
    """Своя шапка frameless-окна с названием, настройками и кнопками окна."""
    def __init__(self, window: "StudioWindow") -> None:
        super().__init__(objectName="titlebar")
        self.window = window
        self.drag_origin: QPoint | None = None
        # QHBoxLayout держит бренд слева, растяжку посередине и управление
        # окном справа независимо от ширины основного WebEngine-интерфейса.
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 8, 0)
        self.brand = QPushButton("NEXUS  /  MAGNETIC FIELD STUDIO", objectName="brand")
        # SVG — локальный знак приложения; Qt масштабирует его без потери
        # чёткости на обычных и HiDPI-экранах.
        self.brand.setIcon(QIcon(str(Path(__file__).parent / "ui" / "nexus-mark.svg")))
        self.brand.setIconSize(QSize(17, 17))
        self.brand.setEnabled(False)
        row.addWidget(self.brand)
        row.addStretch()
        # Каждая кнопка вызывает нативное действие окна или событие в HTML.
        # Кортеж описывает символ, Qt-действие и всплывающую подсказку каждой
        # кнопки, чтобы не копировать одинаковый код создания QPushButton.
        for symbol, action, tip in (("⚙", window.open_settings, "Настройки"), ("—", window.showMinimized, "Свернуть"), ("□", window.toggle_maximize, "Развернуть"), ("×", window.close, "Закрыть")):
            button = QPushButton(symbol, objectName="windowControl")
            button.setToolTip(tip)
            button.clicked.connect(action)
            row.addWidget(button)

    def mousePressEvent(self, event) -> None:
        # startSystemMove передаёт перетаскивание оконному менеджеру Ubuntu.
        # Поэтому работают прилипание к краям, разворачивание и рабочие
        # пространства — как у обычных нативных программ.
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window.windowHandle()
            if handle and handle.startSystemMove():
                event.accept()
                return
            # Редкий запасной путь для окружений без system move.
            self.drag_origin = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self.drag_origin)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_origin = None

    def mouseDoubleClickEvent(self, event) -> None:
        """Двойной клик по пустой части шапки разворачивает окно, как в Ubuntu."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.toggle_maximize()
            event.accept()


class StudioWindow(QMainWindow):
    """Главное окно: соединяет нативную Qt-оболочку, WebEngine и оборудование."""
    # Сигналы описывают команды из GUI в worker. Благодаря им выполнение
    # оказывается в QThread, а не в потоке рисования Qt.
    connect_printer_requested = Signal()
    connect_sensor_requested = Signal()
    home_requested = Signal()
    motors_off_requested = Signal()
    motors_on_requested = Signal()
    move_requested = Signal(dict)
    calibrate_requested = Signal()
    recover_requested = Signal()
    shutdown_requested = Signal()
    read_requested = Signal()
    position_requested = Signal()
    scan_requested = Signal(dict)
    search_requested = Signal(dict)
    # Толщина зоны захвата за границей контента. Семь пикселей достаточно для
    # удобного изменения размера, но курсор не цепляет элементы интерфейса.
    RESIZE_MARGIN = 7

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        # Интерфейс умеет перестраиваться в узкий режим CSS-медиазапросами,
        # поэтому окно можно сужать заметно сильнее прежних 1060 px.
        self.setMinimumSize(860, 560)
        self.resize(1540, 930)
        # Защита от очереди из сотен запросов: пока датчик отвечает, новый
        # запрос от таймера 100 мс не создаётся.
        self.measurement_pending = False
        self.position_pending = False
        self._connected = {"printer": False, "sensor": False}
        self._operation_active = False
        # Состояние ручного resize используется только как запасной вариант,
        # если оконный менеджер не поддерживает QWindow.startSystemResize().
        self._resize_edges = Qt.Edge(0)
        self._resize_origin: QPoint | None = None
        self._resize_geometry: QRect | None = None
        self._build_worker()
        root = QWidget(objectName="nativeRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(TitleBar(self))
        self.web = QWebEngineView()
        # UI и будущие графики лежат в проекте. Запрет внешних ресурсов убирает
        # лишние сетевые обращения Chromium и делает запуск предсказуемее.
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.channel = QWebChannel(self.web.page())
        self.bridge = Bridge(self)
        self.channel.registerObject("bridge", self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.web.setUrl(QUrl.fromLocalFile(str(Path(__file__).parent / "ui" / "studio.html")))
        layout.addWidget(self.web, 1)
        self.setCentralWidget(root)
        # Frameless-окно не получает события мыши от вложенного WebEngine.
        # Фильтр видит их до Chromium и обрабатывает только узкую кромку окна.
        root.installEventFilter(self)
        self.web.installEventFilter(self)
        # Два отдельных таймера легче настраивать и проще контролировать:
        # датчик читаем немного чаще, координаты принтера — заметно реже.
        self.live_timer = QTimer(self)
        # 4 измерения в секунду достаточно для живых чисел без движения, но
        # заметно бережнее для I²C и Raspberry Pi, чем прежние 6.25 Гц.
        self.live_timer.setInterval(250)
        self.live_timer.timeout.connect(self._poll_live_data)
        self.live_timer.start()
        self.position_timer = QTimer(self)
        # M114 идёт через тот же serial-порт, что и G-code. Редкий опрос не
        # забивает очередь прошивки; ручные движения всё равно обновляют XYZ
        # сразу после завершения команды.
        self.position_timer.setInterval(750)
        self.position_timer.timeout.connect(self._poll_position)
        self.position_timer.start()
        # После старта окна сразу пробуем подключить принтер и датчик.
        QTimer.singleShot(350, self._auto_connect_devices)

    def _build_worker(self) -> None:
        # Все сигналы подключены к методам окна, а не к lambda: так Qt ставит
        # обновление WebEngine в главный поток и приложение не падает.
        self.thread = QThread(self)
        self.worker = HardwareWorker()
        self.worker.moveToThread(self.thread)
        self.connect_printer_requested.connect(self.worker.connect_printer)
        self.connect_sensor_requested.connect(self.worker.connect_sensor)
        self.home_requested.connect(self.worker.home)
        self.motors_off_requested.connect(self.worker.motors_off)
        self.motors_on_requested.connect(self.worker.motors_on)
        self.move_requested.connect(self.worker.manual_move)
        self.calibrate_requested.connect(self.worker.calibrate)
        self.recover_requested.connect(self.worker.recover_after_emergency_stop)
        self.shutdown_requested.connect(self.worker.shutdown)
        self.read_requested.connect(self.worker.read_sensor)
        self.position_requested.connect(self.worker.read_position)
        self.scan_requested.connect(self.worker.start_scan)
        self.search_requested.connect(self.worker.start_search)
        self.worker.log.connect(self._handle_log)
        self.worker.status.connect(self._handle_status)
        self.worker.measurement.connect(self._measurement)
        self.worker.position.connect(self._position)
        self.worker.read_done.connect(self._read_done)
        self.worker.position_done.connect(self._position_done)
        self.worker.progress.connect(self._progress)
        self.worker.scan_finished.connect(self._scan_finished)
        self.worker.scan_failed.connect(self._scan_failed)
        self.worker.search_progress.connect(self._search_progress)
        self.worker.search_finished.connect(self._search_finished)
        self.worker.search_failed.connect(self._search_failed)
        self.thread.start()

    def push(self, event: str, payload: dict) -> None:
        # Передаём событие в JS как JSON — так строки и числа безопасно экранируются.
        script = f"window.NexusApp && window.NexusApp.receive({json.dumps(event)}, {json.dumps(payload)});"
        self.web.page().runJavaScript(script)

    def _auto_connect_devices(self) -> None:
        """Пробует подключить принтер и MLX90393 сразу после запуска окна."""
        self.connect_printer_requested.emit()
        self.connect_sensor_requested.emit()

    def _poll_live_data(self) -> None:
        """Периодически обновляет датчик и координаты принтера без блокировки UI."""
        # Не ставим новое чтение, пока не завершилось предыдущее измерение.
        if self._connected["sensor"] and not self._operation_active and not self.measurement_pending:
            self.measurement_pending = True
            self.read_requested.emit()

    def _poll_position(self) -> None:
        """Опрос текущей позиции принтера отдельным редким таймером."""
        if self._connected["printer"] and not self._operation_active and not self.position_pending:
            self.position_pending = True
            self.position_requested.emit()

    def _measurement(self, value: dict) -> None:
        """Передаёт свежие Bx/By/Bz/|B| из worker-потока в JavaScript."""
        self.push("measurement", value)  # JavaScript обновит Bx/By/Bz/|B|.

    @Slot(dict)
    def _position(self, value: dict) -> None:
        """Передаёт интерфейсу последние известные координаты принтера."""
        self.push("position", value)

    @Slot()
    def _read_done(self) -> None:
        # Следующий тик live_timer теперь может поставить новое I²C-чтение.
        self.measurement_pending = False

    @Slot()
    def _position_done(self) -> None:
        # Аналогично снимаем блокировку следующего запроса M114.
        self.position_pending = False

    @Slot(str, str)
    def _handle_log(self, level: str, message: str) -> None:
        self.push("log", {"level": level, "message": message})

    @Slot(str, bool, str)
    def _handle_status(self, device: str, connected: bool, text: str) -> None:
        if device in self._connected:
            self._connected[device] = connected
        self.push("status", {"device": device, "connected": connected, "text": text})

    @Slot(str)
    def _scan_finished(self, filename: str) -> None:
        self._operation_active = False
        self.push("scanFinished", {"filename": filename})
        self.recover_requested.emit()

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self._operation_active = False
        self.push("scanFailed", {"message": message})
        self.recover_requested.emit()

    def _progress(self, point: int, total: int, value: dict) -> None:
        """Передаёт HTML номер точки, общее число точек и последнее значение."""
        self.push("progress", {"point": point, "total": total, "value": value})

    def _search_progress(self, stage: str, point: int, total: int, value: dict) -> None:
        """Передаёт HTML этап автопоиска, номер точки и последнее значение."""
        self.push("searchProgress", {
            "stage": stage, "point": point, "total": total, "value": value,
        })

    def _search_finished(self, payload: str) -> None:
        """Показывает в HTML найденный центр магнита и возвращает worker в строй."""
        self._operation_active = False
        self.push("searchFinished", json.loads(payload))
        self.recover_requested.emit()

    def _search_failed(self, message: str) -> None:
        """Сообщает HTML об ошибке или отмене автопоиска."""
        self._operation_active = False
        self.push("searchFailed", {"message": message})
        self.recover_requested.emit()

    def emergency_stop(self) -> None:
        """Точка входа M112, вызываемая кнопкой HTML через Bridge."""
        self.worker.emergency_stop_now()
        # Если M112 был нажата вне сканирования, worker уже свободен. При
        # сканировании сигнал дождётся завершения слота в QThread.
        QTimer.singleShot(650, self.recover_requested.emit)

    def cancel_scan(self) -> None:
        """Останавливает текущее сканирование без аварийной блокировки сканера."""
        self.worker.request_cancel()

    def get_current_settings(self) -> dict:
        """Возвращает словарь текущих настроек оборудования и динамики."""
        return {
            "scan_speed": int(getattr(config, "SCAN_SPEED", 6000)),
            "move_speed": int(getattr(config, "MOVE_SPEED", 8000)),
            "z_speed": int(getattr(config, "Z_SPEED", 1500)),
            "settle_time": float(getattr(config, "POINT_SETTLE_TIME", 0.10)),
            "samples_per_point": int(getattr(config, "SAMPLES_PER_POINT", 1)),
            "scan_height": float(getattr(config, "SCAN_HEIGHT", 5.0)),
            "sensor_timeout": float(getattr(config, "SENSOR_RECONNECT_TIMEOUT", 5.0)),
            "printer_port": str(getattr(config, "PRINTER_PORT", "/dev/ttyUSB0")),
            "baudrate": int(getattr(config, "PRINTER_BAUDRATE", 115200)),
            "i2c_bus": int(getattr(config, "I2C_BUS", 1)),
            "sensor_address": hex(getattr(config, "MLX90393_ADDRESS", 0x0C)),
        }

    def apply_settings(self, payload: str) -> dict:
        """Применяет пользовательские настройки к config.py в рантайме."""
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            if "scan_speed" in data:
                config.SCAN_SPEED = max(500, min(15000, int(data["scan_speed"])))
            if "move_speed" in data:
                config.MOVE_SPEED = max(1000, min(15000, int(data["move_speed"])))
            if "z_speed" in data:
                config.Z_SPEED = max(200, min(5000, int(data["z_speed"])))
            if "settle_time" in data:
                config.POINT_SETTLE_TIME = max(0.005, min(2.0, float(data["settle_time"])))
            if "samples_per_point" in data:
                config.SAMPLES_PER_POINT = max(1, min(20, int(data["samples_per_point"])))
            if "scan_height" in data:
                config.SCAN_HEIGHT = max(0.5, min(50.0, float(data["scan_height"])))
            if "sensor_timeout" in data:
                config.SENSOR_RECONNECT_TIMEOUT = max(0.5, min(30.0, float(data["sensor_timeout"])))

            self.push("log", {"level": "SUCCESS", "message": f"Настройки сканера сохранены: скорость {config.SCAN_SPEED} мм/мин, стабилизация {config.POINT_SETTLE_TIME:.2f} с"})
            return {"ok": True, "settings": self.get_current_settings()}
        except Exception as error:
            self.push("log", {"level": "ERROR", "message": f"Ошибка сохранения настроек: {error}"})
            return {"ok": False, "error": str(error)}

    def start_scan(self, parameters: dict) -> None:
        """Приостанавливает фоновые опросы и запускает площадной скан."""
        if self._operation_active:
            self.push("log", {"level": "WARNING", "message": "Операция уже выполняется"})
            return
        parameters.setdefault("scan_speed", getattr(config, "SCAN_SPEED", 6000))
        parameters.setdefault("move_speed", getattr(config, "MOVE_SPEED", 8000))
        parameters.setdefault("z_speed", getattr(config, "Z_SPEED", 1500))
        parameters.setdefault("settle_time", getattr(config, "POINT_SETTLE_TIME", 0.10))
        parameters.setdefault("samples_per_point", getattr(config, "SAMPLES_PER_POINT", 1))
        parameters.setdefault("scan_height", getattr(config, "SCAN_HEIGHT", 5.0))
        self.worker.prepare_operation()
        self._operation_active = True
        self.measurement_pending = False
        self.position_pending = False
        self.scan_requested.emit(parameters)

    def start_search(self, parameters: dict) -> None:
        """Приостанавливает фоновые опросы и запускает поиск центра."""
        if self._operation_active:
            self.push("log", {"level": "WARNING", "message": "Операция уже выполняется"})
            return
        self.worker.prepare_operation()
        self._operation_active = True
        self.measurement_pending = False
        self.position_pending = False
        self.search_requested.emit(parameters)

    def open_settings(self) -> None:
        self.push("navigate", {"page": "settings"})

    def toggle_maximize(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _resize_edges_at(self, global_position: QPoint) -> Qt.Edge:
        """Определяет край или угол окна под курсором в глобальных координатах."""
        if self.isMaximized():
            return Qt.Edge(0)
        frame = self.frameGeometry()
        margin = self.RESIZE_MARGIN
        edges = Qt.Edge(0)
        if global_position.x() <= frame.left() + margin:
            edges |= Qt.Edge.LeftEdge
        elif global_position.x() >= frame.right() - margin:
            edges |= Qt.Edge.RightEdge
        if global_position.y() <= frame.top() + margin:
            edges |= Qt.Edge.TopEdge
        elif global_position.y() >= frame.bottom() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _resize_cursor(edges: Qt.Edge) -> Qt.CursorShape:
        """Возвращает привычную форму курсора для конкретного края или угла."""
        left_right = Qt.Edge.LeftEdge | Qt.Edge.RightEdge
        top_bottom = Qt.Edge.TopEdge | Qt.Edge.BottomEdge
        if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        if edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        if edges in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge):
            return Qt.CursorShape.SizeBDiagCursor
        return Qt.CursorShape.ArrowCursor

    def _resize_manually(self, global_position: QPoint) -> None:
        """Меняет QRect только когда compositor не предоставил system resize."""
        if not self._resize_geometry or not self._resize_origin:
            return
        delta = global_position - self._resize_origin
        geometry = QRect(self._resize_geometry)
        minimum_width = self.minimumWidth()
        minimum_height = self.minimumHeight()
        if self._resize_edges & Qt.Edge.LeftEdge:
            geometry.setLeft(min(geometry.right() - minimum_width + 1, geometry.left() + delta.x()))
        if self._resize_edges & Qt.Edge.RightEdge:
            geometry.setRight(max(geometry.left() + minimum_width - 1, geometry.right() + delta.x()))
        if self._resize_edges & Qt.Edge.TopEdge:
            geometry.setTop(min(geometry.bottom() - minimum_height + 1, geometry.top() + delta.y()))
        if self._resize_edges & Qt.Edge.BottomEdge:
            geometry.setBottom(max(geometry.top() + minimum_height - 1, geometry.bottom() + delta.y()))
        self.setGeometry(geometry)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Даёт resize всем краям поверх Qt-контента и встроенного Chromium."""
        if watched not in (self.centralWidget(), self.web):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseMove:
            mouse = event  # QMouseEvent; Qt передаёт globalPosition().
            global_position = mouse.globalPosition().toPoint()
            if self._resize_edges and mouse.buttons() & Qt.MouseButton.LeftButton:
                self._resize_manually(global_position)
                return True
            edges = self._resize_edges_at(global_position)
            self.setCursor(self._resize_cursor(edges))
        elif event.type() == QEvent.Type.MouseButtonPress:
            mouse = event
            if mouse.button() == Qt.MouseButton.LeftButton:
                edges = self._resize_edges_at(mouse.globalPosition().toPoint())
                if edges:
                    handle = self.windowHandle()
                    if handle and handle.startSystemResize(edges):
                        return True
                    self._resize_edges = edges
                    self._resize_origin = mouse.globalPosition().toPoint()
                    self._resize_geometry = self.geometry()
                    return True
        elif event.type() == QEvent.Type.MouseButtonRelease:
            if self._resize_edges:
                self._resize_edges = Qt.Edge(0)
                self._resize_origin = None
                self._resize_geometry = None
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:
        """Останавливает опросы и освобождает serial/I²C перед выходом."""
        self.live_timer.stop()
        self.position_timer.stop()
        if self._operation_active:
            self.worker.request_cancel(report=False)
        self.shutdown_requested.emit()
        self.thread.quit()
        self.thread.wait(1200)
        event.accept()


def main() -> int:
    """Создаёт QApplication, применяет стиль нативной шапки и показывает окно."""
    app = QApplication(sys.argv)
    app.setApplicationName(PROJECT_NAME)
    app.setStyleSheet((Path(__file__).parent / "ui" / "native.qss").read_text(encoding="utf-8"))
    window = StudioWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
