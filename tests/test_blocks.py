"""macro, proc, struct, at: код выполняется в эмуляторе, проверяются
развёртка, адреса, сообщения об ошибках и листинг."""

import os
import tempfile
import unittest

from gasem import GasemErrors, compile_file, compile_source

from test_highlevel import dword, run, run_symbols, unicorn


def errors(src):
    try:
        compile_source(src)
    except GasemErrors as e:
        return [x.format() for x in e.errors]
    raise AssertionError("ожидалась ошибка:\n" + src)


def code(src):
    return compile_source(src).code


@unittest.skipUnless(unicorn, "unicorn не установлен")
class MacroRunTest(unittest.TestCase):
    def test_params_and_own_labels(self):
        src = """
macro sum_to - reg - n          ; reg = 1 + 2 + ... + n
    xor reg - reg
    mov ecx - n
again:
    add reg - ecx
    loop again
end
sum_to eax - 10
sum_to ebx - 4
hlt
"""
        out, _ = run(src)
        self.assertEqual(out["eax"], 55)
        self.assertEqual(out["ebx"], 10)

    def test_nested_macros_and_blocks(self):
        src = """
macro clamp - reg - hi
    if signed reg > hi
        mov reg - hi
    end
end
macro clamp2 - x - y - hi
    clamp x - hi
    clamp y - hi
end
mov eax - 100
mov ebx - 5
clamp2 eax - ebx - 50
hlt
"""
        out, _ = run(src)
        self.assertEqual((out["eax"], out["ebx"]), (50, 5))

    def test_params_in_do_and_let(self):
        src = """
macro triple - dst - src
    let dst - "src * 3"
end
macro const - dst - n
    do "n * 2 + 1" - dst
end
mov ebx - 7
triple eax - ebx
const ecx - 20
hlt
"""
        out, _ = run(src)
        self.assertEqual((out["eax"], out["ecx"]), (21, 41))

    def test_strings_and_labels_as_arguments(self):
        src = """
macro load - reg - text
    mov reg - text
end
load esi - "Hi"
load edi - msg
hlt
msg: s: "x"
"""
        out, mem, syms = run_symbols(src)
        self.assertEqual(mem[out["esi"]:out["esi"] + 3], b"Hi\0")
        self.assertEqual(out["edi"], syms["msg"])

    def test_postfix_if(self):
        src = """
macro bump - reg
    inc reg
end
xor eax - eax
mov ebx - 1
bump eax if ebx = 1
bump eax if ebx = 2
hlt
"""
        out, _ = run(src)
        self.assertEqual(out["eax"], 1)


class MacroCompileTest(unittest.TestCase):
    def test_same_bytes_as_hand_written(self):
        macro = "macro put - c\n    mov al - c\n    int 0x10\nend\nput 'A'\nput 'B'\n"
        self.assertEqual(code(macro), code("mov al - 'A'\nint 0x10\nmov al - 'B'\nint 0x10\n"))

    def test_listing_shows_bytes_on_call_line(self):
        res = compile_source("macro two\n    nop\n    nop\nend\nstart: two\n")
        line = next(l for l in res.listing.splitlines() if "start: two" in l)
        self.assertIn("9090", line)
        self.assertEqual(res.lines[0][3].text, "start: two")    # отладчик шагает по строке вызова

    def test_errors_point_into_macro_and_name_the_call(self):
        errs = errors("macro bad - x\n    mov x - [bx + cl]\nend\nstart: bad ax\n")
        self.assertEqual(len(errs), 1)
        self.assertIn("<источник>:2: ошибка: регистр cl нельзя использовать в адресе", errs[0])
        self.assertIn("mov ax - [bx + cl]", errs[0])
        self.assertIn("(в макросе 'bad', вызванном в <источник>:4)", errs[0])

    def test_exponential_expansion_is_stopped(self):
        errs = errors("macro m\n    m\n    m\nend\nm\n")
        self.assertTrue(any("слишком много строк" in e or "слишком глубокая" in e for e in errs))
        self.assertLess(len(errs), 200)

    def test_wrong_argument_count(self):
        errs = errors("macro m - x - y\n    nop\nend\nm 1\n")
        self.assertIn("макрос m принимает 2 параметр(а) (x - y), а передано 1", errs[0])

    def test_recursion(self):
        errs = errors("macro m\n    m\nend\nm\n")
        self.assertTrue(any("слишком глубокая вложенность макросов" in e for e in errs))

    def test_definition_errors(self):
        self.assertIn("macro без end", errors("macro m\n    nop\n")[0])
        self.assertIn("команда Gasem", errors("macro mov\nend\n")[0])
        self.assertIn("уже объявлен", errors("macro m\nend\nmacro m\nend\n")[0])
        self.assertIn("не внутри if", errors("if al = 1\nmacro m\nend\nend\n")[0])
        self.assertIn("&&", errors("macro m\n    nop\nend\n&& 2 m\n")[0])
        self.assertEqual(len(errors("macro m - b\n    if b = 1\n    end\nend\nnop\n")), 1)   # тело пропущено

    def test_macro_from_included_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "lib.gsm"), "w", encoding="utf-8") as f:
                f.write("macro twice - x\n    shl x - 1\nend\n")
            res = compile_source('include "lib.gsm"\ntwice ax\n', base_dir=tmp)
            self.assertEqual(res.code, bytes([0xD1, 0xE0]))

    def test_internal_labels_do_not_warn_or_break_locals(self):
        src = "start:\nmacro m\nx:\n    jmp x\nend\nm\n.loc:\n    jmp .loc\n"
        res = compile_source(src)
        self.assertEqual(res.warnings, [])
        self.assertIn("start.loc", res.symbols)


