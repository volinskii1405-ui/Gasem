"""Режим b 64: код выполняется в 64-битном эмуляторе x86, пример long.gsm
загружается в QEMU, проверяются сообщения об ошибках."""

import os
import random
import shutil
import sys
import tempfile
import time
import unittest

from gasem import GasemErrors, compile_file, compile_source

from test_highlevel import unicorn

if unicorn:
    from unicorn import x86_const as X

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGS64 = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi"] + [f"r{i}" for i in range(8, 16)]
ORG = 0x1000
STACK = 0x8000
M64 = (1 << 64) - 1


def run64(src, regs=None):
    """Собрать в режиме b 64 и выполнить до hlt. → (регистры, память, символы)."""
    res = compile_source(f"og {ORG}\nb 64\n{src}\n")
    mu = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_64)
    mu.mem_map(0, 0x10000)
    mu.mem_write(ORG, res.code)
    for name, value in (regs or {}).items():
        mu.reg_write(getattr(X, "UC_X86_REG_" + name.upper()), value)
    mu.reg_write(X.UC_X86_REG_RSP, STACK)

    def on_code(uc, address, size, _):
        if uc.mem_read(address, 1)[0] == 0xF4:
            uc.emu_stop()

    mu.hook_add(unicorn.UC_HOOK_CODE, on_code)
    mu.emu_start(ORG, ORG + len(res.code), count=200000)
    out = {r: mu.reg_read(getattr(X, "UC_X86_REG_" + r.upper())) for r in REGS64}
    return out, bytes(mu.mem_read(0, 0x10000)), res.symbols


def qword(mem, addr):
    return int.from_bytes(mem[addr:addr + 8], "little")


def errors(src):
    try:
        compile_source(src)
    except GasemErrors as e:
        return [x.message for x in e.errors]
    raise AssertionError("ожидалась ошибка:\n" + src)


@unittest.skipUnless(unicorn, "unicorn не установлен")
class Run64Test(unittest.TestCase):
    def test_registers_and_memory(self):
        out, mem, syms = run64("""
mov rax - 0x123456789ABCDEF0
mov r8 - rax
add r8 - 1
mov [val] - r8              ; адрес относительно rip
mov r9 - [val]
mov r10d - -1               ; запись в 32-битную часть обнуляет старшую
movsxd r11 - r10d
lea rsi - [msg]
hlt
val: q: 0
msg: s: "x"
""")
        self.assertEqual(out["r8"], 0x123456789ABCDEF1)
        self.assertEqual(qword(mem, syms["val"]), 0x123456789ABCDEF1)
        self.assertEqual(out["r9"], 0x123456789ABCDEF1)
        self.assertEqual(out["r10"], 0xFFFFFFFF)
        self.assertEqual(out["r11"], M64)
        self.assertEqual(out["rsi"], syms["msg"])

    def test_stack_and_calls(self):
        out, _, _ = run64("""
mov rbx - 0x1111222233334444
push rbx
push 0x7FFF
pop rcx
pop rdx
call twice - rdx
hlt

proc twice - rax uses rbx
    local tmp - qword
    mov [tmp] - rax
    add rax - [tmp]
    mov rbx - 0
end
""")
        self.assertEqual(out["rcx"], 0x7FFF)
        self.assertEqual(out["rdx"], 0x1111222233334444)
        self.assertEqual(out["rax"], 0x2222444466668888)
        self.assertEqual(out["rbx"], 0x1111222233334444)
        self.assertEqual(out["rsp"], STACK)

    def test_signed_division(self):
        out, _, _ = run64("mov rax - -100\ncqo\nmov rcx - 7\nidiv rcx\nhlt")
        self.assertEqual(out["rax"], (-14) & M64)
        self.assertEqual(out["rdx"], (-2) & M64)

    def test_high_level_blocks(self):
        out, _, _ = run64("""
xor rax - rax
for r12 - 1 - 11
    add rax - r12
end
mov rcx - 0
while rcx < 5
    inc rcx
end
if rax = 55 and rcx = 5
    mov r15 - 1
end
hlt
""")
        self.assertEqual((out["rax"], out["rcx"], out["r15"]), (55, 5, 1))


class Model64:
    """Случайное выражение для let и его значение (как посчитает процессор)."""

    def __init__(self, rnd, regs, mem, signed):
        self.rnd, self.regs, self.mem, self.signed = rnd, regs, mem, signed

    def s(self, v):
        v &= M64
        return v - (1 << 64) if self.signed and v >> 63 else v

    def leaf(self):
        r = self.rnd.random()
        if r < 0.4:
            name = self.rnd.choice(["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r13"])
            return name, self.regs[name]
        if r < 0.55:
            name = self.rnd.choice(["ebx", "ecx", "r10d"])
            full = {"ebx": "rbx", "ecx": "rcx", "r10d": "r10"}[name]
            v = self.regs[full] & 0xFFFFFFFF
            if self.signed and v >> 31:
                v -= 1 << 32
            return name, v & M64
        if r < 0.7:
            return "[q0]", self.mem["q0"]
        if r < 0.8:
            v = self.mem["d0"]
            if self.signed and v >> 31:
                v -= 1 << 32
            return "dword [d0]", v & M64
        return None

    def expr(self, depth):
        if depth == 0 or self.rnd.random() < 0.25:
            leaf = self.leaf()
            if leaf is None:
                v = self.rnd.randrange(0, 1000)
                return str(v), v, True
            return leaf[0], leaf[1], False
        op = self.rnd.choice(["+", "-", "*", "&", "|", "^", "<<", ">>", "/", "%"])
        a, va, ca = self.expr(depth - 1)
        b, vb, cb = self.expr(depth - 1)
        if ca and cb:
            a, va = "rax", self.regs["rax"]
        if op in ("<<", ">>"):
            if cb:
                vb %= 64
                b = str(vb)
            else:
                b, vb = f"({b} & 63)", vb & 63
            if op == "<<":
                return f"({a} << {b})", (va << vb) & M64, False
            return f"({a} >> {b})", (self.s(va) >> vb) & M64, False
        if op in ("/", "%"):
            if cb:
                vb = vb or 1
                b = str(vb)
            else:
                b, vb = f"(({b} & 255) | 1)", (vb & 255) | 1
            x, y = self.s(va), self.s(vb)
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            return f"({a} {op} {b})", (q if op == "/" else x - q * y) & M64, False
        f = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y,
             "&": lambda x, y: x & y, "|": lambda x, y: x | y, "^": lambda x, y: x ^ y}[op]
        return f"({a} {op} {b})", f(va, vb) & M64, False


