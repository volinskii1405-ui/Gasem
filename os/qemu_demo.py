#!/usr/bin/env python3
"""Собрать GasemOS, запустить в QEMU без окна, «понажимать клавиши» и снять скриншоты.

    python3 os/qemu_demo.py [папка_для_скриншотов]

Нужны qemu-system-i386 и Pillow (pip install pillow) — без Pillow
скриншоты сохраняются в формате PPM.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from gasem import compile_file  # noqa: E402

# символ → имя клавиши для команды sendkey монитора QEMU
KEYS = {" ": "spc", "\n": "ret", "-": "minus", "=": "equal", "+": "shift-equal",
        "*": "shift-8", "/": "slash", "%": "shift-5", "!": "shift-1", ",": "comma",
        ".": "dot", "'": "apostrophe", ":": "shift-semicolon", "?": "shift-slash",
        "\b": "backspace"}


class Qemu:
    def __init__(self, image, workdir):
        self.sock_path = os.path.join(workdir, "monitor.sock")
        qemu = shutil.which("qemu-system-i386") or shutil.which("qemu-system-x86_64")
        if not qemu:
            sys.exit("qemu-system-i386 не найден")
        self.proc = subprocess.Popen(
            [qemu, "-m", "128", "-drive", f"format=raw,file={image}", "-display", "none",
             "-monitor", f"unix:{self.sock_path},server,nowait", "-no-reboot"],
            cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.workdir = workdir
        for _ in range(100):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.sock_path)
        self.sock.settimeout(2)
        self.read()

    def read(self):
        data = b""
        try:
            while not data.endswith(b"(qemu) "):
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data

    def cmd(self, line):
        self.sock.sendall(line.encode() + b"\n")
        return self.read()

    def key(self, name, delay=0.06):
        self.cmd(f"sendkey {name} 30")
        time.sleep(delay)

    def type(self, text, delay=0.06):
        for ch in text:
            if ch.isalpha() and ch.isupper():
                name = "shift-" + ch.lower()
            elif ch.isalnum():
                name = ch
            else:
                name = KEYS[ch]
            self.key(name, delay)

    def screen_text(self):
        """Прочитать текстовый видеобуфер (80x25) из памяти машины."""
        return [bytes(row[0::2]) for row in self.screen_cells()]

    def screen_cells(self):
        """Видеобуфер построчно: пары байт (символ, цвет)."""
        # путь относительный: в мониторе QEMU '/' читается как деление
        self.cmd("pmemsave 0xb8000 4000 vga.bin")
        with open(os.path.join(self.workdir, "vga.bin"), "rb") as f:
            raw = f.read()
        return [raw[r * 160:(r + 1) * 160] for r in range(25)]

    def screenshot(self, path):
        path = os.path.abspath(path)            # QEMU работает в своей временной папке
        ppm = path.rsplit(".", 1)[0] + ".ppm"
        self.cmd(f"screendump {ppm}")
        time.sleep(0.3)
        try:
            from PIL import Image
        except ImportError:
            return ppm
        Image.open(ppm).save(path)
        os.remove(ppm)
        return path

    def quit(self):
        try:
            self.cmd("quit")
        except OSError:
            pass
        self.proc.wait(timeout=10)


def play_snake(vm, seconds):
    """Простой автопилот: ведёт голову змейки к еде, читая видеопамять."""
    dirs = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
    opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}
    current = "right"
    end = time.time() + seconds
    while time.time() < end:
        cells = vm.screen_cells()
        rows = [bytes(row[0::2]) for row in cells]
        head = food = None
        for r in range(25):
            for c in range(80):
                ch, attr = cells[r][2 * c], cells[r][2 * c + 1]
                if ch == 0x04:
                    food = (r, c)
                elif ch == 0xDB and attr == 0x0A:      # голова — светло-зелёная
                    head = (r, c)
        if head is None or food is None:
            break

        def free(cell):
            r, c = cell
            return 3 <= r <= 23 and 1 <= c <= 78 and rows[r][c] in (0x20, 0x04)

        want = []
        if food[0] < head[0]:
            want.append("up")
        if food[0] > head[0]:
            want.append("down")
        if food[1] < head[1]:
            want.append("left")
        if food[1] > head[1]:
            want.append("right")
        choice = None
        for d in want + ["up", "down", "left", "right"]:
            if d == opposite[current]:
                continue
            dr, dc = dirs[d]
            if free((head[0] + dr, head[1] + dc)):
                choice = d
                break
        if choice and choice != current:
            vm.key(choice, 0)
            current = choice
        time.sleep(0.03)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        image = os.path.join(tmp, "gasemos.img")
        with open(image, "wb") as f:
            f.write(compile_file(os.path.join(HERE, "gasemos.gsm")).code)

        vm = Qemu(image, tmp)
        shots = []

        def shot(name):
            shots.append(vm.screenshot(os.path.join(out_dir, name)))
            print("снимок:", shots[-1])

        try:
            time.sleep(4)
            shot("1-boot.png")

            vm.type("help\n")
            time.sleep(0.5)
            shot("2-help.png")

            vm.type("clear\n")
            for line in ["about\n", "cpu\n", "mem\n", "time\n", "uptime\n"]:
                vm.type(line)
                time.sleep(0.2)
            time.sleep(0.5)
            shot("3-system-info.png")

            vm.type("clear\n")
            for line in ["calc 6*7\n", "calc 1000-2026\n", "calc 100/0\n",
                         "echo Hello from Gasem!\n", "color 14\n", "echo Yellow text\n",
                         "color 11\n", "echo Cyan text\n", "color 7\n", "dir\n"]:
                vm.type(line)
                time.sleep(0.2)
            time.sleep(0.5)
            shot("4-commands.png")

            vm.type("snake\n")
            time.sleep(0.5)
            play_snake(vm, 40)
            shot("5-snake.png")
            vm.type("q")
            time.sleep(0.5)

            vm.type("crash\n")
            time.sleep(0.8)
            shot("6-exception.png")
        finally:
            vm.quit()
    return shots


if __name__ == "__main__":
    main()
