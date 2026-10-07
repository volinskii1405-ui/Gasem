"""s:, списки в push/pop, условие в конце строки, строки в командах,
call с аргументами, подсказки и предупреждения."""

import unittest

from gasem import GasemErrors, compile_source

from test_highlevel import run, unicorn


def build(src):
    return compile_source(src).code


def errors(src):
    with unittest.TestCase().assertRaises(GasemErrors) as cm:
        compile_source(src)
    return [e.message for e in cm.exception.errors]


def c_string(mem, addr):
    return mem[addr:mem.index(b"\0", addr)]


class StringDataTest(unittest.TestCase):
    def test_s_data(self):
        self.assertEqual(build('s: "Hello"'), b"Hello\0")
        self.assertEqual(build('msg s: "Hi" - 10'), b"Hi\n\0")
        self.assertEqual(build('s: 1 - 2'), b"\x01\x02\0")
        self.assertEqual(build('s:'), b"\0")

    def test_s_is_reserved(self):
        self.assertTrue(any("s: — строка" in m for m in errors("s-4")))
        self.assertTrue(any("зарезервированное" in m for m in errors("s b: 1")))

    def test_do_reads_s_variable(self):
        code = build('og 0x100\ndo "[m] + 1" - ax\nm s: "ab"')
        self.assertIn(b"ab1\0", code)


