"""GasemOS: образ собирается правильно и система работает в QEMU."""

import os
import shutil
import sys
import tempfile
import time
import unittest

from gasem import compile_file, compile_source

OS_DIR = os.path.join(os.path.dirname(__file__), "..", "os")
QEMU = shutil.which("qemu-system-i386") or shutil.which("qemu-system-x86_64")


def build():
    return compile_file(os.path.join(OS_DIR, "gasemos.gsm"))


class GasemOSBuildTest(unittest.TestCase):
    def test_image_layout(self):
        res = build()
        code = res.code
        self.assertEqual(len(code) % 512, 0)
        self.assertEqual(code[510:512], b"\x55\xaa")          # загрузочная сигнатура
        self.assertEqual(res.symbols["kernel_start"], 0x7E00)  # ядро сразу за загрузчиком
        kernel = res.symbols["kernel_end"] - res.symbols["kernel_start"]
        self.assertEqual(res.symbols["KERNEL_SECTORS"], kernel // 512)
        fs = res.symbols["FS_START"] * 512                     # GasemFS — за ядром
        self.assertLessEqual(res.symbols["kernel_end"] - 0x7C00, fs)
        self.assertEqual(code[fs:fs + 8], b"GASEMFS1")

    def test_programs_on_disk(self):
        sys.path.insert(0, OS_DIR)
        from gasemfs import GasemFS
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "gasemos.img")
            with open(image, "wb") as f:
                f.write(build().code)
            fs = GasemFS(image)
            self.assertTrue(fs.check())
            names = [n for n, _, _ in fs.files()]
            for prog in ("hello", "edit", "paint", "mandel", "music", "ticker"):
                self.assertIn(prog + ".bin", names)
                data = fs.read(prog + ".bin")
                self.assertEqual(data[:4], b"GAPP")             # заголовок программы
                entry = int.from_bytes(data[4:8], "little")    # main — внутри программы
                self.assertTrue(0x400000 < entry < 0x400000 + len(data), prog)
            # программа на диске — та же, что собирается отдельно из os/apps
            alone = compile_file(os.path.join(OS_DIR, "apps", "hello.gsm")).code
            self.assertEqual(fs.read("hello.bin"), alone)


