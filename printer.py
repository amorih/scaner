"""Низкоуровневое управление 3D-принтером через Linux serial-порт.

Модуль не зависит от PySide6: его можно использовать из консольной версии и
из GUI. Он посылает обычный G-code и ждёт ответ прошивки `ok`.
"""

# os открывает файл устройства `/dev/ttyUSB0` и читает/пишет байты.
import os
# re разбирает строку ответа M114 и достаёт реальные X/Y/Z координаты.
import re
# time нужен для таймаутов ответов и паузы после открытия USB-порта.
import time
# termios настраивает UART в Linux без сторонней библиотеки pyserial.
import termios
# select ждёт данные порта без постоянного активного опроса процессора.
import select
import logging


logger = logging.getLogger(__name__)


class Printer:
    """Обёртка над G-code транспортом принтера."""

    def __init__(self, port, baudrate=115200):
        """Открывает порт и переводит его в режим 8N1 со скоростью 115200.

        Аргумент baudrate переводится в константу termios и применяется к
        входящему и исходящему направлению USB-порта.
        """
        # Проверяем скорость до открытия порта: ошибочная настройка не должна
        # выглядеть в интерфейсе как «принтер подключён, но молчит».
        speed = self._baudrate_constant(baudrate)
        self.fd = None
        # O_NOCTTY не даёт порту стать терминалом процесса, O_NONBLOCK не
        # позволяет чтению зависнуть бесконечно.
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attr = termios.tcgetattr(self.fd)

            # Очищаем флаги преобразований ввода/вывода и локальной обработки.
            attr[0] = 0
            attr[1] = 0
            # CS8 — 8 бит данных, CREAD — разрешить чтение, CLOCAL — игнорировать
            # состояние модемных линий USB-переходника.
            attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attr[3] = 0
            attr[4] = speed
            attr[5] = speed
            attr[6][termios.VMIN] = 0
            attr[6][termios.VTIME] = 1
            termios.tcsetattr(self.fd, termios.TCSANOW, attr)

            # Многие платы Ender перезагружаются при открытии USB-порта.
            time.sleep(2)
            self.clear()
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    @staticmethod
    def _baudrate_constant(baudrate):
        """Переводит обычное число скорости в константу termios Linux."""
        supported = {
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
        }
        try:
            return supported[int(baudrate)]
        except (KeyError, TypeError, ValueError) as error:
            available = ", ".join(str(rate) for rate in supported)
            raise ValueError(f"Неподдерживаемая скорость порта: {baudrate}. Доступно: {available}") from error

    def clear(self):
        """Выбрасывает приветственные и старые сообщения прошивки из буфера."""
        while True:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                break
            try:
                os.read(self.fd, 4096)
            except BlockingIOError:
                break

    def _exchange(self, command, timeout):
        """Передаёт G-code и возвращает строки ответа вплоть до `ok`."""
        if self.fd is None:
            raise RuntimeError("Порт принтера закрыт")

        logger.debug("printer -> %s", command)
        os.write(self.fd, (command.rstrip("\n") + "\n").encode("ascii"))
        end = time.monotonic() + timeout
        buffer = b""
        lines = []

        # Данные USB могут прийти частями; поэтому накапливаем байты до `\n`.
        while time.monotonic() < end:
            remaining = max(0.0, end - time.monotonic())
            ready, _, _ = select.select([self.fd], [], [], min(0.1, remaining))
            if not ready:
                continue
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not data:
                continue
            buffer += data

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue
                logger.debug("printer <- %s", text)
                lines.append(text)
                if text.lower().startswith("ok"):
                    return lines
                if "error" in text.lower():
                    raise RuntimeError(text)
        raise TimeoutError(command)

    def send(self, command, timeout=30):
        """Передаёт одну строку G-code и ждёт `ok` или ошибку прошивки."""
        self._exchange(command, timeout)

    @staticmethod
    def _parse_position(text):
        """Извлекает X/Y/Z из строки ответа M114 или возвращает None.

        Метод вынесен отдельно от serial-цикла, чтобы формат ответа прошивки
        можно было проверить автоматическим тестом без подключённого принтера.
        """
        match = re.search(
            r"X:\s*(-?\d+(?:\.\d+)?)\s+Y:\s*(-?\d+(?:\.\d+)?)\s+Z:\s*(-?\d+(?:\.\d+)?)",
            text,
        )
        if not match:
            return None
        return {
            "x": float(match.group(1)),
            "y": float(match.group(2)),
            "z": float(match.group(3)),
        }

    def query_position(self, timeout=4):
        """Запрашивает у прошивки реальные координаты через M114.

        В отличие от координат, которые приложение может только предполагать,
        M114 возвращает текущее состояние осей из самой прошивки принтера.
        Это и нужно для живого отображения XYZ в нижней строке интерфейса.
        """
        for text in self._exchange("M114", timeout):
            position = self._parse_position(text)
            if position is not None:
                return position
        raise RuntimeError("Прошивка не вернула координаты M114")

    def home(self):
        """G90 выбирает абсолютные координаты, G28 ищет концевики."""
        self.send("G90")
        self.send("G28", timeout=90)

    def move(self, x=None, y=None, z=None, speed=5000):
        """Собирает G1 только из переданных осей и ждёт окончания движения."""
        command = "G1"
        if x is not None:
            command += f" X{x:.3f}"
        if y is not None:
            command += f" Y{y:.3f}"
        if z is not None:
            command += f" Z{z:.3f}"
        command += f" F{speed}"
        self.send(command)
        self.wait()

    def wait(self):
        """M400 блокирует следующий шаг Python до завершения движения в буфере."""
        self.send("M400")

    def motors_off(self):
        """M84 отключает удерживающий ток шаговых двигателей."""
        self.send("M84")

    def motors_on(self):
        """M17 включает удерживающий ток шаговых двигателей без движения."""
        self.send("M17")

    def display_status(self, message):
        """Показывает короткий статус на штатном LCD Marlin."""
        safe = str(message).replace("\n", " ")[:28]
        self.send(f"M117 {safe}")

    def begin_measurement(self, total=0):
        """Сбрасывает статистику LCD и начинает новый сеанс измерений."""
        self.send(f"M790 R P0 N0 T{max(0, int(total))} S1")

    def display_progress(self, point, total, magnitude=None):
        """Обновляет прогресс через специализированный протокол сканера."""
        percent = 0 if total <= 0 else max(0, min(100, int(point * 100 / total)))
        parts = [f"M790 P{percent} N{int(point)} T{int(total)} S1"]
        if magnitude is not None:
            parts.append(f"B{float(magnitude):.2f}")
        self.send(" ".join(parts))

    def display_field(self, bx, by, bz, magnitude):
        """Передаёт компоненты поля в отдельные переменные интерфейса."""
        self.send(self._scanner_data_command(bx, by, bz, magnitude))

    @staticmethod
    def _scanner_data_command(bx, by, bz, magnitude, point=None, total=None):
        """Собирает компактный пакет M790, помещающийся в буфер Marlin."""
        parts = [
            "M790",
            f"X{float(bx):.2f}",
            f"Y{float(by):.2f}",
            f"Z{float(bz):.2f}",
            f"B{float(magnitude):.2f}",
        ]
        if point is not None and total is not None:
            point, total = int(point), int(total)
            percent = 0 if total <= 0 else max(0, min(100, int(point * 100 / total)))
            parts.extend((f"P{percent}", f"N{point}", f"T{total}"))
        parts.append("S1")
        return " ".join(parts)

    def display_scan_data(self, bx, by, bz, magnitude, point, total):
        """Одним пакетом синхронизирует датчик, точку и прогресс с LCD."""
        self.send(self._scanner_data_command(bx, by, bz, magnitude, point, total))

    def signal_complete(self):
        """Показывает завершение и проигрывает короткую мелодию."""
        self.send("M790 P100 S1 C1")
        self.display_status("SCAN COMPLETE")
        self.send("M300 S1568 P120")
        self.send("M300 S1976 P120")
        self.send("M300 S2637 P240")

    def signal_error(self, message="SENSOR ERROR"):
        """Показывает ошибку и проигрывает низкий предупреждающий сигнал."""
        self.send("M790 S0 E1")
        self.display_status(message)
        self.send("M300 S220 P250")
        self.send("M300 S165 P400")

    def stop(self):
        """Немедленно шлёт M112; исключения игнорируются при уже закрытом порте."""
        self._write_immediate("M112")

    def cancel_motion(self):
        """Пытается мягко остановить текущую траекторию без блокировки прошивки.

        M410 очищает буфер планировщика Marlin и останавливает движение, но не
        переводит принтер в аварийное состояние, в отличие от M112.
        """
        self._write_immediate("M410")

    def _write_immediate(self, command):
        """Посылает аварийную команду без ожидания ответа."""
        try:
            if self.fd is not None:
                os.write(self.fd, (command.rstrip("\n") + "\n").encode("ascii"))
        except (OSError, TypeError):
            pass

    def recover_after_stop(self):
        """Автоматически снимает состояние emergency stop после M112.

        В прошивках Marlin после M112 большинство команд игнорируются до M999.
        Мы даём плате короткую паузу, очищаем старый вывод и просим прошивку
        перезапустить командный цикл, чтобы пользователю не пришлось
        передёргивать питание или заново открывать приложение.
        """
        time.sleep(0.35)
        self.clear()
        self.send("M999", timeout=10)
        self.clear()

    def close(self):
        """Освобождает файловый дескриптор USB-порта после завершения работы."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
