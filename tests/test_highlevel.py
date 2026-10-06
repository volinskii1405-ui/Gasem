"""if / while / for / repeat / let: код запускается в эмуляторе x86 и
результат сверяется с ожидаемым (для let — со случайными выражениями)."""

import random
import unittest

from gasem import GasemErrors, compile_source

try:
    import unicorn
    from unicorn import x86_const as X
except ImportError:  # pragma: no cover
    unicorn = None

REGS32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
REGS16 = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"]
REGS8 = ["al", "cl", "dl", "bl", "ah", "ch", "dh", "bh"]
ORG = 0x1000
STACK = 0x8000


def run(src, bits=32, regs=None):
    """Собрать и выполнить код до hlt. → (регистры, память)."""
    code = compile_source(f"og {ORG}\nb {bits}\n{src}\n").code
    mu = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_32 if bits == 32 else unicorn.UC_MODE_16)
    mu.mem_map(0, 0x10000)
    mu.mem_write(ORG, code)
    for name, value in (regs or {}).items():
        mu.reg_write(getattr(X, "UC_X86_REG_" + name.upper()), value)
    mu.reg_write(X.UC_X86_REG_ESP, STACK)

    def on_code(uc, address, size, _):
        if uc.mem_read(address, 1)[0] == 0xF4:
            uc.emu_stop()

    mu.hook_add(unicorn.UC_HOOK_CODE, on_code)
    mu.emu_start(ORG, ORG + len(code), count=200000)
    out = {r: mu.reg_read(getattr(X, "UC_X86_REG_" + r.upper())) for r in REGS32}
    return out, bytes(mu.mem_read(0, 0x10000))


def run_symbols(src, bits=32, regs=None):
    res = compile_source(f"og {ORG}\nb {bits}\n{src}\n")
    out, mem = run(src, bits, regs)
    return out, mem, res.symbols


def dword(mem, addr):
    return int.from_bytes(mem[addr:addr + 4], "little")


