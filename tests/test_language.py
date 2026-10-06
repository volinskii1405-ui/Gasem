"""Тесты языка Gasem: директивы, данные, do, метки, ошибки."""

import os
import tempfile
import unittest

from gasem import GasemErrors, compile_source


def build(src):
    return compile_source(src).code


def hexs(src):
    return build(src).hex(" ")


class ErrorsMixin:
    def assertError(self, src, fragment, line=None):
        with self.assertRaises(GasemErrors) as cm:
            compile_source(src)
        messages = [e.message for e in cm.exception.errors]
        self.assertTrue(any(fragment in m for m in messages),
                        f"нет ошибки с «{fragment}», есть: {messages}")
        if line is not None:
            self.assertEqual(cm.exception.errors[0].loc.line, line)


class DirectivesTest(unittest.TestCase, ErrorsMixin):
    def test_og_sets_addresses(self):
        res = compile_source("og 0x7C00\nmov si - msg\nmsg b: 1\n")
        self.assertEqual(res.code, bytes([0xBE, 0x03, 0x7C, 0x01]))
        self.assertEqual(res.symbols["msg"], 0x7C03)
        self.assertEqual(res.origin, 0x7C00)

    def test_bits(self):
        self.assertEqual(hexs("b 16\nmov eax - 1"), "66 b8 01 00 00 00")
        self.assertEqual(hexs("b 32\nmov eax - 1"), "b8 01 00 00 00")
        self.assertEqual(hexs("b 32\nmov ax - 1"), "66 b8 01 00")

    def test_bits_switch_midway(self):
        self.assertEqual(hexs("b 16\ninc ax\nb 32\ninc ax"), "40 66 40")

    def test_bad_bits(self):
        self.assertError("b 64", "b 16 и b 32")

    def test_og_twice(self):
        self.assertError("og 0\nog 1", "только один раз")

    def test_og_after_code(self):
        self.assertError("nop\nog 0x7C00", "до первой команды")

    def test_align(self):
        self.assertEqual(hexs("nop\nalign 4\nnop"), "90 90 90 90 90")
        self.assertEqual(hexs("nop\nalign 4 - 0\nb: 1"), "90 00 00 00 01")
        self.assertError("align 3", "степенью двойки")

    def test_dollar(self):
        self.assertEqual(hexs("og 0x100\nnop\nw: $\nw: $$"), "90 01 01 00 01")

    def test_comments_and_blank_lines(self):
        self.assertEqual(hexs("; комментарий\n\n   nop ; ещё\n"), "90")


