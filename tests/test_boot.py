"""Примеры из examples/ запускаются в эмуляторе x86 и проверяется результат."""

import os
import unittest

from gasem import compile_file, compile_source

from emu import run_boot, unicorn

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def example(name):
    return compile_file(os.path.join(EXAMPLES, name)).code


@unittest.skipUnless(unicorn, "unicorn не установлен (pip install unicorn)")
class BootTest(unittest.TestCase):
    def test_hello(self):
        r = run_boot(example("hello.gsm"))
        # Цикл из документации печатает и завершающий 0 (проверка идёт после int 0x10).
        self.assertEqual(r.output, b"Hello\0")

    def test_do(self):
        r = run_boot(example("do.gsm"))
        self.assertEqual(r.output, b"Hello\r\nHello1\r\nHelloWorld\r\n8")

    def test_protected_mode(self):
        r = run_boot(example("pmode.gsm"))
        self.assertEqual(r.screen(), "Gasem: 32-bit protected mode")
        self.assertTrue(r.port92 & 2, "A20 не включена")
        self.assertTrue(r.regs["cr0"] & 1, "бит PE не установлен")

    def test_a20_preserves_ax(self):
        code = compile_source("og 0x7C00\nmov ax - 0x1234\nmov a20 - 1\nmov bx - ax\n"
                              "mov a20 - 0\nmov cx - ax\njmp $").code
        r = run_boot(code)
        self.assertEqual(r.regs["ebx"] & 0xFFFF, 0x1234)
        self.assertEqual(r.regs["ecx"] & 0xFFFF, 0x1234)
        self.assertEqual(r.port92 & 3, 0)   # после mov a20 - 0

    def test_arithmetic_program(self):
        src = """
og 0x7C00
b 16
    xor ax - ax
    mov cx - 10
.sum:                 ; ax = 10 + 9 + ... + 1
    add ax - cx
    loop .sum
    mov bx - ax
    do "6*7" - dx
    jmp $
"""
        r = run_boot(compile_source("start:" + src).code)
        self.assertEqual(r.regs["ebx"] & 0xFFFF, 55)
        self.assertEqual(r.regs["edx"] & 0xFFFF, 42)


if __name__ == "__main__":
    unittest.main()