@unittest.skipUnless(unicorn, "unicorn не установлен")
class ControlFlowTest(unittest.TestCase):
    def check(self, src, expect, bits=32, regs=None):
        out, _ = run(src + "\nhlt", bits, regs)
        for reg, value in expect.items():
            self.assertEqual(out[reg] & 0xFFFFFFFF, value & 0xFFFFFFFF, f"{reg}\n{src}")

    def test_for_sum(self):
        self.check("xor eax - eax\nfor ecx - 1 - 11\n    add eax - ecx\nend", {"eax": 55, "ecx": 11})

    def test_for_step_and_countdown(self):
        self.check("xor eax - eax\nfor ecx - 0 - 10 - 2\n    add eax - ecx\nend", {"eax": 0 + 2 + 4 + 6 + 8})
        self.check("xor eax - eax\nfor ecx - 5 - 0 - -1\n    add eax - ecx\nend", {"eax": 15, "ecx": 0})

    def test_for_memory_counter(self):
        self.check("xor eax - eax\nfor dword [i] - 0 - 4\n    add eax - [i]\nend\nhlt\ni d: 0",
                   {"eax": 0 + 1 + 2 + 3})

    def test_signed_for(self):
        self.check("xor eax - eax\nfor signed ecx - -3 - 3\n    inc eax\nend", {"eax": 6})

    def test_break_continue_primes(self):
        src = """
xor eax - eax              ; число простых меньше 50
for ebx - 2 - 50
    mov edx - 1            ; edx = 1, если ebx простое
    for ecx - 2 - ebx
        let esi - "ebx % ecx"
        if esi = 0
            xor edx - edx
            break
        end
    end
    if edx = 0
        continue
    end
    inc eax
end"""
        self.check(src, {"eax": 15})

    def test_while_gcd(self):
        src = """
while ebx != 0
    let edx - "eax % ebx"
    mov eax - ebx
    mov ebx - edx
end"""
        self.check(src, {"eax": 6}, regs={"eax": 48, "ebx": 18})

    def test_if_chain(self):
        src = """
if al = 10
    mov ebx - 1
elif al >= 'a' and al <= 'z'
    mov ebx - 2
elif al < '0' or al > '9'
    mov ebx - 3
else
    mov ebx - 4
end"""
        for ch, want in ((10, 1), (ord("q"), 2), (ord("#"), 3), (ord("7"), 4), (ord("Z"), 3)):
            self.check(src, {"ebx": want}, regs={"eax": ch, "ebx": 0})

    def test_signed_and_unsigned(self):
        src = "xor ebx - ebx\nxor ecx - ecx\nif signed eax < 0\n    inc ebx\nend\nif eax < 0\n    inc ecx\nend"
        self.check(src, {"ebx": 1, "ecx": 0}, regs={"eax": 0xFFFFFFFB})

    def test_flags_and_truth(self):
        self.check("xor ebx - ebx\nchk al - al\nif zero\n    inc ebx\nend\nif not carry\n    inc ebx\nend",
                   {"ebx": 2}, regs={"eax": 0})
        self.check("xor ebx - ebx\nif eax\n    inc ebx\nend\nif not ecx\n    add ebx - 10\nend",
                   {"ebx": 11}, regs={"eax": 5, "ecx": 0})

    def test_while_one_and_repeat(self):
        self.check("xor eax - eax\nwhile 1\n    inc eax\n    if eax = 7\n        break\n    end\nend",
                   {"eax": 7})
        self.check("xor eax - eax\nrepeat\n    add eax - 3\nuntil eax >= 10", {"eax": 12})

    def test_string_loop(self):
        src = "mov esi - text\nxor ecx - ecx\nwhile byte [esi]\n    inc esi\n    inc ecx\nend\nhlt\ntext b: \"Gasem\" - 0"
        self.check(src, {"ecx": 5})

    def test_mirrored_compare(self):
        self.check("xor ebx - ebx\nif 5 < eax\n    inc ebx\nend", {"ebx": 1}, regs={"eax": 9})

    def test_16bit(self):
        self.check("xor ax - ax\nfor cx - 1 - 101\n    add ax - cx\nend", {"eax": 5050}, bits=16)
        # беззнаковое сравнение по умолчанию: 0xFA00 > 0 даже в 16 битах
        self.check("xor bx - bx\nmov di - 0xFA00\nif di > 100\n    inc bx\nend", {"ebx": 1}, bits=16)

    def test_constants_with_spaced_minus(self):
        self.check("N = 10 - 3\nmov eax - N", {"eax": 7})


class ControlFlowErrorsTest(unittest.TestCase):
    def assertError(self, src, fragment):
        with self.assertRaises(GasemErrors) as cm:
            compile_source(src)
        msgs = [e.message for e in cm.exception.errors]
        self.assertTrue(any(fragment in m for m in msgs), msgs)

    def test_errors(self):
        self.assertError("if al = 1\nnop", "if без end")
        self.assertError("end", "end без if")
        self.assertError("else", "else без if")
        self.assertError("if al = 1\nelse\nelse\nend", "второй else")
        self.assertError("if al = 1\nelse\nelif al = 2\nend", "elif после else")
        self.assertError("break", "break вне цикла")
        self.assertError("until zero", "until без repeat")
        self.assertError("repeat\nnop\nend", "закрывается словом until")
        self.assertError("while\nend", "ожидается условие")
        self.assertError("for ecx - 0\nend", "формат: for")
        self.assertError("for ecx - 0 - 10 - 0\nend", "ненулевое")
        self.assertError("if 1 = 2\nend", "два числа")
        self.assertError("if not al = 1\nend", "not перед сравнением")
        self.assertError("&& 2 if al = 1", "нельзя повторять")

    def test_let_errors(self):
        self.assertError('let eax - 5', "формат: let")
        self.assertError("let eax - 'x'", "двойных кавычках")
        self.assertError('let ds - "1+1"', "приёмник let")
        self.assertError('let eax - "esp + 4"', "esp нельзя")
        self.assertError('let [x] - "eax"\nx d: 0', "укажите размер")
        self.assertError('let ax - "dword [x]"\nx d: 0', "не помещается")


