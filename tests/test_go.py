"""Go-версия компилятора выдаёт то же, что Python-версия.

Сравниваются машинный код, листинг, символы, карта строк для отладчика,
предупреждения и тексты ошибок (с указателем на столбец), а также gasem fmt —
на всех программах из репозитория, на тысячах их случайных фрагментов
с «опечатками» и на заготовленных пограничных случаях.

Нужен go (или собранные программы в GASEM_GO_BIN). Весь остальной набор
тестов проверяет Go-версию так: GASEM_IMPL=go python3 -m pytest tests
"""

import glob
import os
import random
import unittest

import gasem.fmt
from gasem.errors import GasemErrors

import impl

ROOT = os.path.join(os.path.dirname(__file__), "..")
SOURCES = sorted(glob.glob(os.path.join(ROOT, "examples", "*.gsm")) + glob.glob(os.path.join(ROOT, "os", "*.gsm")))
PY_FORMAT = gasem.fmt.format_text if impl.IMPL != "go" else None

CHARS = list(" -;:[]()+*/$&|^~<>=,'\"\\\t.0123456789abxyzALDHefghsw_ЖёЯ²٣")
WORDS = ["mov", "if", "end", "else", "elif", "while", "for", "let", "do", "b:", "w:", "d:", "s:", "&&", "og",
         "b 32", "ax", "eax", "al", "[bx]", "[esi+4]", " - ", "pool", "args", "call", "jmp", "push", "pop",
         "repeat", "until", "break", "continue", "signed", "not", "and", "or", "zero", "carry", "byte", "word",
         "short", "far", "$", "$$", "510-($-$$)", '"str"', "'c'", "nxtb", "chk", "jfnz", "a20", "es:[di]",
         "1<<4", "-1", "label:", ".loc:", "x = 5", "ret if carry", "break if al = 1", "int 0x10",
         "macro m - a", "proc f - esi", "uses ebx", "local v - dword", "return", "return eax",
         "struct P", "x: w", "P 1 - 2", "at 0x500", "m eax", "[v]", "P.x",
         "b 64", "rax", "r8", "r12d", "sil", "[abs 0x10]", "movsxd", "cqo", "push rax", "syscall", "qword"]

EDGE_CASES = [
    "q: 0xFFFFFFFFFFFFFFFF - -1 - 0x10000000000000000", "d: 0xFFFFFFFFFFFFFFFF", "X = 1 << 4096\nb: X >> 4090",
    "X = 1 << 4097", "X = (1<<200) / 3\nd: X & 0xFFFFFFFF", "X = -7 / 2\nY = -7 % 2\nb: X - Y", "X = 7 / 0",
    "og 1 << 70\nl: jmp l", "og -1", "mov eax - 0x1FFFFFFFF", "mov ax - -32769", "push -129", "add ax - 0xFF80",
    "b: 0x0x10", "b: 1a", "b: ٣", "b: ²", "мой: jmp мой", 'do "\\xff\\xfe" - ax', 'let ax - "\\xe2\\x82"',
    'b: "\\x4"', 'b: "\\q"', "b: 'ab", "a:\x0cb:\x1cc:\u2028d:", "\u00a0mov ax - 1", "İ: b: 0", "MOV AX - 1",
    "&& -1 b: 1", "b/ 2: 1 - 2 - 3", "align 3", "align 4 - 256", "b 15", 'do "[x] + 1" - ax\nx: d: 5',
    'do "[x]" - ax\nx: w: 1 - 2', "for al - 0 - 10 - 0\nend", 'b 32\ndo "" + "' + "c" * 300 + '" - eax',
    'let eax - "ebx + ecx * edx - esi / edi % ebp"', 'let al - "(esi + edi) * 3"', "jmp short $+200",
    "mov [bx+bx] - ax", "mov [eax*3] - ax", "mov [esp*2] - eax", "mov al - [-1]",
    'call puts - "a" - "b"\nputs: ret', "args f - eax - ebx\ncall f - ebx - eax\nf: ret",
    "if al = 1\nelse\nelse\nend", "repeat\nuntil not carry and al < 3 or [x]\nx: b: 0",
    "macro put - c - n\n    mov al - c\n    do \"n * 2\" - bx\nx:\n    jmp x\nend\nput 'A' - 3\nput 'B' - 4",
    "macro m\n    m\n    m\nend\nm", "macro m - b\nend", "macro mov\nend", "m 1\nmacro m - a\nend\nm",
    "b 32\nproc f - esi uses ebx - esi\n    local n - dword\n    local buf - byte 16\n    mov [n] - 1\n"
    "    lea esi - [buf]\n    return [n] if zero\n    return eax\nend\ncall f - \"x\"",
    "proc f\n    nop\n    local x - word\nend", "return 1", "b 32\nproc f uses eax\n    return 2\nend",
    "struct P\n    x: w\n    y: d 2\nend\nstruct R\n    a: P\n    tag: b 3\nend\nr: R\np: P 1 - 2\nmov ax - [bx + R.a.y]",
    "struct P\n    x w\nend", "struct P\n    size: b\nend\nP 1 - 2 - 3",
    "og 0x7C00\nat 0x50000\nb 32\nx: jmp x\nmov eax - $$\nend\ny: nop", "at nothing\nnop\nend",
    "og 0x7C00\nnop\nat 0x500\nog 0x600\nend",
    "b 64\nmov rax - 0x123456789\nmov r8d - -1\npush r12\nmov [x] - rax\nmov eax - [abs 0x1234]\n"
    "lea rsi - [x]\nmovsxd rcx - eax\nx: q: 0", "b 64\nlet rax - \"rbx * r9 + [q] / 3\"\nq: q: 5",
    "b 64\nproc f - rdi uses rbx\n    local t - qword\n    mov [t] - rdi\n    return [t]\nend",
    "b 64\npush eax", "b 64\nmov ah - sil", "mov r8 - 1", "b 64\nadd rax - 0x80000000", "b 64\njcxz $",
]