@unittest.skipUnless(QEMU, "QEMU не установлен")
class GasemOSQemuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, OS_DIR)
        import qemu_demo
        cls.tmp = tempfile.TemporaryDirectory()
        image = os.path.join(cls.tmp.name, "gasemos.img")
        with open(image, "wb") as f:
            f.write(build().code)
        cls.vm = qemu_demo.Qemu(image, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.vm.quit()
        cls.tmp.cleanup()

    def screen(self):
        return [row.decode("cp437").rstrip() for row in self.vm.screen_text()]

    def wait_for(self, text, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            lines = self.screen()
            if any(text in line for line in lines):
                return lines
            time.sleep(0.2)
        self.fail(f"на экране нет «{text}»:\n" + "\n".join(self.screen()))

    def run_command(self, command, expect):
        self.vm.type(command + "\n")
        return self.wait_for(expect, timeout=5)

    def test_shell(self):
        lines = self.wait_for("gasem>")
        self.assertIn("GasemOS 0.2", lines[0])         # строка состояния
        self.assertIn("uptime", lines[0])
        self.run_command("calc 6*7", "= 42")
        self.run_command("calc 5-8", "= -3")
        self.run_command("mem", "Memory: 128 MB")
        self.run_command("cpu", "Vendor:")
        self.run_command("echo Gasem works", "Gasem works")
        self.run_command("frobnicate", "Unknown command: frobnicate")

    def test_files(self):
        self.wait_for("gasem>")
        self.run_command("clear", "gasem>")
        self.run_command("ls", "8 file(s)")
        self.run_command("cat hello.gsm", "jfnz print")
        self.run_command("write note.txt Hello from GasemFS", "Saved 19 bytes to note.txt")
        self.run_command("append note.txt Second line", "Saved 31 bytes to note.txt")
        self.run_command("cp note.txt copy.txt", "Saved 31 bytes to copy.txt")
        self.run_command("mv copy.txt old.txt", "Renamed.")
        self.run_command("rm old.txt", "Deleted old.txt")
        self.run_command("cat old.txt", "File not found: old.txt")


@unittest.skipUnless(QEMU, "QEMU не установлен")
class GasemFSPersistenceTest(unittest.TestCase):
    """Файлы, записанные системой, остаются на диске после перезапуска."""

    def test_reboot_keeps_files(self):
        sys.path.insert(0, OS_DIR)
        import qemu_demo
        from gasemfs import GasemFS
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "gasemos.img")
            with open(image, "wb") as f:
                f.write(build().code)
            lines = [f"line {i} of a file longer than one 512-byte block" for i in range(12)]

            vm = qemu_demo.Qemu(image, tmp)
            try:
                time.sleep(3)
                for line in lines:
                    vm.type(f"append big.txt {line}\n", delay=0.03)
                vm.type("shutdown\n")
                vm.proc.wait(timeout=20)       # ACPI выключение
            finally:
                if vm.proc.poll() is None:
                    vm.quit()

            fs = GasemFS(image)                 # проверяем диск независимым кодом
            self.assertTrue(fs.check())
            self.assertEqual(fs.read("big.txt").decode(), "".join(l + "\n" for l in lines))

            vm = qemu_demo.Qemu(image, tmp)     # и снова загружаемся
            try:
                time.sleep(3)
                vm.type("cat big.txt\n")
                time.sleep(1)
                screen = [r.decode("cp437") for r in vm.screen_text()]
                self.assertTrue(any(lines[-1] in r for r in screen), "\n".join(screen))
            finally:
                vm.quit()


DIVZERO = """include "sdk.gsm"
main:
    print "dividing by zero...\\n"
    xor edx - edx
    xor eax - eax
    div eax
    ret
"""


@unittest.skipUnless(QEMU, "QEMU не установлен")
class GasemOSProgramsTest(unittest.TestCase):
    """Программы с диска, задачи, редактор, графика, мышь и звук."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, OS_DIR)
        import qemu_demo
        from gasemfs import GasemFS
        cls.tmp = tempfile.TemporaryDirectory()
        image = os.path.join(cls.tmp.name, "gasemos.img")
        with open(image, "wb") as f:
            f.write(build().code)
        crash = compile_source(DIVZERO, base_dir=OS_DIR).code   # своя программа на диск
        GasemFS(image).put("divzero.bin", crash)
        cls.wav = os.path.join(cls.tmp.name, "sound.wav")
        cls.vm = qemu_demo.Qemu(image, cls.tmp.name, args=(
            "-audiodev", f"wav,id=snd,path={cls.wav}", "-machine", "pcspk-audiodev=snd"))

    @classmethod
    def tearDownClass(cls):
        cls.vm.quit()
        cls.tmp.cleanup()

    screen = GasemOSQemuTest.screen
    wait_for = GasemOSQemuTest.wait_for
    run_command = GasemOSQemuTest.run_command

    def setUp(self):
        self.wait_for("gasem>")
        for task in self.tasks()[1:]:       # остатки упавших тестов
            self.run_command("kill " + task[0], "stopped")
        self.run_command("clear", "gasem>")

    def tasks(self):
        self.run_command("clear", "gasem>")
        lines = self.run_command("ps", "PROGRAM")
        return [l.split() for l in lines if l.startswith("   ")]

    def test_hello_and_arguments(self):
        self.run_command("hello", "Hello, world!")
        lines = self.run_command("hello Gasem", "Hello, Gasem!")
        self.assertTrue(any("running as task #1" in l for l in lines))
        self.run_command("run hello.bin from run", "Hello, from run!")
        self.run_command("nothing", "Unknown command: nothing")

    def test_background_tasks(self):
        self.run_command("ticker &", "[1] ticker.bin started in the background")
        self.run_command("ticker &", "[2] ticker.bin started in the background")
        tasks = [(t[0], t[2]) for t in self.tasks() if t[1] in ("run", "ready", "sleep")]
        self.assertEqual(tasks, [("0", "shell"), ("1", "ticker.bin"), ("2", "ticker.bin")])
        bar = lambda: self.vm.screen_cells()[0][72:96]   # там бегает точка
        first = bar()
        time.sleep(0.5)
        self.assertNotEqual(first, bar())
        self.run_command("kill 1", "Task 1 stopped.")
        self.run_command("kill 2", "Task 2 stopped.")
        self.run_command("kill 2", "No such task.")
        self.run_command("kill 0", "Usage: kill")
        self.run_command("clear", "gasem>")
        self.assertEqual(self.tasks(), [["0", "run", "shell"]])

    def test_ctrl_c_stops_the_program(self):
        self.vm.type("ticker\n")
        time.sleep(1)
        self.assertEqual(sum("gasem>" in l for l in self.screen()), 1)   # оболочка ждёт
        self.vm.key("ctrl-c")
        end = time.time() + 3
        while time.time() < end and sum("gasem>" in l for l in self.screen()) < 2:
            time.sleep(0.1)
        self.assertEqual(sum("gasem>" in l for l in self.screen()), 2)
        self.assertEqual(self.tasks(), [["0", "run", "shell"]])

    def test_crash_does_not_stop_the_system(self):
        lines = self.run_command("divzero", "crashed")
        self.assertTrue(any("divzero.bin crashed: exception #0: Divide Error" in l for l in lines))
        self.run_command("hello again", "Hello, again!")

    def test_editor(self):
        self.run_command("edit memo.txt", "New file")
        self.vm.type("First line\nabc")
        self.vm.key("left")
        self.vm.key("left")
        self.vm.type("X")
        self.vm.key("up")
        self.vm.key("end")
        self.vm.type("!")
        lines = self.wait_for("Ln 1, Col 12")
        self.assertIn("[modified]", lines[24])
        self.vm.key("ctrl-s")
        self.wait_for("Saved")
        self.vm.key("ctrl-q")
        self.wait_for("gasem>")
        lines = self.run_command("cat memo.txt", "aXbc")
        self.assertTrue(any(l == "First line!" for l in lines))
        self.run_command("edit memo.txt", "Ln 1, Col 1")     # файл открывается снова
        self.vm.type("Z")
        self.vm.key("esc")
        self.wait_for("Not saved!")                          # без сохранения — переспрашивает
        self.vm.key("esc")
        self.wait_for("gasem>")
        lines = self.run_command("cat memo.txt", "aXbc")
        self.assertTrue(any(l == "First line!" for l in lines))

    def gfx(self):
        return self.vm.memory(0xA0000, 320 * 200)

    def test_paint_with_mouse(self):
        self.vm.type("paint\n")
        time.sleep(1.5)
        self.vm.mouse(-100, -40)              # из (160, 100) в (60, 60)
        self.vm.mouse(buttons=1)
        for _ in range(10):
            self.vm.mouse(10, 5)              # мазок до (160, 110)
        self.vm.mouse(buttons=0)
        time.sleep(0.3)
        canvas = self.gfx()[320 * 16:320 * 184]
        self.assertGreater(canvas.count(0), 150)            # чёрная линия кистью 3x3
        self.assertGreater(canvas.count(15), 40000)         # остальное — белый холст
        self.vm.mouse(90, 200)                # к палитре: (250, 199) — красный (12)
        self.vm.mouse(buttons=1)
        self.vm.mouse(buttons=0)
        self.vm.mouse(-50, -100)
        self.vm.mouse(buttons=1)
        for _ in range(5):
            self.vm.mouse(-6, 6)
        self.vm.mouse(buttons=0)
        time.sleep(0.3)
        self.assertGreater(self.gfx()[320 * 16:320 * 184].count(12), 50)
        self.vm.key("esc")
        self.wait_for("gasem>")               # снова текстовый режим и прежний экран

    def test_mandelbrot(self):
        self.vm.type("mandel\n")
        time.sleep(1)
        end = time.time() + 20
        while time.time() < end and self.gfx()[-320 * 18:].count(255) < 100:   # надпись внизу
            time.sleep(0.5)
        image = self.gfx()
        self.assertGreater(image.count(0), 5000)            # само множество — чёрное
        self.assertGreater(len(set(image)), 20)             # вокруг — переходы цвета
        self.vm.key("spc")
        self.wait_for("gasem>")

    def tone(self, start):
        """Частота звука, записанного после смещения start в WAV-файле."""
        with open(self.wav, "rb") as f:
            f.seek(start)
            data = f.read()
        samples = [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data) - 3, 4)]
        self.assertGreater(len(samples), 4410, "звука нет")
        mean = sum(samples) / len(samples)
        crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a - mean) * (b - mean) < 0)
        return crossings / 2 / (len(samples) / 44100)

    def test_sound(self):
        start = os.path.getsize(self.wav)
        self.run_command("beep 440 500", "gasem>")
        time.sleep(1)
        self.assertAlmostEqual(self.tone(start), 440, delta=25)
        start = os.path.getsize(self.wav)
        self.run_command("beep 1000 300", "gasem>")
        time.sleep(1)
        self.assertAlmostEqual(self.tone(start), 1000, delta=50)

    def test_help_lists_programs(self):
        lines = self.run_command("help", "Programs on the disk")
        self.assertTrue(any("edit <file>" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
