"""Responsive desktop application for the magnetic-field scanner."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QProgressBar, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from config import PRINTER_BAUDRATE, PRINTER_PORT, PROJECT_NAME, PROJECT_VERSION
from csv_export import save
from printer import Printer
from scanner import ScanCancelled, scan
from sensor import MagneticSensor
from storage import get_points, list_scans, save_scan


class HardwareWorker(QObject):
    """All serial, I²C and long scan operations run outside the UI thread."""

    log = Signal(str)
    printer_state = Signal(bool, str)
    sensor_state = Signal(bool, str)
    measurement = Signal(dict)
    scan_progress = Signal(int, int, dict)
    scan_finished = Signal(str)
    scan_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.printer: Printer | None = None
        self.sensor: MagneticSensor | None = None
        self.cancel_requested = False

    @Slot()
    def connect_printer(self) -> None:
        if self.printer:
            self.printer_state.emit(True, "Подключен")
            return
        try:
            self.printer = Printer(PRINTER_PORT, PRINTER_BAUDRATE)
            self.printer_state.emit(True, f"{PRINTER_PORT} · подключен")
            self.log.emit("Принтер подключен. Координаты не изменялись.")
        except Exception as error:
            self.printer_state.emit(False, "Не подключен")
            self.log.emit(f"Ошибка принтера: {error}")

    @Slot()
    def connect_sensor(self) -> None:
        if self.sensor:
            self.sensor_state.emit(True, "Подключен")
            return
        try:
            self.sensor = MagneticSensor()
            self.sensor_state.emit(True, "MLX90393 · подключен")
            self.log.emit("Датчик MLX90393 подключен.")
        except Exception as error:
            self.sensor_state.emit(False, "Не подключен")
            self.log.emit(f"Ошибка датчика: {error}")

    @Slot()
    def home(self) -> None:
        if not self._printer_ready():
            return
        try:
            self.log.emit("Выполняется G28.")
            self.printer.home()
            self.log.emit("Принтер в домашнем положении.")
        except Exception as error:
            self.log.emit(f"Ошибка G28: {error}")

    @Slot()
    def motors_off(self) -> None:
        if not self._printer_ready():
            return
        try:
            self.printer.motors_off()
            self.log.emit("Двигатели отключены (M84).")
        except Exception as error:
            self.log.emit(f"Ошибка M84: {error}")

    @Slot()
    def calibrate_sensor(self) -> None:
        if not self._sensor_ready():
            return
        try:
            self.sensor.zero()
            self.log.emit("Нулевая точка датчика сохранена.")
        except Exception as error:
            self.log.emit(f"Ошибка калибровки: {error}")

    @Slot()
    def read_sensor(self) -> None:
        if not self._sensor_ready():
            return
        try:
            value = self.sensor.measure()
            self.measurement.emit(value)
            self.log.emit("Получено контрольное измерение.")
        except Exception as error:
            self.log.emit(f"Ошибка измерения: {error}")

    @Slot(dict)
    def start_scan(self, parameters: dict) -> None:
        if not self._printer_ready() or not self._sensor_ready():
            self.scan_failed.emit("Подключите принтер и датчик перед сканированием.")
            return
        try:
            self.cancel_requested = False
            self.log.emit("Калибровка датчика и возврат домой…")
            self.sensor.zero()
            self.printer.home()
            results = scan(
                self.printer, self.sensor, parameters, confirm=False,
                progress_callback=lambda point, total, value: self.scan_progress.emit(point, total, value),
                cancelled=lambda: self.cancel_requested,
            )
            filename = save(results)
            save_scan(filename, parameters, results)
            self.scan_finished.emit(filename)
            self.log.emit("Сканирование завершено. CSV и архив сохранены.")
        except ScanCancelled:
            self.scan_failed.emit("Сканирование остановлено.")
            self.log.emit("Сканирование отменено.")
        except Exception as error:
            self.scan_failed.emit(str(error))
            self.log.emit(f"Ошибка сканирования: {error}")

    def emergency_stop_now(self) -> None:
        """M112 is deliberately immediate and does not wait for the worker queue."""
        self.cancel_requested = True
        if self.printer:
            self.printer.stop()
        self.log.emit("Отправлена аварийная остановка M112.")

    def _printer_ready(self) -> bool:
        if self.printer:
            return True
        self.log.emit("Сначала подключите принтер.")
        return False

    def _sensor_ready(self) -> bool:
        if self.sensor:
            return True
        self.log.emit("Сначала подключите датчик.")
        return False


class ScannerWindow(QMainWindow):
    connect_printer_requested = Signal()
    connect_sensor_requested = Signal()
    home_requested = Signal()
    motors_off_requested = Signal()
    calibrate_requested = Signal()
    measure_requested = Signal()
    scan_requested = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{PROJECT_NAME} — Studio")
        self.resize(1280, 790)
        self._build_worker()
        self._build_ui()
        self._load_archive()

    def _build_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = HardwareWorker()
        self.worker.moveToThread(self.thread)
        self.connect_printer_requested.connect(self.worker.connect_printer)
        self.connect_sensor_requested.connect(self.worker.connect_sensor)
        self.home_requested.connect(self.worker.home)
        self.motors_off_requested.connect(self.worker.motors_off)
        self.calibrate_requested.connect(self.worker.calibrate_sensor)
        self.measure_requested.connect(self.worker.read_sensor)
        self.scan_requested.connect(self.worker.start_scan)
        self.worker.log.connect(self._append_log)
        self.worker.printer_state.connect(self._set_printer_state)
        self.worker.sensor_state.connect(self._set_sensor_state)
        self.worker.measurement.connect(self._show_measurement)
        self.worker.scan_progress.connect(self._show_progress)
        self.worker.scan_finished.connect(self._scan_finished)
        self.worker.scan_failed.connect(self._scan_failed)
        self.thread.start()

    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._dashboard_page())
        self.pages.addWidget(self._scan_page())
        self.pages.addWidget(self._calibration_page())
        self.pages.addWidget(self._archive_page())
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)
        layout.addWidget(self._statusbar())

    def _header(self) -> QWidget:
        bar = QFrame(objectName="header")
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 7, 18, 7)
        row.addWidget(QLabel("◆  MAGNET SCANNER", objectName="logo"))
        row.addSpacing(18)
        for text, page in (("Рабочая область", 0), ("Сканирование", 1), ("Калибровка", 2), ("Архив", 3)):
            button = QPushButton(text, objectName="nav")
            button.clicked.connect(lambda checked=False, index=page: self._show_page(index))
            row.addWidget(button)
        row.addStretch()
        row.addWidget(QLabel(f"{PROJECT_NAME} · {PROJECT_VERSION}", objectName="version"))
        return bar

    def _sidebar(self) -> QWidget:
        panel = QFrame(objectName="sidebar")
        panel.setFixedWidth(292)
        column = QVBoxLayout(panel)
        column.setContentsMargins(14, 16, 14, 16)
        column.setSpacing(9)
        column.addWidget(self._section("ПОДКЛЮЧЕНИЯ"))
        self.printer_label = self._state_label("Принтер", "Не подключен")
        self.sensor_label = self._state_label("Датчик", "Не подключен")
        column.addWidget(self.printer_label)
        column.addWidget(self._button("Подключить принтер", self.connect_printer_requested.emit))
        column.addWidget(self.sensor_label)
        column.addWidget(self._button("Подключить MLX90393", self.connect_sensor_requested.emit))
        column.addSpacing(9)
        column.addWidget(self._section("ПРИНТЕР"))
        column.addWidget(self._button("Домой · G28", self.home_requested.emit))
        column.addWidget(self._button("Отключить двигатели · M84", self.motors_off_requested.emit))
        stop = self._button("Стоп · M112", self._emergency_stop)
        stop.setObjectName("emergency")
        column.addWidget(stop)
        column.addSpacing(9)
        column.addWidget(self._section("ЖУРНАЛ"))
        self.log_label = QLabel("Готово. Подключите оборудование.", objectName="log")
        self.log_label.setWordWrap(True)
        column.addWidget(self.log_label)
        column.addStretch()
        return panel

    def _dashboard_page(self) -> QWidget:
        page = QWidget(objectName="page")
        box = self._page_layout(page, "Рабочая область", "Спокойная панель управления для подготовки измерений")
        grid = QGridLayout()
        self.field_card = self._card("ПОСЛЕДНЕЕ ПОЛЕ", "— µT")
        self.printer_card = self._card("ПРИНТЕР", "Не подключен")
        self.sensor_card = self._card("ДАТЧИК", "Не подключен")
        grid.addWidget(self._card("СОСТОЯНИЕ", "Ожидание"), 0, 0)
        grid.addWidget(self.printer_card, 0, 1)
        grid.addWidget(self.sensor_card, 0, 2)
        grid.addWidget(self.field_card, 0, 3)
        box.addLayout(grid)
        card = QFrame(objectName="largeCard")
        inside = QVBoxLayout(card)
        inside.addWidget(QLabel("Следующий шаг", objectName="panelTitle"))
        message = QLabel("Подключите принтер и датчик, откройте «Сканирование» и задайте параметры магнита. "
                         "Карта поля и Plotly-графики будут построены на основе записей в архиве.", objectName="placeholder")
        message.setWordWrap(True)
        inside.addWidget(message)
        button = self._button("Перейти к параметрам", lambda: self._show_page(1))
        button.setObjectName("quietAction")
        inside.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        box.addWidget(card, 1)
        return page

    def _scan_page(self) -> QWidget:
        page = QWidget(objectName="page")
        box = self._page_layout(page, "Сканирование", "Параметры траектории. Перед началом принтер выполнит G28, а датчик запомнит ноль.")
        form = QFrame(objectName="largeCard")
        grid = QGridLayout(form)
        grid.setContentsMargins(24, 22, 24, 22)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self.shape = QComboBox()
        self.shape.addItem("Цилиндр", "cylinder")
        self.shape.addItem("Прямоугольник", "rectangle")
        self.shape.currentIndexChanged.connect(self._update_shape_fields)
        self.size_x = self._spin(20.0)
        self.size_y = self._spin(20.0)
        self.height = self._spin(5.0)
        self.step = self._spin(2.0, 0.1)
        self.dimension_label = QLabel("Диаметр, мм")
        self.size_y_label = QLabel("Размер Y, мм")
        entries = (("Форма", self.shape), (self.dimension_label, self.size_x),
                   (self.size_y_label, self.size_y), ("Высота магнита, мм", self.height),
                   ("Шаг сканирования, мм", self.step))
        for row, (label, widget) in enumerate(entries):
            label = QLabel(label) if isinstance(label, str) else label
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)
            if row == 2:
                self.size_y_label = label
                self.size_y_row = row
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setFormat("Ожидание запуска")
        grid.addWidget(self.progress, 5, 0, 1, 2)
        self.scan_confirm = QCheckBox("Магнит закреплён в центре стола; движение безопасно")
        self.scan_confirm.toggled.connect(self._set_scan_enabled)
        grid.addWidget(self.scan_confirm, 6, 0, 1, 2)
        self.scan_button = self._button("Начать сканирование", self._start_scan)
        self.scan_button.setObjectName("quietAction")
        self.scan_button.setEnabled(False)
        grid.addWidget(self.scan_button, 7, 0, 1, 2)
        box.addWidget(form)
        box.addStretch()
        self._update_shape_fields()
        return page

    def _calibration_page(self) -> QWidget:
        page = QWidget(objectName="page")
        box = self._page_layout(page, "Калибровка", "Уберите магнит от датчика: измеренное поле станет нулевой точкой.")
        card = QFrame(objectName="largeCard")
        inside = QVBoxLayout(card)
        inside.addWidget(QLabel("Текущее поле", objectName="panelTitle"))
        self.measurement_label = QLabel("Нет измерений", objectName="measurement")
        inside.addWidget(self.measurement_label)
        inside.addWidget(self._button("Считать показания", self.measure_requested.emit))
        calibrate = self._button("Сохранить ноль датчика", self.calibrate_requested.emit)
        calibrate.setObjectName("quietAction")
        inside.addWidget(calibrate)
        box.addWidget(card)
        box.addStretch()
        return page

    def _archive_page(self) -> QWidget:
        page = QWidget(objectName="page")
        box = self._page_layout(page, "Архив измерений", "CSV остаётся в папке results, а точки дополнительно доступны в локальной базе приложения.")
        controls = QHBoxLayout()
        refresh = self._button("Обновить список", self._load_archive)
        controls.addWidget(refresh)
        controls.addStretch()
        box.addLayout(controls)
        self.scan_table = QTableWidget(0, 5)
        self.scan_table.setHorizontalHeaderLabels(["Дата", "Форма", "Размер", "Шаг", "Точек"])
        self.scan_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.scan_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.scan_table.itemSelectionChanged.connect(self._load_selected_points)
        self.scan_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        box.addWidget(self.scan_table, 1)
        box.addWidget(QLabel("Точки выбранного сканирования", objectName="panelTitle"))
        self.points_table = QTableWidget(0, 8)
        self.points_table.setHorizontalHeaderLabels(["№", "X", "Y", "Z", "Bx, µT", "By, µT", "Bz, µT", "|B|, µT"])
        self.points_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.points_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        box.addWidget(self.points_table, 2)
        return page

    def _statusbar(self) -> QWidget:
        bar = QFrame(objectName="statusbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 4, 16, 4)
        self.status = QLabel("● Система готова")
        row.addWidget(self.status)
        row.addStretch()
        row.addWidget(QLabel("Ubuntu · PySide6 · MLX90393", objectName="version"))
        return bar

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 3:
            self._load_archive()

    def _update_shape_fields(self) -> None:
        cylinder = self.shape.currentData() == "cylinder"
        self.dimension_label.setText("Диаметр, мм" if cylinder else "Размер X, мм")
        self.size_y_label.setVisible(not cylinder)
        self.size_y.setVisible(not cylinder)

    def _set_scan_enabled(self, checked: bool) -> None:
        self.scan_button.setEnabled(checked)

    def _start_scan(self) -> None:
        x = self.size_x.value()
        parameters = {
            "shape": self.shape.currentData(), "size_x": x,
            "size_y": x if self.shape.currentData() == "cylinder" else self.size_y.value(),
            "height": self.height.value(), "step": self.step.value(),
        }
        self.progress.setValue(0)
        self.progress.setFormat("Подготовка оборудования…")
        self.scan_button.setEnabled(False)
        self.scan_requested.emit(parameters)

    def _emergency_stop(self) -> None:
        self.worker.emergency_stop_now()
        self.scan_button.setEnabled(self.scan_confirm.isChecked())
        self.progress.setFormat("Отправлена аварийная остановка")

    def _show_progress(self, point: int, total: int, value: dict) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(point)
        self.progress.setFormat(f"Точка {point}/{total} · |B| = {value['b']:.2f} µT")
        self._show_measurement(value)

    def _scan_finished(self, filename: str) -> None:
        self.progress.setFormat(f"Готово · {Path(filename).name}")
        self.scan_confirm.setChecked(False)
        self._load_archive()
        self._show_page(3)

    def _scan_failed(self, message: str) -> None:
        self.progress.setFormat(f"Не выполнено · {message}")
        self.scan_button.setEnabled(self.scan_confirm.isChecked())

    def _show_measurement(self, value: dict) -> None:
        self.measurement_label.setText(
            f"Bx {value['bx']:.2f} µT   ·   By {value['by']:.2f} µT   ·   "
            f"Bz {value['bz']:.2f} µT   ·   |B| {value['b']:.2f} µT"
        )
        self.field_card.findChild(QLabel, "cardValue").setText(f"{value['b']:.2f} µT")

    def _load_archive(self) -> None:
        scans = list_scans()
        self.scan_table.blockSignals(True)
        self.scan_table.setRowCount(len(scans))
        for row, scan_item in enumerate(scans):
            size = f"Ø {scan_item['size_x']:.1f} мм" if scan_item["shape"] == "cylinder" else f"{scan_item['size_x']:.1f} × {scan_item['size_y']:.1f} мм"
            values = (scan_item["created_at"].replace("T", " "), "Цилиндр" if scan_item["shape"] == "cylinder" else "Прямоугольник", size, f"{scan_item['step']:.2f} мм", str(scan_item["point_count"]))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, scan_item["id"])
                self.scan_table.setItem(row, column, cell)
        self.scan_table.blockSignals(False)
        self.points_table.setRowCount(0)
        if scans:
            self.scan_table.selectRow(0)
            # selectRow() does not reliably emit itemSelectionChanged after a
            # model rebuild on every Qt platform, so populate the preview
            # explicitly as well.
            self._load_selected_points()

    def _load_selected_points(self) -> None:
        rows = self.scan_table.selectionModel().selectedRows()
        if not rows:
            return
        scan_id = self.scan_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        points = get_points(scan_id)
        self.points_table.setUpdatesEnabled(False)
        self.points_table.setRowCount(len(points))
        for row, point in enumerate(points):
            values = (point["point_no"], point["x"], point["y"], point["z"], point["bx"], point["by"], point["bz"], point["b"])
            for column, value in enumerate(values):
                text = str(value) if column == 0 else f"{value:.3f}"
                self.points_table.setItem(row, column, QTableWidgetItem(text))
        self.points_table.setUpdatesEnabled(True)

    def _append_log(self, message: str) -> None:
        self.log_label.setText(message)
        self.status.setText(f"● {message}")

    def _set_printer_state(self, connected: bool, text: str) -> None:
        color = "#b8e3cd" if connected else "#cf9bab"
        self.printer_label.setText(f"<b>Принтер</b><br><span style='color:{color}'>{text}</span>")
        self.printer_card.findChild(QLabel, "cardValue").setText(text)

    def _set_sensor_state(self, connected: bool, text: str) -> None:
        color = "#b8e3cd" if connected else "#cf9bab"
        self.sensor_label.setText(f"<b>Датчик</b><br><span style='color:{color}'>{text}</span>")
        self.sensor_card.findChild(QLabel, "cardValue").setText(text)

    @staticmethod
    def _page_layout(page: QWidget, title: str, subtitle: str) -> QVBoxLayout:
        box = QVBoxLayout(page)
        box.setContentsMargins(30, 26, 30, 26)
        box.setSpacing(12)
        box.addWidget(QLabel(title, objectName="pageTitle"))
        box.addWidget(QLabel(subtitle, objectName="subtitle"))
        return box

    @staticmethod
    def _section(text: str) -> QLabel:
        return QLabel(text, objectName="section")

    @staticmethod
    def _button(text: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _state_label(name: str, state: str) -> QLabel:
        return QLabel(f"<b>{name}</b><br><span>{state}</span>", objectName="device")

    @staticmethod
    def _spin(value: float, minimum: float = 1.0) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(minimum, 220.0)
        box.setValue(value)
        box.setSuffix(" мм")
        box.setDecimals(2)
        return box

    @staticmethod
    def _card(caption: str, value: str) -> QFrame:
        card = QFrame(objectName="card")
        box = QVBoxLayout(card)
        box.addWidget(QLabel(caption, objectName="caption"))
        box.addWidget(QLabel(value, objectName="cardValue"))
        return card

    def closeEvent(self, event) -> None:
        self.worker.emergency_stop_now()
        self.thread.quit()
        self.thread.wait(1500)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet((Path(__file__).parent / "ui" / "app.qss").read_text(encoding="utf-8"))
    window = ScannerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