class PushPopListTest(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(build("push ax - bx - cx\npop ax - bx - cx"),
                         build("push ax\npush bx\npush cx\npop cx\npop bx\npop ax"))
        self.assertEqual(build("b 32\npush eax - word [x] - 5\nx d: 0"),
                         build("b 32\npush eax\npush word [x]\npush 5\nx d: 0"))

    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_restores_registers(self):
        out, _ = run("push eax - ebx - ecx\nxor eax - eax\nmov ebx - 7\nmov ecx - 9\n"
                     "pop eax - ebx - ecx\nhlt", regs={"eax": 1, "ebx": 2, "ecx": 3})
        self.assertEqual((out["eax"], out["ebx"], out["ecx"]), (1, 2, 3))

    def test_not_in_repeat(self):
        self.assertTrue(any("&&" in m for m in errors("&& 2 push ax - bx")))


class PostfixIfTest(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(build("ret if carry"), bytes.fromhex("73 01 c3"))
        self.assertEqual(build("inc bx if zero"), bytes.fromhex("75 01 43"))
        # jmp ... if — это один условный переход
        self.assertEqual(build("l: jmp l if al = 27"), bytes.fromhex("3c 1b 74 fc"))

    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_semantics(self):
        src = """
xor eax - eax
for ecx - 0 - 20
    continue if ecx & 1         ; нечётные пропускаем
    break if ecx = 12
    add eax - ecx
end
mov ebx - 5
dec ebx if eax = 30             ; 0+2+4+6+8+10 = 30
hlt"""
        src = src.replace("continue if ecx & 1", "chk ecx - 1\n    continue if not zero")
        out, _ = run(src)
        self.assertEqual(out["eax"], 30)
        self.assertEqual(out["ebx"], 4)

    def test_errors(self):
        self.assertTrue(any("ожидается условие" in m for m in errors("ret if")))
        self.assertTrue(any("нельзя дополнить" in m for m in errors("end if zero")))
        self.assertTrue(any("нельзя сочетать с &&" in m for m in errors("&& 3 nop if zero")))
        self.assertTrue(any("break вне цикла" in m for m in errors("break if zero")))


class StringOperandTest(unittest.TestCase):
    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_inline_strings_without_pool(self):
        # без pool строка кладётся прямо в код, и выполнение её обходит
        out, mem = run('mov esi - "Hello"\nmov edi - "World"\nmov ebx - "Hello"\ninc ecx\nhlt',
                       regs={"ecx": 0})
        self.assertEqual(c_string(mem, out["esi"]), b"Hello")
        self.assertEqual(c_string(mem, out["edi"]), b"World")
        self.assertEqual(out["ebx"], out["esi"])          # одинаковые строки — одна копия
        self.assertEqual(out["ecx"], 1)                   # код после строк выполнился

    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_pool(self):
        src = 'mov esi - "first"\nmov edi - "second"\nhlt\npool\nmov ebx - "later"\nhlt'
        res = compile_source("og 0x1000\nb 32\n" + src)
        out, mem = run(src)
        self.assertEqual(c_string(mem, out["esi"]), b"first")
        self.assertEqual(c_string(mem, out["edi"]), b"second")
        self.assertIn(b"first\0second\0", res.code)        # обе строки — в pool, подряд
        self.assertEqual(res.code.count(b"first"), 1)

    def test_several_pools(self):
        code = build('mov ax - "a1"\npool\nmov ax - "b2"\npool')
        self.assertLess(code.index(b"a1\0"), code.index(b"b2\0"))

    def test_char_vs_string(self):
        self.assertEqual(build("mov al - 'A'"), bytes.fromhex("b0 41"))
        self.assertTrue(any("одинарных кавычках" in m for m in errors('mov al - "A"')))
        self.assertTrue(any("одинарных кавычках" in m for m in errors('cmp byte [bx] - "A"')))
        self.assertTrue(any("одинарных кавычках" in m for m in errors('if al = "A"\nend')))


class CallArgsTest(unittest.TestCase):
    def test_default_registers(self):
        self.assertEqual(build("b 32\ncall f - 1 - 2\nf: ret"),
                         build("b 32\nmov eax - 1\nmov ebx - 2\ncall f\nf: ret"))
        self.assertEqual(build("call f - 1\nf: ret"), build("mov ax - 1\ncall f\nf: ret"))

    def test_declared_registers(self):
        self.assertEqual(build("b 32\ncall f - 7 - 3\nargs f - esi - cl\nf: ret"),
                         build("b 32\nmov esi - 7\nmov cl - 3\ncall f\nf: ret"))

    def test_tail_jump_and_widening(self):
        self.assertEqual(build("b 32\njmp f - bl\nargs f - eax\nf: ret"),
                         build("b 32\nmovzx eax - bl\njmp f\nf: ret"))

    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_order_of_moves(self):
        # eax нужен для ebx, поэтому сначала mov ebx - eax, потом mov eax - 5
        src = "call f - 5 - eax\nhlt\nargs f - eax - ebx\nf:\n    ret"
        out, _ = run(src, regs={"eax": 9})
        self.assertEqual((out["eax"], out["ebx"]), (5, 9))

    def test_errors(self):
        self.assertTrue(any("принимает 1" in m for m in errors("call f - 1 - 2\nargs f - esi\nf: ret")))
        self.assertTrue(any("по кругу" in m for m in errors("call f - ebx - eax\nargs f - eax - ebx\nf: ret")))
        self.assertTrue(any("уже объявлены" in m for m in errors("args f - eax\nargs f - ebx\nf: ret")))
        self.assertTrue(any("регистры общего" in m for m in errors("args f - ds\nf: ret")))


class HintsTest(unittest.TestCase):
    def first(self, src):
        return errors(src)[0]

    def test_names(self):
        self.assertIn("может быть, 'print'", self.first("jmp prnit\nprint: ret"))
        self.assertIn("может быть, 'msg'", self.first("mov si - Msg\nmsg s: 1"))
        self.assertIn("может быть, регистр 'ebx'", self.first("b 32\nmov eax - ebz"))
        self.assertIn("может быть, 'msg'", self.first('do "[mgs]" - ax\nmsg s: "x"'))

    def test_commands(self):
        self.assertIn("может быть, 'mov'", self.first("mvo ax - 1"))
        self.assertIn("может быть, 'nxtb'", self.first("nxbt"))
        self.assertIn("может быть, 'jfnz'", self.first("jnfz x\nx:"))
        self.assertIn("добавьте двоеточие", self.first("print"))


class WarningsTest(unittest.TestCase):
    def test_unused_label(self):
        res = compile_source("start:\n    call f\n    ret\nf: ret\nunused: nop\n.tmp: nop")
        msgs = [w.message for w in res.warnings]
        self.assertEqual(msgs, ["метка 'unused' нигде не используется",
                                "метка 'unused.tmp' нигде не используется"])

    def test_variables_used_by_do_count(self):
        res = compile_source('do "[msg]" - ax\nmsg s: "x"')
        self.assertEqual(res.warnings, [])


if __name__ == "__main__":
    unittest.main()