@unittest.skipUnless(unicorn, "unicorn не установлен")
class RandomLet64Test(unittest.TestCase):
    def test_random(self):
        rnd = random.Random(64)
        too_complex = 0
        for _ in range(300):
            regs = {r: rnd.getrandbits(64) for r in REGS64 if r != "rsp"}
            mem = {"q0": rnd.getrandbits(64), "d0": rnd.getrandbits(32)}
            signed = rnd.random() < 0.3
            model = Model64(rnd, regs, mem, signed)
            text, value, const = model.expr(rnd.randint(1, 4))
            while const:
                text, value, const = model.expr(rnd.randint(1, 4))
            dest = rnd.choice(["rax", "rbx", "rsi", "r12", "qword [res]"])
            kw = "let signed" if signed else "let"
            src = f'{kw} {dest} - "{text}"\nhlt\nq0: q: {mem["q0"]}\nd0: d: {mem["d0"]}\nres: q: 0\n'
            with self.subTest(src=src):
                try:
                    out, memory, syms = run64(src, regs)
                except GasemErrors as e:
                    if "слишком сложное" in e.errors[0].message:
                        too_complex += 1
                        continue
                    raise
                expected = dict(regs)
                if dest == "qword [res]":
                    self.assertEqual(qword(memory, syms["res"]), value, src)
                else:
                    expected[dest] = value
                for r in expected:
                    self.assertEqual(out[r], expected[r], f"{r}: {out[r]:#x} != {expected[r]:#x}\n{src}")
        self.assertLess(too_complex, 6)


class Errors64Test(unittest.TestCase):
    def test_mode_errors(self):
        self.assertIn("регистр r8 есть только в режиме b 64", errors("b 32\nmov r8 - 1")[0])
        self.assertIn("регистр rax есть только в режиме b 64", errors("mov rax - 1")[0])
        self.assertIn("команда syscall есть только в режиме b 64", errors("syscall")[0])
        self.assertIn("команды pusha нет в режиме b 64", errors("b 64\npusha")[0])
        self.assertIn("в стек кладутся 64-битные", errors("b 64\npush eax")[0])
        self.assertIn("нет 16-битной адресации", errors("b 64\nmov ax - [bx]")[0])
        self.assertIn("ah, bh, ch и dh нельзя", errors("b 64\nmov ah - sil")[0])
        self.assertIn("не помещается в 32 бита со знаком", errors("b 64\nadd rax - 0x80000000")[0])
        self.assertIn("недоступен в режиме b 64", errors("b 64\njmp 0x08:0x1000")[0])
        self.assertIn("movsxd", errors("b 64\nmovzx rax - ecx")[0])
        self.assertIn("b 16, b 32 и b 64", errors("b 8")[0])

    def test_rip_relative_only_for_addresses(self):
        res = compile_source("og 0x1000\nb 64\nVGA = 0xB8000\nmov [VGA] - ax\nmov eax - [x]\nx: d: 0")
        self.assertEqual(res.code[:8], bytes.fromhex("6689042500800B00"))   # число — как есть
        self.assertEqual(res.code[8:14], bytes.fromhex("8B0500000000"))     # метка — относительно rip


QEMU64 = shutil.which("qemu-system-x86_64")


@unittest.skipUnless(QEMU64, "нужен qemu-system-x86_64")
class LongModeExampleTest(unittest.TestCase):
    def test_boots_into_long_mode(self):
        sys.path.insert(0, os.path.join(ROOT, "os"))
        import qemu_demo
        res = compile_file(os.path.join(ROOT, "examples", "long.gsm"))
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "long.img")
            with open(image, "wb") as f:
                f.write(res.code)
            vm = qemu_demo.Qemu(image, tmp, qemu=QEMU64)
            try:
                for _ in range(50):
                    first = vm.screen_text()[0].decode("cp437").rstrip(" \0")
                    if first.startswith("Gasem"):
                        break
                    time.sleep(0.1)
                self.assertEqual(first, "Gasem: 64-bit long mode")
                self.assertIn("0x123456789abcdef0", vm.cmd(f"xp /1gx {res.symbols['result']:#x}").decode())
                self.assertIn("CS64", vm.cmd("info registers").decode())
            finally:
                vm.quit()


if __name__ == "__main__":
    unittest.main()