class DataTest(unittest.TestCase, ErrorsMixin):
    def test_b_colon_empty(self):
        res = compile_source("buf b:\nnop")
        self.assertEqual(res.code, b"\x90")
        self.assertEqual(res.symbols["buf"], 0)

    def test_b_string(self):
        self.assertEqual(build('b: "Hello"'), b"Hello")
        self.assertEqual(build('msg b: "Hello" - 0'), b"Hello\0")
        self.assertEqual(build("b: 'A' - 0x42 - 67"), b"ABC")

    def test_escapes(self):
        self.assertEqual(build(r'b: "a\n\r\t\0\\\"\x41"'), b'a\n\r\t\0\\"A')

    def test_utf8_string(self):
        self.assertEqual(build('b: "Привет"'), "Привет".encode("utf-8"))

    def test_b_dash_n(self):
        self.assertEqual(build("b-5"), bytes(5))
        self.assertEqual(build("buf b-3"), bytes(3))
        self.assertEqual(build("w-3"), bytes(6))
        self.assertEqual(build("d-2"), bytes(8))
        self.assertEqual(build("N = 4\nb-N*2"), bytes(8))

    def test_b_slash_n(self):
        self.assertEqual(build('b/ 8: "Hi"'), b"Hi" + bytes(6))
        self.assertEqual(build('b/ 3: 1 - 2 - 3'), b"\x01\x02\x03")
        self.assertEqual(build('w/ 2: 0x1234'), b"\x34\x12\x00\x00")
        self.assertEqual(build('d/ 2: 1'), b"\x01" + bytes(7))
        self.assertEqual(build('b/ 4 "ab"'), b"ab\0\0")

    def test_b_slash_errors(self):
        self.assertError("b/ 4", "требует инициализации")
        self.assertError("b/ 0: 1", "больше нуля")
        self.assertError('b/ 2: "abc"', "не помещается")

    def test_words_and_dwords(self):
        self.assertEqual(build("w: 0xAA55"), b"\x55\xAA")
        self.assertEqual(build("w: 1 - 2"), b"\x01\x00\x02\x00")
        self.assertEqual(build("d: 0x12345678"), b"\x78\x56\x34\x12")
        self.assertEqual(build("q: 0x00CF9A000000FFFF"), bytes.fromhex("ffff0000009acf00"))
        self.assertEqual(build('w: "abc"'), b"abc\0")
        self.assertEqual(build("w: -1"), b"\xff\xff")

    def test_data_overflow(self):
        self.assertError("b: 256", "не помещается")
        self.assertError("w: 0x10000", "не помещается")

    def test_repeat(self):
        self.assertEqual(build("&& 3 b: 0"), bytes(3))
        self.assertEqual(build("&& 2 w: 0xAA55"), b"\x55\xAA" * 2)
        self.assertEqual(build("&& 3 nop"), b"\x90" * 3)
        self.assertEqual(build("&& 0 nop"), b"")

    def test_boot_sector_padding(self):
        code = build("og 0x7C00\njmp $\n&& 510-($-$$) b: 0\nw: 0xAA55")
        self.assertEqual(len(code), 512)
        self.assertEqual(code[:2], b"\xeb\xfe")
        self.assertEqual(code[-2:], b"\x55\xaa")

    def test_boot_sector_overflow(self):
        self.assertError("og 0x7C00\n&& 600 nop\n&& 510-($-$$) b: 0", "загрузочный сектор")

    def test_data_label_on_previous_line(self):
        res = compile_source("msg:\n    b: \"Hi\" - 0\ndo \"[msg] + 1\" - ax")
        self.assertIn(b"Hi1\0", res.code)


class DoTest(unittest.TestCase, ErrorsMixin):
    VARS = '\njmp $\nmsg b: "Hello" - 0\nf b: "World" - 0\n'

    def strings_loaded(self, src):
        """Вернуть строку, адрес которой do кладёт в приёмник (через листинг кода)."""
        res = compile_source("og 0x7C00\n" + src + self.VARS)
        code = res.code
        # do со строкой: EB len <строка\0> B8 addr
        self.assertEqual(code[0], 0xEB)
        n = code[1]
        text = code[2:2 + n]
        mov = code[2 + n:2 + n + 3]
        self.assertEqual(mov[0], 0xB8)
        self.assertEqual(int.from_bytes(mov[1:3], "little"), 0x7C02)
        return text

    def test_number(self):
        self.assertEqual(hexs('do "4+4" - ax'), "b8 08 00")
        self.assertEqual(hexs('do "(1 << 4) | 3" - bl'), "b3 13")
        self.assertEqual(hexs('do "10 - 2*3" - cx'), "b9 04 00")

    def test_whole_string(self):
        self.assertEqual(self.strings_loaded('do "[msg]" - ax'), b"Hello\0")

    def test_string_plus_number(self):
        self.assertEqual(self.strings_loaded('do "[msg] + 1" - ax'), b"Hello1\0")

    def test_string_plus_string(self):
        self.assertEqual(self.strings_loaded('do "[msg] + [f]" - ax'), b"HelloWorld\0")

    def test_string_literal(self):
        self.assertEqual(self.strings_loaded("do \"'Hi, ' + [f]\" - ax"), b"Hi, World\0")

    def test_address(self):
        res = compile_source('og 0x7C00\ndo "msg" - si\ndo "msg + 2" - bx\nmsg b: 1')
        self.assertEqual(res.code[:6], bytes([0xBE, 0x06, 0x7C, 0xBB, 0x08, 0x7C]))

    def test_number_variable(self):
        self.assertEqual(hexs('do "[n] * 2" - ax\nn w: 21'), "b8 2a 00 15 00")

    def test_same_string_stored_once(self):
        code = build('og 0x7C00\ndo "[msg]" - ax\ndo "[msg]" - si' + self.VARS)
        self.assertEqual(code.count(b"Hello\0"), 2)   # копия от do + сама переменная

    def test_memory_destination(self):
        self.assertEqual(hexs('do "2+3" - word [bx]'), "c7 07 05 00")

    def test_errors(self):
        self.assertError('do 4+4 - ax', "кавычках")
        self.assertError('do "4+4"', "приёмник")
        self.assertError('do "[msg] - 1" - ax' + self.VARS, "не применима")
        self.assertError('do "ax + 1" - bx', "вычисляется при компиляции")
        self.assertError('do "[nope]" - ax', "неизвестная переменная")
        self.assertError('lbl: nop\ndo "[lbl]" - ax', "не переменная")
        self.assertError('do "300" - al', "не помещается")