def outcome(fn, *args):
    try:
        r = fn(*args)
    except GasemErrors as e:
        return "ошибки", [x.format() for x in e.errors]
    return ("ok", r.code, r.origin, list(r.symbols.items()), r.listing,
            [(a, n, bits, loc.file, loc.line, loc.text) for a, n, bits, loc in r.lines],
            [w.format() for w in r.warnings])


def mutate(text, rnd):
    lines = text.split("\n")
    for _ in range(rnd.randint(0, 3)):
        i = rnd.randrange(len(lines))
        s, kind = lines[i], rnd.randrange(6)
        j = rnd.randrange(len(s) + 1)
        if kind == 0:
            s = s[:j] + s[j + 1:]
        elif kind == 1:
            s = s[:j] + rnd.choice(CHARS) + s[j:]
        elif kind == 2:
            s = s[:j] + rnd.choice(WORDS) + s[j:]
        elif kind == 3:
            s = s.replace(" - ", rnd.choice([" -", ",", " + ", " - - "]), 1)
        elif kind == 4:
            s = rnd.choice(WORDS) + " " + rnd.choice(WORDS) + " - " + rnd.choice(WORDS)
        else:
            s = ""
        lines[i] = s
    return "\n".join(lines)


@unittest.skipIf(impl.go_compiler() is None, "нужен go (или GASEM_GO_BIN)")
class GoMatchesPythonTest(unittest.TestCase):
    def setUp(self):
        self.go = impl.go_compiler()

    def check(self, text, what):
        py = outcome(impl.PY_COMPILE_SOURCE, text)
        go = outcome(self.go.compile_source, text)
        self.assertEqual(py, go, f"{what}:\n{text}")

    def test_repository_sources(self):
        for path in SOURCES:
            with self.subTest(path=os.path.basename(path)):
                self.assertEqual(outcome(impl.PY_COMPILE_FILE, path), outcome(self.go.compile_file, path))

    def test_fragments_with_typos(self):
        rnd = random.Random(2026)
        lines = []
        for path in SOURCES:
            with open(path, encoding="utf-8") as f:
                lines += f.read().split("\n")
        for k in range(1500):
            start = rnd.randrange(len(lines))
            text = "\n".join(lines[start:start + rnd.randint(1, 40)])
            text = mutate(rnd.choice(["", "og 0x7C00\n", "b 32\n"]) + text, rnd)
            self.check(text, f"фрагмент {k}")

    def test_edge_cases(self):
        for text in EDGE_CASES:
            for prefix in ("", "og 0x7C00\n"):
                self.check(prefix + text, "пограничный случай")

    @unittest.skipIf(PY_FORMAT is None, "format_text подменён (GASEM_IMPL=go)")
    def test_formatter(self):
        rnd = random.Random(7)
        texts = []
        for path in SOURCES:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        texts += [mutate(t, rnd).replace("    ", rnd.choice(["\t", "  ", " "])) for t in texts for _ in range(5)]
        texts += EDGE_CASES
        for text in texts:
            self.assertEqual(PY_FORMAT(text), self.go.format_text(text))


if __name__ == "__main__":
    unittest.main()