@unittest.skipUnless(unicorn, "unicorn не установлен")
class ProcRunTest(unittest.TestCase):
    def test_locals_uses_and_return(self):
        src = """
mov esi - text
mov ebx - 0x1234
call strlen
hlt

proc strlen - esi uses ebx - esi
    local count - dword
    mov [count] - 0
    while byte [esi] != 0
        inc [count]
        inc esi
    end
    mov ebx - 0           ; ebx восстановится из uses
    return [count]
end
text: s: "Hello!"
"""
        out, _ = run(src)
        self.assertEqual(out["eax"], 6)
        self.assertEqual(out["ebx"], 0x1234)
        self.assertEqual(out["esp"], 0x8000)

    def test_call_with_arguments_and_early_return(self):
        src = """
call max - 7 - 3
mov edi - eax
call max - 2 - 9
hlt

proc max - eax - ebx
    return if signed eax >= ebx
    return ebx
end
"""
        out, _ = run(src)
        self.assertEqual((out["edi"], out["eax"]), (7, 9))

    def test_arrays_and_structs_in_locals_16bit(self):
        src = """
call fill
hlt

struct Pair
    a: w
    b: w
end
proc fill uses bx
    local buf - byte 4
    local p - Pair
    mov byte [buf] - 1
    mov byte [buf + 3] - 4
    mov word [p + Pair.b] - 0x55
    mov al - [buf]
    add al - [buf + 3]
    mov dx - [p + Pair.b]
    mov bx - 0
end
"""
        out, _ = run(src, bits=16, regs={"ebx": 0x77})
        self.assertEqual(out["eax"] & 0xFF, 5)
        self.assertEqual(out["edx"] & 0xFFFF, 0x55)
        self.assertEqual(out["ebx"] & 0xFFFF, 0x77)

    def test_no_frame_without_locals(self):
        self.assertEqual(code("b 32\nproc f uses ebx\n    xor ebx - ebx\nend\n"),
                         bytes([0x53, 0x31, 0xDB, 0x5B, 0xC3]))
        self.assertEqual(code("proc f\nend\n"), bytes([0xC3]))

    def test_proc_errors(self):
        self.assertIn("return вне proc", errors("return\n")[0])
        self.assertIn("local пишется в начале proc", errors("proc f\n    nop\n    local x - dword\nend\n")[0])
        self.assertIn("лежит в стеке", errors("b 32\nproc f\n    local x - dword\n    mov eax - x\nend\n")[0])
        self.assertIn("затрёт результат", errors("b 32\nproc f uses eax\n    return 1\nend\n")[0])
        self.assertIn("proc без end", errors("proc f\n    nop\n")[0])
        self.assertIn("не внутри if", errors("proc f\nproc g\nend\nend\n")[0])


