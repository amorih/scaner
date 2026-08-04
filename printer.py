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


class Printer:
    """Обёртка над G-code транспортом принтера."""

    def __init__(self, port, baudrate=115200):
        """Открывает порт и переводит его в режим 8N1 со скоростью 115200.

        Аргумент baudrate оставлен в API для понятности, но прошивка проекта
        использует B115200, заданный ниже через termios.
        """
        # O_NOCTTY не даёт порту стать терминалом процесса, O_NONBLOCK не
        # позволяет чтению зависнуть бесконечно.
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attr = termios.tcgetattr(self.fd)

        # Очищаем флаги преобразований ввода/вывода и локальной обработки.
        attr[0] = 0
        attr[1] = 0
        # CS8 — 8 бит данных, CREAD — разрешить чтение, CLOCAL — игнорировать
        # состояние модемных линий USB-переходника.
        attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attr[3] = 0
        attr[4] = termios.B115200  # входящая скорость
        attr[5] = termios.B115200  # исходящая скорость
        # VMIN=0/VTIME=1: os.read возвращает доступные данные, не ждёт пакет.
        attr[6][termios.VMIN] = 0
        attr[6][termios.VTIME] = 1
        termios.tcsetattr(self.fd, termios.TCSANOW, attr)

        # Многие платы Ender перезагружаются при открытии USB-порта.
        time.sleep(2)
        self.clear()

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

    def send(self, command, timeout=30):
        """Передаёт одну строку G-code и ждёт `ok` или ошибку прошивки."""
        print(">", command)  # Дублируем обмен в терминал для диагностики.
        os.write(self.fd, (command + "\n").encode())
        end = time.time() + timeout
        buffer = b""

        # Данные USB могут прийти частями; поэтому накапливаем байты до `\n`.
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
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
                if text:
                    print("<", text)
                if text.lower().startswith("ok"):
                    return
                if "error" in text.lower():
                    raise RuntimeError(text)
        raise TimeoutError(command)

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
        os.write(self.fd, b"M114\n")
        end = time.time() + timeout
        buffer = b""
        position = None

        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
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
                parsed_position = self._parse_position(text)
                if parsed_position:
                    position = parsed_position
                if "error" in text.lower():
                    raise RuntimeError(text)
                if text.lower().startswith("ok") and position is not None:
                    return position

        if position is not None:
            return position
        raise TimeoutError("M114")

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

    def stop(self):
        """Немедленно шлёт M112; исключения игнорируются при уже закрытом порте."""
        try:
            os.write(self.fd, b"M112\n")
        except OSError:
            pass

    def cancel_motion(self):
        """Пытается мягко остановить текущую траекторию без блокировки прошивки.

        M410 очищает буфер планировщика Marlin и останавливает движение, но не
        переводит принтер в аварийное состояние, в отличие от M112.
        """
        try:
            os.write(self.fd, b"M410\n")
        except OSError:
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
        os.close(self.fd)
