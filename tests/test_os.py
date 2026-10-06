"""GasemOS: образ собирается правильно и система работает в QEMU."""

import os
import shutil
import sys
import tempfile
import time
import unittest

from gasem import compile_file

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
        self.assertIn("GasemOS 0.1", lines[0])         # строка состояния
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
        self.run_command("ls", "2 file(s)")
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


if __name__ == "__main__":
    unittest.main()