# ---------------------------------------------------------------- случайные let

class Model:
    """Случайное выражение для let + его значение, посчитанное на Python."""

    def __init__(self, rnd, width, signed, regs, mem):
        self.rnd, self.width, self.signed = rnd, width, signed
        self.mask = (1 << width) - 1
        self.regs, self.mem = regs, mem

    def s(self, v):
        v &= self.mask
        return v - (1 << self.width) if self.signed and v >> (self.width - 1) else v

    def ext(self, v, bits):
        v &= (1 << bits) - 1
        if self.signed and v >> (bits - 1):
            v -= 1 << bits
        return v & self.mask

    def leaf(self):
        r = self.rnd.random()
        names = REGS32 if self.width == 32 else REGS16
        if r < 0.45:
            i = self.rnd.choice([0, 1, 2, 3, 5, 6, 7])
            return names[i], self.regs[REGS32[i]] & self.mask
        if r < 0.6:
            i = self.rnd.randrange(8)
            v = self.regs[REGS32[i & 3]] >> (8 if i >= 4 else 0)
            return REGS8[i], self.ext(v, 8)
        if r < 0.75:
            name = self.rnd.choice(["v0", "v1"])
            size = 32 if self.width == 32 else 16
            word = "dword" if size == 32 else "word"
            return f"{word} [{name}]", self.mem[name] & self.mask
        if r < 0.85:
            return "byte [b0]", self.ext(self.mem["b0"], 8)
        return None  # число

    def expr(self, depth):
        if depth == 0 or self.rnd.random() < 0.25:
            leaf = self.leaf()
            if leaf is None:
                v = self.rnd.randrange(0, 300)
                return str(v), v, True
            return leaf[0], leaf[1], False
        if self.rnd.random() < 0.1:
            text, v, const = self.expr(depth - 1)
            if const:
                return text, v, const
            op = self.rnd.choice("-~")
            return f"{op}({text})", (-v if op == "-" else ~v) & self.mask, False
        op = self.rnd.choice(["+", "-", "*", "&", "|", "^", "<<", ">>", "/", "%"])
        a, va, ca = self.expr(depth - 1)
        b, vb, cb = self.expr(depth - 1)
        if ca and cb:                       # два числа подряд свернулись бы при компиляции
            a, va = ("eax", self.regs["eax"] & self.mask) if self.width == 32 else ("ax", self.regs["eax"] & 0xFFFF)
        if op in ("<<", ">>"):
            if cb:
                vb %= self.width
                b = str(vb)
            else:
                b, vb = f"({b} & {self.width - 1})", vb & (self.width - 1)
            if op == "<<":
                return f"({a} << {b})", (va << vb) & self.mask, False
            return f"({a} >> {b})", (self.s(va) >> vb) & self.mask, False
        if op in ("/", "%"):
            if cb:
                vb = vb or 1
                b = str(vb)
            else:
                b, vb = f"(({b} & 255) | 1)", (vb & 255) | 1
            x, y = self.s(va), self.s(vb)
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            r = x - q * y
            return f"({a} {op} {b})", (q if op == "/" else r) & self.mask, False
        f = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y,
             "&": lambda x, y: x & y, "|": lambda x, y: x | y, "^": lambda x, y: x ^ y}[op]
        return f"({a} {op} {b})", f(va, vb) & self.mask, False


