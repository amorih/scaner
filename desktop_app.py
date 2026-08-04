"""Главный запуск desktop-приложения NEXUS Studio.

Здесь нет формул магнитного поля и G-code: модуль связывает готовые модули
оборудования с HTML-интерфейсом через Qt WebEngine и QWebChannel.
"""

from __future__ import annotations  # Разрешает использовать типы классов до их объявления.

# json превращает словари Python в безопасные данные для JavaScript и обратно.
import json
# sys передаёт Qt аргументы запуска и возвращает код завершения приложения.
import sys
# Path формирует абсолютный путь к локальным файлам UI рядом с этим модулем.
from pathlib import Path

# Базовые объекты Qt: сигналы, поток, таймер, URL и флаги окна.
from PySide6.QtCore import QObject, QPoint, QSize, QThread, QTimer, QUrl, Signal, Slot, Qt
# QWebChannel публикует объект Bridge в JavaScript как `bridge`.
from PySide6.QtWebChannel import QWebChannel
# Настройки и виджет встроенного Chromium-движка для HTML-интерфейса.
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
# Нативные Qt-виджеты нужны только для оболочки, шапки и размещения WebEngine.
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMainWindow, QPushButton, QVBoxLayout, QWidget

# Настройки оборудования, общие для GUI и остальных Python-модулей.
from config import (
    BED_SIZE_X,
    BED_SIZE_Y,
    MOVE_SPEED,
    PRINTER_BAUDRATE,
    PRINTER_PORT,
    PROJECT_NAME,
    PROJECT_VERSION,
    SCAN_HEIGHT,
    SENSOR_OFFSET_X,
    SENSOR_OFFSET_Y,
    Z_SPEED,
)
# Экспорт CSV и классы физического оборудования.
from csv_export import save
from printer import Printer
from scanner import ScanCancelled, scan, sensor_to_printer
from sensor import MagneticSensor
# SQLite-функции для страницы архива в HTML.
from storage import get_points, list_scans, save_scan


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

    def __init__(self) -> None:
        super().__init__()
        self.printer: Printer | None = None
        self.sensor: MagneticSensor | None = None
        self.cancel_requested = False
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
            self.log.emit("SUCCESS", "Последовательное соединение с принтером установлено")
        except Exception as error:
            self.printer_homed = False
            self.status.emit("printer", False, "Не подключён")
            self.log.emit("ERROR", f"Принтер: {error}")

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
        """Вызывает G28 только после успешного подключения принтера."""
        if not self.printer:
            self.log.emit("WARNING", "Перед поиском дома подключите принтер")
            return
        try:
            self.log.emit("INFO", "Запущен поиск домашнего положения G28")
            self.printer.home()
            self.printer_homed = True
            self.current_position = {"x": 0.0, "y": 0.0, "z": 0.0}
            self.position.emit(dict(self.current_position))
            self.status.emit("printer", True, f"{PRINTER_PORT} · откалиброван")
            self.log.emit("SUCCESS", "Принтер в домашнем положении")
        except Exception as error:
            self.log.emit("ERROR", f"G28: {error}")

    @Slot()
    def motors_off(self) -> None:
        """Передаёт M84: снимает удерживающий ток, но не меняет координаты."""
        if self.printer:
            self.printer.motors_off()
            self.log.emit("INFO", "Двигатели отключены: M84")

    @Slot()
    def motors_on(self) -> None:
        """Передаёт M17: включает удержание осей, но не запускает движение."""
        if self.printer:
            self.printer.motors_on()
            self.log.emit("INFO", "Двигатели включены: M17")

    @Slot(dict)
    def manual_move(self, target: dict) -> None:
        """Ручное перемещение принтера в абсолютные координаты из HTML-карты.

        Интерфейс передаёт X/Y/Z в системе координат самого принтера, а не
        координаты центра магнита. Это делает карту прямой и понятной:
        клик в рабочей зоне сразу означает точку стола.
        """
        if not self.printer:
            self.log.emit("WARNING", "Сначала подключите принтер")
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

        if not 0.0 <= x <= BED_SIZE_X:
            self.log.emit("ERROR", f"X={x:.2f} вне стола 0…{BED_SIZE_X:.0f} мм")
            return
        if not 0.0 <= y <= BED_SIZE_Y:
            self.log.emit("ERROR", f"Y={y:.2f} вне стола 0…{BED_SIZE_Y:.0f} мм")
            return
        if z < 0.0:
            self.log.emit("ERROR", "Z не может быть меньше 0 мм")
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

    @Slot()
    def cancel_scan(self) -> None:
        """Обычная отмена сканирования без ввода принтера в emergency state."""
        self.cancel_requested = True
        if self.printer:
            self.printer.cancel_motion()
        self.log.emit("WARNING", "Запрошена остановка текущего сканирования")

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
            return
        try:
            self.measurement.emit(self.sensor.measure())
        except Exception as error:
            self._sensor_failed(error, "Измерение")
        finally:
            self.read_done.emit()

    @Slot()
    def read_position(self) -> None:
        """Запрашивает реальные координаты принтера у прошивки через M114."""
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
            self.log.emit("ERROR", f"Позиция принтера: {error}")
        finally:
            self.position_done.emit()

    def _sensor_failed(self, error: Exception, operation: str) -> None:
        """Помечает датчик недоступным после ошибки I²C и останавливает опрос.

        `Remote I/O error` означает, что Linux не получил ответ от MLX90393.
        Обнуление self.sensor не даёт таймеру 100 мс бесконечно писать ошибки.
        """
        self.sensor = None
        self.status.emit("sensor", False, f"Ошибка I²C: {error}")
        self.log.emit("ERROR", f"{operation}: {error}")

    @Slot(dict)
    def start_scan(self, parameters: dict) -> None:
        # Этот слот выполняется в QThread. Поэтому длительный scan() не
        # замораживает HTML-интерфейс и позволяет отображать прогресс.
        if not self.printer or not self.sensor:
            self.scan_failed.emit("Перед сканированием подключите принтер и MLX90393")
            return
        self.scanning = True
        self.cancel_requested = False
        try:
            self.log.emit("INFO", "Калибровка датчика и поиск дома принтера")
            self.sensor.zero()
            self.printer.home()
            self.printer_homed = True
            self.status.emit("printer", True, f"{PRINTER_PORT} · откалиброван")
            results = scan(
                self.printer, self.sensor, parameters, confirm=False,
                progress_callback=lambda point, total, value: self.progress.emit(point, total, value),
                cancelled=lambda: self.cancel_requested,
            )
            filename = save(results)
            save_scan(filename, parameters, results)
            center_x, center_y = sensor_to_printer(0, 0)
            self.current_position = {
                "x": center_x,
                "y": center_y,
                "z": parameters["height"] + SCAN_HEIGHT + 20,
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

    def emergency_stop_now(self) -> None:
        """Немедленно шлёт M112; восстановление M999 выполняется отдельно.

        Этот метод намеренно вызывается напрямую из GUI-потока: M112 должен
        попасть в USB-порт даже тогда, когда worker занят долгим сканом.
        Он делает только неблокирующую запись, не читает порт и не отправляет
        другие команды, поэтому не конкурирует с ожидающим `send()`.
        """
        self.cancel_requested = True
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
            self.log.emit("SUCCESS", "Принтер восстановлен после M112; выполните G28")
        except Exception as error:
            self.log.emit("ERROR", f"Восстановление после M112: {error}")


class Bridge(QObject):
    """Явный и небольшой API, доступный JavaScript через QWebChannel."""

    def __init__(self, window: "StudioWindow") -> None:
        super().__init__()
        self.window = window

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
        try:
            coordinates = json.loads(payload)
            self.window.move_requested.emit(coordinates)
        except (TypeError, ValueError) as error:
            # Некорректный JSON нельзя отправлять в worker: это была бы
            # потенциально опасная команда движения с неполными координатами.
            self.window.push("log", {"level": "ERROR", "message": f"Invalid move request: {error}"})
    @Slot()
    def calibrate(self) -> None: self.window.calibrate_requested.emit()
    @Slot()
    def cancelScan(self) -> None: self.window.cancel_scan()
    @Slot()
    def emergencyStop(self) -> None: self.window.emergency_stop()

    @Slot(str)
    def startScan(self, payload: str) -> None:
        """Получает JSON параметров из HTML и отправляет словарь в worker-поток."""
        try:
            parameters = json.loads(payload)
            self.window.scan_requested.emit(parameters)
        except (TypeError, ValueError) as error:
            # Ошибка парсинга показывается в консоли HTML вместо падения GUI.
            self.window.push("log", {"level": "ERROR", "message": f"Invalid scan request: {error}"})

    @Slot(result=str)
    def archive(self) -> str:
        """Возвращает JS строку JSON со списком сканов SQLite."""
        return json.dumps([dict(item) for item in list_scans()])

    @Slot(int, result=str)
    def points(self, scan_id: int) -> str:
        """Возвращает JS строку JSON с точками одного выбранного скана."""
        return json.dumps([dict(item) for item in get_points(scan_id)])

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
        # Frameless-окно не имеет системной шапки, поэтому перемещаем его сами.
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self.drag_origin)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_origin = None


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
    cancel_scan_requested = Signal()
    recover_requested = Signal()
    read_requested = Signal()
    position_requested = Signal()
    scan_requested = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setMinimumSize(1060, 680)
        self.resize(1540, 930)
        # Защита от очереди из сотен запросов: пока датчик отвечает, новый
        # запрос от таймера 100 мс не создаётся.
        self.measurement_pending = False
        self.position_pending = False
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
        self.cancel_scan_requested.connect(self.worker.cancel_scan)
        self.recover_requested.connect(self.worker.recover_after_emergency_stop)
        self.read_requested.connect(self.worker.read_sensor)
        self.position_requested.connect(self.worker.read_position)
        self.scan_requested.connect(self.worker.start_scan)
        self.worker.log.connect(self._handle_log)
        self.worker.status.connect(self._handle_status)
        self.worker.measurement.connect(self._measurement)
        self.worker.position.connect(self._position)
        self.worker.read_done.connect(self._read_done)
        self.worker.position_done.connect(self._position_done)
        self.worker.progress.connect(self._progress)
        self.worker.scan_finished.connect(self._scan_finished)
        self.worker.scan_failed.connect(self._scan_failed)
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
        if self.worker.sensor and not self.worker.scanning and not self.measurement_pending:
            self.measurement_pending = True
            self.read_requested.emit()

    def _poll_position(self) -> None:
        """Опрос текущей позиции принтера отдельным редким таймером."""
        if self.worker.printer and not self.worker.scanning and not self.position_pending:
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
        self.push("status", {"device": device, "connected": connected, "text": text})

    @Slot(str)
    def _scan_finished(self, filename: str) -> None:
        self.push("scanFinished", {"filename": filename})
        self.recover_requested.emit()

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self.push("scanFailed", {"message": message})
        self.recover_requested.emit()

    def _progress(self, point: int, total: int, value: dict) -> None:
        """Передаёт HTML номер точки, общее число точек и последнее значение."""
        self.push("progress", {"point": point, "total": total, "value": value})

    def emergency_stop(self) -> None:
        """Точка входа M112, вызываемая кнопкой HTML через Bridge."""
        self.worker.emergency_stop_now()
        # Если M112 был нажата вне сканирования, worker уже свободен. При
        # сканировании сигнал дождётся завершения слота в QThread.
        QTimer.singleShot(650, self.recover_requested.emit)

    def cancel_scan(self) -> None:
        """Останавливает текущее сканирование без аварийной блокировки принтера."""
        self.cancel_scan_requested.emit()

    def open_settings(self) -> None:
        self.push("navigate", {"page": "settings"})

    def toggle_maximize(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def closeEvent(self, event) -> None:
        """При закрытии просто завершает поток, не оставляя M112 после выхода."""
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