class StructTest(unittest.TestCase):
    def test_offsets_sizes_nested(self):
        res = compile_source("struct P\n    x: w\n    y: w\nend\nstruct R\n    a: P\n    b: P\n"
                             "    tag: b 3\n    n: d\nend\n")
        s = res.symbols
        self.assertEqual((s["P.x"], s["P.y"], s["P.size"]), (0, 2, 4))
        self.assertEqual((s["R.a"], s["R.b"], s["R.b.y"], s["R.tag"], s["R.n"], s["R.size"]), (0, 4, 6, 8, 11, 15))

    def test_instances(self):
        src = 'struct E\n    id: b\n    name: b 4\n    n: w\nend\ne1: E 7 - "ab" - 0x1234\ne2: E 1\n&& 2 E\n'
        self.assertEqual(code(src), bytes([7]) + b"ab\0\0" + bytes([0x34, 0x12]) + bytes([1]) + bytes(6)
                         + bytes(14))

    def test_struct_errors(self):
        self.assertIn("поле структуры", errors("struct T\n    x w\nend\n")[0])
        self.assertIn("уже есть", errors("struct T\n    x: b\n    x: w\nend\n")[0])
        self.assertIn("не может называться 'size'", errors("struct T\n    size: b\nend\n")[0])
        self.assertIn("2 пол", errors("struct T\n    x: b\n    y: b\nend\nT 1 - 2 - 3\n")[0])
        self.assertIn("struct без end", errors("struct T\n    x: b\n")[0])


class AtTest(unittest.TestCase):
    def test_virtual_addresses(self):
        src = "og 0x7C00\nstart: jmp start\nat 0x50000\nb 32\napp: jmp app\n    mov eax - $$\n    call app\nend\nafter: nop\n"
        res = compile_source(src)
        self.assertEqual(res.symbols["app"], 0x50000)
        self.assertEqual(res.symbols["after"], 0x7C00 + 2 + 2 + 5 + 5)
        self.assertEqual(res.code[4:9], bytes([0xB8, 0x00, 0x00, 0x05, 0x00]))   # $$ = адрес блока
        self.assertEqual(res.code[9:14], bytes([0xE8, 0xF4, 0xFF, 0xFF, 0xFF]))  # call app (относительно)

    def test_og_inside_at(self):
        res = compile_source("og 0x7C00\nnop\nat 0x500\nog 0x500\nx: nop\nend\n")
        self.assertEqual(res.symbols["x"], 0x500)
        self.assertIn("og внутри at должен совпадать", errors("og 0x7C00\nnop\nat 0x500\nog 0x600\nend\n")[0])

    def test_bad_address_does_not_break_end(self):
        errs = errors("og 0x7C00\nat nothing\nnop\nend\nnop\n")
        self.assertEqual(len(errs), 1)
        self.assertIn("адрес at должно быть известно", errs[0])

    def test_listing_and_debug_lines_use_virtual_addresses(self):
        res = compile_source("og 0x7C00\nat 0x9000\nnop\nend\n")
        self.assertIn("00009000  90", res.listing)
        self.assertEqual(res.lines[0][0], 0x9000)


class IncprogTest(unittest.TestCase):
    def write(self, tmp, name, text):
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_separate_program_with_own_origin_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "app.gsm", "og 0x50000\nb 32\nstart: jmp start\n    mov eax - start\n")
            res = compile_source('og 0x7C00\nstart: nop\napp: incprog "app.gsm"\napp_end:\nw: app_end-app\n',
                                 base_dir=tmp)
            app = bytes([0xEB, 0xFE, 0xB8, 0x00, 0x00, 0x05, 0x00])
            self.assertEqual(res.code, b"\x90" + app + bytes([len(app), 0]))
            self.assertEqual(res.symbols["start"], 0x7C00)      # имена программы не смешиваются

    def test_errors_and_warnings_come_from_the_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "bad.gsm", "nop\nmov ax - [bx + cl]\n")
            self.write(tmp, "warn.gsm", "start: nop\nunused: nop\n")
            self.write(tmp, "self.gsm", 'incprog "self.gsm"\n')
            errs = errors_in(tmp, 'incprog "bad.gsm"\n')
            self.assertEqual(len(errs), 1)
            self.assertIn("bad.gsm:2: ошибка: регистр cl нельзя использовать в адресе", errs[0])
            res = compile_source('incprog "warn.gsm"\n', base_dir=tmp)
            self.assertIn("метка 'unused' нигде не используется", res.warnings[0].format())
            with self.assertRaises(GasemErrors) as cm:
                compile_file(os.path.join(tmp, "self.gsm"))
            self.assertIn("собирает саму себя через incprog", cm.exception.errors[0].format())
            self.assertIn("не удалось открыть файл", errors_in(tmp, 'incprog "nope.gsm"\n')[0])
            self.assertIn("&&", errors_in(tmp, '&& 2 incprog "warn.gsm"\n')[0])


def errors_in(tmp, src):
    try:
        compile_source(src, base_dir=tmp)
    except GasemErrors as e:
        return [x.format() for x in e.errors]
    raise AssertionError("ожидалась ошибка:\n" + src)


if __name__ == "__main__":
    unittest.main()
