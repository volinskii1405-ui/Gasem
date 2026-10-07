"""Узлы синтаксического дерева Gasem: выражения, операнды и операторы."""


# ---------------------------------------------------------------- выражения

class Expr:
    col = None


class Num(Expr):
    def __init__(self, value, col=None):
        self.value = value
        self.col = col


class Str(Expr):
    def __init__(self, value, col=None):
        self.value = value  # bytes
        self.col = col


class Sym(Expr):
    """Имя метки или константы (значение — адрес/число)."""

    def __init__(self, name, col=None):
        self.name = name
        self.col = col


class Here(Expr):
    """$ — адрес текущей строки."""

    def __init__(self, col=None):
        self.col = col


class Start(Expr):
    """$$ — адрес начала программы (значение og)."""

    def __init__(self, col=None):
        self.col = col


class RegNode(Expr):
    """Регистр внутри выражения (допустим только в адресе [..]).

    local — имя локальной переменной proc, если регистр взялся из неё ([ebp-4])."""

    def __init__(self, reg, col=None, local=None):
        self.reg = reg
        self.col = col
        self.local = local


class Var(Expr):
    """[имя] внутри выражения do — значение переменной целиком."""

    def __init__(self, name, col=None):
        self.name = name
        self.col = col


class MemNode(Expr):
    """Чтение памяти [..] внутри выражения let (во время выполнения)."""

    def __init__(self, mem, col=None):
        self.mem = mem      # MemOperand
        self.col = col


class Unary(Expr):
    def __init__(self, op, x, col=None):
        self.op = op
        self.x = x
        self.col = col


class Binary(Expr):
    def __init__(self, op, a, b, col=None):
        self.op = op
        self.a = a
        self.b = b
        self.col = col


def has_reg(node):
    if isinstance(node, RegNode):
        return True
    if isinstance(node, Unary):
        return has_reg(node.x)
    if isinstance(node, Binary):
        return has_reg(node.a) or has_reg(node.b)
    return False


def is_const(node):
    """Можно ли вычислить выражение при компиляции (нет регистров и памяти)."""
    if isinstance(node, (RegNode, MemNode, Var)):
        return False
    if isinstance(node, Unary):
        return is_const(node.x)
    if isinstance(node, Binary):
        return is_const(node.a) and is_const(node.b)
    return True


def first_reg(node):
    if isinstance(node, RegNode):
        return node
    if isinstance(node, Unary):
        return first_reg(node.x)
    if isinstance(node, Binary):
        return first_reg(node.a) or first_reg(node.b)
    return None


def fold_const(node):
    """Вычислить выражение из одних чисел (без меток). None — если нельзя."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Unary):
        v = fold_const(node.x)
        if v is None:
            return None
        return {"-": -v, "+": v, "~": ~v}[node.op]
    if isinstance(node, Binary):
        a, b = fold_const(node.a), fold_const(node.b)
        if a is None or b is None:
            return None
        if node.op == "+":
            return a + b
        if node.op == "-":
            return a - b
        if node.op == "*":
            return a * b
        return None
    return None


# ---------------------------------------------------------------- операнды

class RegOperand:
    def __init__(self, reg):
        self.reg = reg


class MemOperand:
    def __init__(self, size, seg, base, index, scale, disp, jump=None):
        self.size = size     # 8/16/32/64 или None
        self.seg = seg       # сегментный регистр или None
        self.base = base
        self.index = index
        self.scale = scale
        self.disp = disp     # Expr или None
        self.jump = jump     # 'far' для jmp far [..]


class ImmOperand:
    def __init__(self, expr, size=None, jump=None, from_string=False):
        self.expr = expr
        self.size = size     # явный размер (byte/word/dword)
        self.jump = jump     # short / near / far
        self.from_string = from_string   # "строка" в команде — это её адрес


class FarOperand:
    """сегмент:смещение — для дальних jmp/call."""

    def __init__(self, seg, off, size=None):
        self.seg = seg
        self.off = off
        self.size = size


class A20Operand:
    """Абстракция порта: линия A20."""


# ---------------------------------------------------------------- операторы

class Stmt:
    loc = None


class LabelStmt(Stmt):
    def __init__(self, name, loc, col=None):
        self.name = name
        self.loc = loc
        self.col = col


class ConstStmt(Stmt):
    def __init__(self, name, expr, loc, col=None):
        self.name = name
        self.expr = expr
        self.loc = loc
        self.col = col


class OrgStmt(Stmt):
    def __init__(self, expr, loc):
        self.expr = expr
        self.loc = loc


class BitsStmt(Stmt):
    def __init__(self, expr, loc):
        self.expr = expr
        self.loc = loc


class AlignStmt(Stmt):
    def __init__(self, expr, fill, loc):
        self.expr = expr
        self.fill = fill
        self.loc = loc


class DataStmt(Stmt):
    """b: / b-N / b/ N (и то же для w, d, q)."""

    def __init__(self, unit, kind, count, items, loc, name="b"):
        self.unit = unit     # размер элемента в байтах: 1, 2, 4, 8
        self.kind = kind     # 'list' (b:), 'fill' (b-N), 'fixed' (b/ N)
        self.count = count   # Expr для fill/fixed
        self.items = items   # список Expr
        self.loc = loc
        self.name = name     # b / w / d / q — для сообщений


class TimesStmt(Stmt):
    """&& N <команда> — повторение."""

    def __init__(self, count, body, loc):
        self.count = count
        self.body = body
        self.loc = loc


class InstrStmt(Stmt):
    def __init__(self, prefixes, mnemonic, operands, loc, written=None):
        self.prefixes = prefixes
        self.mnemonic = mnemonic
        self.operands = operands
        self.loc = loc
        self.written = written or mnemonic  # как команда написана в исходнике
        self.flags = {}  # выбранные размеры кодировок (растут монотонно между проходами)


class DoStmt(Stmt):
    """do "выражение" - приёмник"""

    def __init__(self, expr, dest, loc):
        self.expr = expr
        self.dest = dest
        self.loc = loc
        self.flags = {}


class IncbinStmt(Stmt):
    def __init__(self, data, loc):
        self.data = data
        self.loc = loc


class CallStmt(Stmt):
    """call/jmp с аргументами: call puts - "Hello". Разворачивается после разбора,
    когда известны все объявления args."""

    def __init__(self, mnemonic, prefixes, target, args, bits, loc):
        self.mnemonic = mnemonic
        self.prefixes = prefixes
        self.target = target
        self.args = args
        self.bits = bits
        self.loc = loc


class AtStmt(Stmt):
    """at АДРЕС — код ниже работает по другому адресу, чем лежит в файле."""

    def __init__(self, expr, loc):
        self.expr = expr
        self.loc = loc


class AtEndStmt(Stmt):
    """end блока at."""

    def __init__(self, loc):
        self.loc = loc


class PoolStmt(Stmt):
    """pool — место для строк из команд, встреченных выше."""

    def __init__(self, loc):
        self.loc = loc
        self.entries = []    # (метка, bytes)
