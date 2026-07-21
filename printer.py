import os
import time
import termios
import select


class Printer:

    def __init__(self, port, baudrate=115200):

        self.fd = os.open(
            port,
            os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK
        )

        attr = termios.tcgetattr(self.fd)

        attr[0] = 0
        attr[1] = 0
        attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attr[3] = 0

        attr[4] = termios.B115200
        attr[5] = termios.B115200

        attr[6][termios.VMIN] = 0
        attr[6][termios.VTIME] = 1

        termios.tcsetattr(
            self.fd,
            termios.TCSANOW,
            attr
        )

        time.sleep(2)

        self.clear()


    def clear(self):

        while True:

            ready, _, _ = select.select(
                [self.fd],
                [],
                [],
                0.05
            )

            if not ready:
                break

            try:
                os.read(self.fd, 4096)
            except BlockingIOError:
                break


    def send(self, command, timeout=30):

        print(">", command)

        os.write(
            self.fd,
            (command + "\n").encode()
        )

        end = time.time() + timeout

        buffer = b""

        while time.time() < end:

            ready, _, _ = select.select(
                [self.fd],
                [],
                [],
                0.1
            )

            if not ready:
                continue

            try:
                data = os.read(
                    self.fd,
                    4096
                )
            except BlockingIOError:
                continue

            if not data:
                continue

            buffer += data

            while b"\n" in buffer:

                line, buffer = buffer.split(
                    b"\n",
                    1
                )

                text = line.decode(
                    errors="ignore"
                ).strip()

                if text:

                    print("<", text)

                if text.lower().startswith("ok"):
                    return

                if "error" in text.lower():
                    raise RuntimeError(text)

        raise TimeoutError(command)


    def home(self):

        print("\nДомой...")

        self.send("G90")
        self.send("G28", timeout=90)


    def move(
        self,
        x=None,
        y=None,
        z=None,
        speed=5000
    ):

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

        self.send("M400")


    def set_absolute(self):

        self.send("G90")


    def set_relative(self):

        self.send("G91")


    def motors_off(self):

        self.send("M84")


    def stop(self):

        try:
            os.write(
                self.fd,
                b"M112\n"
            )

        except OSError:
            pass


    def park(
        self,
        x=10,
        y=10,
        z=30
    ):

        self.move(
            z=z,
            speed=1500
        )

        self.move(
            x=x,
            y=y,
            speed=6000
        )


    def close(self):

        os.close(self.fd)



