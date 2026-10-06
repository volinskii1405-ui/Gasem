"""Вычисление выражений Gasem.

Значение выражения — целое число или строка (bytes). В обычных выражениях
строка превращается в число (байты в порядке little-endian, как 'A' = 0x41).
В выражениях do оператор + со строкой означает конкатенацию:
"Hello" + 1 = "Hello1".
"""

from . import nodes as N
from .errors import GasemError


def to_int(value, col=None):
    if isinstance(value, bytes):
        if len(value) > 8:
            raise GasemError("строка слишком длинная, чтобы использовать её как число", None, col)
        return int.from_bytes(value, "little")
    return value


def to_text(value):
    if isinstance(value, bytes):
        return value
    return str(value).encode("ascii")


class Evaluator:
    def __init__(self, asm, here, mode=None):
        self.asm = asm
        self.here = here
        self.mode = mode       # None или 'do'
        self.known = True      # все ли символы уже определены

    def error(self, msg, node):
        raise GasemError(msg, None, node.col if node is not None else None)

    def eval(self, node):
        if isinstance(node, N.Num):
            return node.value
        if isinstance(node, N.Str):
            return node.value
        if isinstance(node, N.Sym):
            value = self.asm.lookup(node.name)
            if value is None:
                if self.asm.final:
                    self.error(f"неизвестное имя '{node.name}'", node)
                self.known = False
                return 0
            return value
        if isinstance(node, N.Here):
            return self.here
        if isinstance(node, N.Start):
            return self.asm.org
        if isinstance(node, N.RegNode):
            if self.mode == "do":
                self.error(f"регистр {node.reg.name} нельзя использовать в do: "
                           f"выражение do вычисляется при компиляции", node)
            self.error(f"регистр {node.reg.name} нельзя использовать в выражении", node)
        if isinstance(node, N.Var):
            if self.mode != "do":
                self.error("[имя] как значение переменной допускается только в do", node)
            return self.asm.var_value(node.name, self, node)
        if isinstance(node, N.Unary):
            v = self.eval(node.x)
            if isinstance(v, bytes):
                if self.mode == "do":
                    self.error(f"операция '{node.op}' не применима к строке", node)
                v = to_int(v, node.col)
            if node.op == "-":
                return -v
            if node.op == "~":
                return ~v
            return v
        if isinstance(node, N.Binary):
            return self.binary(node)
        raise AssertionError(f"неизвестный узел {node!r}")

    def binary(self, node):
        a = self.eval(node.a)
        b = self.eval(node.b)
        op = node.op
        if isinstance(a, bytes) or isinstance(b, bytes):
            if self.mode == "do":
                if op == "+":
                    return to_text(a) + to_text(b)
                self.error(f"операция '{op}' не применима к строкам (для строк есть только +)", node)
            a, b = to_int(a, node.col), to_int(b, node.col)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%"):
            if b == 0:
                if self.asm.final:
                    self.error("деление на ноль", node)
                return 0
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q if op == "/" else a - q * b
        if op in ("<<", ">>"):
            if b < 0 or b > 4096:
                if self.asm.final:
                    self.error(f"недопустимая величина сдвига {b}", node)
                return 0
            return a << b if op == "<<" else a >> b
        if op == "&":
            return a & b
        if op == "|":
            return a | b
        if op == "^":
            return a ^ b
        raise AssertionError(op)