class InstructionsTest(unittest.TestCase, ErrorsMixin):
    def test_gasem_aliases(self):
        self.assertEqual(hexs("nxtb\nchk al - al"), "ac 84 c0")
        self.assertEqual(hexs("l: jfnz l"), "75 fe")
        self.assertEqual(hexs("l: jfz l\njfc l\njfe l"), "74 fe 72 fc 74 fa")

    def test_a20_on(self):
        self.assertEqual(hexs("mov a20 - 1"), "50 e4 92 0c 02 24 fe e6 92 58")

    def test_a20_off(self):
        self.assertEqual(hexs("mov a20 - 0"), "50 e4 92 24 fc e6 92 58")

    def test_a20_errors(self):
        self.assertError("mov a20 - 5", "только 0")
        self.assertError("mov ax - a20", "можно только переключать")

    def test_jump_grows_when_far(self):
        self.assertEqual(build("jmp t\n&& 10 nop\nt:")[:2], b"\xeb\x0a")
        self.assertEqual(build("jmp t\n&& 200 nop\nt:")[:3], b"\xe9\xc8\x00")
        self.assertEqual(build("jz t\n&& 200 nop\nt:")[:4], b"\x0f\x84\xc8\x00")

    def test_chain_of_jumps_converges(self):
        src = "".join(f"jmp l{i}\n" for i in range(40)) + "&& 100 nop\n"
        src += "".join(f"l{i}: nop\n" for i in range(40))
        code = build(src)
        self.assertEqual(len(code), 40 * 3 + 100 + 40)

    def test_far_jump(self):
        self.assertEqual(hexs("jmp 0x08:0x7C50"), "ea 50 7c 08 00")

    def test_segment_prefix_forms(self):
        self.assertEqual(hexs("mov ax - [es:di]"), hexs("mov ax - es:[di]"))
        self.assertEqual(hexs("es movsb"), "26 a4")

    def test_errors(self):
        self.assertError("mov ax, 1", "а не запятой")
        self.assertError("mov ax-1", "дефис с пробелами")
        self.assertError("mov ax - - 1", "пустой операнд")
        self.assertError("print", "добавьте двоеточие")
        self.assertError("frob ax", "неизвестная команда")
        self.assertError("mov [bx] - 5", "укажите byte, word или dword")
        self.assertError("mov al - 300", "не помещается")
        self.assertError("mov ds - 5", "прямо в ds")
        self.assertError("mov ax - [bx+cx]", "16-битный адрес")
        self.assertError("mov ax - bl", "не совпадают")
        self.assertError("add [bx] - [si]", "два операнда в памяти")
        self.assertError("jmp short t\n&& 200 nop\nt:", "короткого перехода")
        self.assertError("jmp nowhere", "неизвестное имя")
        self.assertError("mov cs - ax", "cs нельзя")
        self.assertError('mov ax - "abc', "незакрытая строка")


