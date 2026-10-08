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
        "\b": "backspace", "_": "shift-minus", "&": "shift-7", "(": "shift-9", ")": "shift-0",
        "[": "bracket_left", "]": "bracket_right", "{": "shift-bracket_left", "}": "shift-bracket_right",
        ";": "semicolon", "<": "shift-comma", ">": "shift-dot", '"': "shift-apostrophe",
        "#": "shift-3", "@": "shift-2", "$": "shift-4", "^": "shift-6", "\\": "backslash",
        "|": "shift-backslash", "\t": "tab", "`": "grave_accent", "~": "shift-grave_accent"}


class Qemu:
    def __init__(self, image, workdir, qemu=None, args=()):
        self.sock_path = os.path.join(workdir, "monitor.sock")
        qemu = qemu or shutil.which("qemu-system-i386") or shutil.which("qemu-system-x86_64")
        if not qemu:
            sys.exit("qemu-system-i386 не найден")
        self.proc = subprocess.Popen(
            [qemu, "-m", "128", "-drive", f"format=raw,file={image}", "-display", "none",
             "-monitor", f"unix:{self.sock_path},server,nowait", "-no-reboot", *args],
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
        raw = self.memory(0xB8000, 4000)
        return [raw[r * 160:(r + 1) * 160] for r in range(25)]

    def memory(self, addr, size):
        """Прочитать физическую память машины."""
        # путь относительный: в мониторе QEMU '/' читается как деление
        self.cmd(f"pmemsave {addr:#x} {size} mem.bin")
        with open(os.path.join(self.workdir, "mem.bin"), "rb") as f:
            return f.read()

    def mouse(self, dx=0, dy=0, buttons=None):
        """Сдвинуть мышь и/или нажать кнопки (1 — левая, 2 — правая)."""
        if dx or dy:
            self.cmd(f"mouse_move {dx} {dy}")
        if buttons is not None:
            self.cmd(f"mouse_button {buttons}")
        time.sleep(0.05)

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
        self.sock.close()


def play_snake(vm, seconds, target=10):
    """Простой автопилот: ведёт голову змейки к еде, читая видеопамять.
    Останавливается, набрав target очков."""
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
        title = rows[1].decode("cp437")
        if "Score:" in title and int(title.split("Score:")[1].split()[0]) >= target:
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


class Pointer:
    """Мышь в программе paint: QEMU двигает её относительно, а мы помним,
    где она (координаты как в GasemOS: 0..319, 0..199, сначала — центр)."""

    def __init__(self, vm):
        self.vm, self.x, self.y = vm, 160, 100

    def goto(self, x, y):
        x, y = min(max(x, 0), 319), min(max(y, 0), 199)
        while (self.x, self.y) != (x, y):
            dx, dy = max(-12, min(12, x - self.x)), max(-12, min(12, y - self.y))
            self.vm.mouse(dx, dy)
            self.x, self.y = self.x + dx, self.y + dy

    def stroke(self, points):
        self.goto(*points[0])
        self.vm.mouse(buttons=1)
        for p in points[1:]:
            self.goto(*p)
        self.vm.mouse(buttons=0)

    def pick(self, color):
        """Щелчок по палитре внизу экрана."""
        self.goto(color * 20 + 10, 194)
        self.vm.mouse(buttons=1)
        self.vm.mouse(buttons=0)


def draw_house(vm):
    import math
    p = Pointer(vm)
    p.pick(2)                                      # трава
    p.stroke([(5, 172), (314, 172)])
    p.stroke([(5, 176), (314, 176)])
    p.pick(6)                                      # стены
    p.stroke([(110, 170), (110, 112), (190, 112), (190, 170), (110, 170)])
    p.pick(12)                                     # крыша
    p.stroke([(98, 114), (150, 70), (202, 114), (98, 114)])
    p.pick(1)                                      # дверь
    p.stroke([(140, 170), (140, 138), (160, 138), (160, 170)])
    p.pick(11)                                     # окно
    p.stroke([(118, 124), (132, 124), (132, 136), (118, 136), (118, 124)])
    p.stroke([(168, 124), (182, 124), (182, 136), (168, 136), (168, 124)])
    p.pick(14)                                     # солнце
    p.stroke([(265 + round(16 * math.cos(a * math.pi / 10)), 50 + round(14 * math.sin(a * math.pi / 10)))
              for a in range(21)])
    for a in range(0, 20, 3):
        c, s = math.cos(a * math.pi / 10), math.sin(a * math.pi / 10)
        p.stroke([(265 + round(21 * c), 50 + round(19 * s)), (265 + round(28 * c), 50 + round(25 * s))])
    p.goto(300, 140)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    shots = []
    with tempfile.TemporaryDirectory() as tmp:
        image = os.path.join(tmp, "gasemos.img")
        with open(image, "wb") as f:
            f.write(compile_file(os.path.join(HERE, "gasemos.gsm")).code)

        def shot(vm, name):
            shots.append(vm.screenshot(os.path.join(out_dir, name)))
            print("снимок:", shots[-1])

        def wait_text(vm, text, timeout=20):
            end = time.time() + timeout
            while time.time() < end and not any(text in r.decode("cp437") for r in vm.screen_text()):
                time.sleep(0.2)

        def run(vm, lines, pause=0.3):
            for line in lines:
                vm.type(line + "\n")
                time.sleep(pause)
            time.sleep(0.5)

        vm = Qemu(image, tmp)
        try:
            time.sleep(4)
            shot(vm, "1-boot.png")
            run(vm, ["help"])
            shot(vm, "2-help.png")
            run(vm, ["clear", "about", "cpu", "mem", "time", "uptime"])
            shot(vm, "3-system-info.png")
            run(vm, ["clear", "calc 6*7", "calc 1000-2026", "calc 100/0", "echo Hello from Gasem!",
                     "color 14", "echo Yellow text", "color 11", "echo Cyan text", "color 7", "dir"])
            shot(vm, "4-commands.png")
            run(vm, ["clear", "ls", "write note.txt GasemFS keeps files on the disk.",
                     "append note.txt They survive a reboot.", "cp note.txt copy.txt",
                     "mv copy.txt backup.txt", "ls", "cat note.txt"])
            shot(vm, "5-files.png")
            vm.type("shutdown\n")                  # выключаем — файлы должны остаться
            vm.proc.wait(timeout=20)
        finally:
            if vm.proc.poll() is None:
                vm.quit()

        vm = Qemu(image, tmp)                     # новая загрузка с того же диска
        try:
            time.sleep(4)
            run(vm, ["ls", "cat note.txt"])
            shot(vm, "6-after-reboot.png")
            run(vm, ["snake"])
            play_snake(vm, 40)
            shot(vm, "7-snake.png")
            vm.type("q")
            time.sleep(0.5)
            run(vm, ["clear", "ls", "hello Gasem", "ticker &", "music &", "ps"], pause=0.6)
            shot(vm, "9-programs.png")
            run(vm, ["kill 2", "clear", "edit note.txt"])
            vm.key("end")
            vm.key("down")
            vm.key("end")
            vm.type("\n\nGasemOS 0.2 runs programs from the disk:\n"
                    "  edit    - this text editor\n  paint   - draw with the mouse\n"
                    "  mandel  - the Mandelbrot set\n  music   - a melody on the PC speaker\n"
                    "  ticker  - a background task\n\nCtrl+S saves the file, Ctrl+Q quits.", delay=0.03)
            wait_text(vm, "Ctrl+Q quits.")         # QEMU отдаёт нажатия с задержкой
            shot(vm, "10-editor.png")
            vm.key("ctrl-s")
            vm.key("ctrl-q")
            run(vm, ["paint"], pause=1.5)
            draw_house(vm)
            time.sleep(0.5)
            shot(vm, "11-paint.png")
            vm.key("esc")
            run(vm, ["mandel"], pause=6)
            shot(vm, "12-mandel.png")
            vm.key("spc")
            time.sleep(0.5)
            run(vm, ["crash"])
            shot(vm, "8-exception.png")
        finally:
            vm.quit()
    return shots


if __name__ == "__main__":
    main()
