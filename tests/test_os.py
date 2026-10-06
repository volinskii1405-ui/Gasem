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
        self.assertEqual(res.symbols["KERNEL_SECTORS"], len(code) // 512 - 1)


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


if __name__ == "__main__":
    unittest.main()