@unittest.skipUnless(unicorn, "unicorn не установлен")
class RandomLetTest(unittest.TestCase):
    def random_case(self, rnd, bits):
        regs = {r: rnd.getrandbits(32) for r in REGS32}
        regs["esp"] = STACK
        mem = {"v0": rnd.getrandbits(32), "v1": rnd.getrandbits(32), "b0": rnd.getrandbits(8)}
        if bits == 16:
            for r in REGS32:
                if r != "esp":
                    regs[r] &= 0xFFFF
        width = rnd.choice([16, 32]) if bits == 32 else 16
        names = REGS32 if width == 32 else REGS16
        r = rnd.random()
        if r < 0.6:
            dest = names[rnd.choice([0, 1, 2, 3, 5, 6, 7])]
        elif r < 0.8:
            dest = rnd.choice(REGS8)
            width = bits                    # для байтового приёмника считается в ширину режима
        else:
            dest = ("dword" if width == 32 else "word") + " [res]"
        signed = rnd.random() < 0.3
        model = Model(rnd, width, signed, regs, mem)
        text, value, const = model.expr(rnd.randint(1, 4))
        while const:                        # число целиком проверяется отдельно (как mov)
            text, value, const = model.expr(rnd.randint(1, 4))
        return regs, mem, width, signed, text, value, dest

    def run_case(self, bits, regs, mem, width, signed, text, value, dest):
        kw = "let signed" if signed else "let"
        src = (f'{kw} {dest} - "{text}"\nhlt\n'
               f"v0 d: {mem['v0']}\nv1 d: {mem['v1']}\nb0 b: {mem['b0']}\nres d: 0\n")
        try:
            out, memory, syms = run_symbols(src, bits, regs)
        except GasemErrors as e:
            if "слишком сложное" in e.errors[0].message:
                self.too_complex += 1            # честный отказ компилятора, а не ошибка
                return
            raise
        expected = dict(regs)
        if dest in REGS8:
            i = REGS8.index(dest)
            full = REGS32[i & 3]
            shift = 8 if i >= 4 else 0
            expected[full] = (regs[full] & ~(0xFF << shift)) | ((value & 0xFF) << shift)
        elif "[res]" in dest:
            got = dword(memory, syms["res"]) & ((1 << width) - 1)
            self.assertEqual(got, value & ((1 << width) - 1), src)
        else:
            i = (REGS32 if width == 32 else REGS16).index(dest)
            full = REGS32[i]
            m = (1 << width) - 1
            expected[full] = (regs[full] & ~m & 0xFFFFFFFF) | (value & m)
        for r in REGS32:
            if bits == 16 and r == "esp":
                continue
            self.assertEqual(out[r], expected[r] & 0xFFFFFFFF,
                             f"регистр {r}: {out[r]:#x} != {expected[r]:#x}\n{src}")
        self.assertEqual(dword(memory, syms["v0"]), mem["v0"])

    def random_run(self, bits, seed, count):
        rnd = random.Random(seed)
        self.too_complex = 0
        for _ in range(count):
            case = self.random_case(rnd, bits)
            with self.subTest(expr=case[4], dest=case[6]):
                self.run_case(bits, *case)
        # от перегруженных выражений компилятор вправе отказаться, но редко
        self.assertLess(self.too_complex, count * 0.02)

    def test_random_32(self):
        self.random_run(32, 2026, 400)

    def test_random_16(self):
        self.random_run(16, 16, 250)

    def test_examples(self):
        out, _ = run('let ebx - "[row]*80 + [col]"\nhlt\nrow d: 3\ncol d: 7')
        self.assertEqual(out["ebx"], 247)
        out, _ = run('let eax - "eax*3 + 1"\nhlt', regs={"eax": 5})
        self.assertEqual(out["eax"], 16)
        out, _ = run('let eax - "ebx + eax"\nhlt', regs={"eax": 5, "ebx": 7})
        self.assertEqual(out["eax"], 12)
        out, _ = run('let signed eax - "ebx / -2"\nhlt', regs={"ebx": 9})
        self.assertEqual(out["eax"], 0xFFFFFFFC)


if __name__ == "__main__":
    unittest.main()