class LabelsTest(unittest.TestCase, ErrorsMixin):
    def test_forward_and_backward(self):
        self.assertEqual(hexs("x: jmp y\ny: jmp x"), "eb 00 eb fc")

    def test_local_labels(self):
        src = "f1:\n.l: nop\njmp .l\nf2:\n.l: nop\njmp .l\njmp f1.l"
        self.assertEqual(hexs(src), "90 eb fd 90 eb fd eb f8")

    def test_constants(self):
        self.assertEqual(hexs("X = 5\nY equ X*2+1\nmov ax - Y"), "b8 0b 00")

    def test_forward_constant(self):
        self.assertEqual(hexs("mov bx - SIZE\nSIZE = e-s\ns: nop\ne:"), "bb 01 00 90")

    def test_label_with_instruction(self):
        self.assertEqual(hexs("start: mov al - 1\njmp start"), "b0 01 eb fc")

    def test_cyrillic_names(self):
        self.assertEqual(hexs("og 0x100\nmov si - привет\nпривет b: 1"), "be 03 01 01")

    def test_errors(self):
        self.assertError("l:\nl:", "уже определено", line=2)
        self.assertError("ax: nop", "зарезервированное")
        self.assertError(".x: nop", "без предшествующей глобальной")

    def test_several_errors_reported(self):
        with self.assertRaises(GasemErrors) as cm:
            compile_source("frob\nmov ax, 1\nmov al - 1\nqux")
        self.assertEqual([e.loc.line for e in cm.exception.errors], [1, 2, 4])


class FilesTest(unittest.TestCase, ErrorsMixin):
    def test_include_and_incbin(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "lib.gsm"), "w", encoding="utf-8") as f:
                f.write("helper:\n    ret\n")
            with open(os.path.join(d, "blob.bin"), "wb") as f:
                f.write(b"\x01\x02\x03\x04")
            src = 'call helper\ninclude "lib.gsm"\nincbin "blob.bin"\nincbin "blob.bin" - 1 - 2\n'
            code = compile_source(src, os.path.join(d, "main.gsm"), d).code
            self.assertEqual(code.hex(" "), "e8 00 00 c3 01 02 03 04 02 03")

    def test_missing_include(self):
        self.assertError('include "нет-такого.gsm"', "не удалось открыть")

    def test_listing(self):
        res = compile_source("og 0x7C00\nstart: xor ax - ax\njmp start\n")
        self.assertIn("00007C00  31C0", res.listing)
        self.assertIn("00007C02  EBFC", res.listing)
        self.assertIn("start", res.listing)


class CliTest(unittest.TestCase):
    def test_compile_file(self):
        from gasem.cli import main
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "boot.gsm")
            with open(src, "w", encoding="utf-8") as f:
                f.write("og 0x7C00\njmp $\n&& 510-($-$$) b: 0\nw: 0xAA55\n")
            lst = os.path.join(d, "boot.lst")
            self.assertEqual(main([src, "-q", "-l", lst]), 0)
            with open(os.path.join(d, "boot.bin"), "rb") as f:
                self.assertEqual(len(f.read()), 512)
            self.assertTrue(os.path.exists(lst))

    def test_compile_error_exit_code(self):
        from gasem.cli import main
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "bad.gsm")
            with open(src, "w", encoding="utf-8") as f:
                f.write("mov ax, 1\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main([src]), 1)
            self.assertIn("bad.gsm:1: ошибка", err.getvalue())
            self.assertFalse(os.path.exists(os.path.join(d, "bad.bin")))


if __name__ == "__main__":
    unittest.main()
