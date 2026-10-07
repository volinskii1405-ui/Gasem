"""Высокоуровневые конструкции Gasem.

    if / elif / else / end          ветвления
    while / end                     цикл с условием
    for / end                       цикл со счётчиком
    repeat / until                  цикл с проверкой в конце
    break, continue                 выход из цикла / следующий шаг
    let                             выражение, вычисляемое во время работы

Всё разворачивается при разборе в обычные команды x86 и служебные метки
(их имена начинаются с '@', поэтому не пересекаются с метками программы).
Никакого скрытого кода времени выполнения нет — результат виден в листинге.
"""

from . import nodes as N
from . import x86
from .errors import GasemError
from .lexer import Token, tokenize, OP, ID, SEP, STR

CONTROL_WORDS = {"if", "elif", "else", "end", "while", "for", "repeat", "until",
                 "break", "continue", "let"}

FLAGS = {"zero": "z", "carry": "c", "sign": "s", "overflow": "o", "parity": "p"}
RELATIONS = {"=": "eq", "==": "eq", "!=": "ne", "<": "lt", ">": "gt", "<=": "le", ">=": "ge"}
MIRROR = {"eq": "eq", "ne": "ne", "lt": "gt", "gt": "lt", "le": "ge", "ge": "le"}
UNSIGNED_CC = {"eq": "e", "ne": "ne", "lt": "b", "gt": "a", "le": "be", "ge": "ae"}
SIGNED_CC = {"eq": "e", "ne": "ne", "lt": "l", "gt": "g", "le": "le", "ge": "ge"}
INVERT = {"e": "ne", "ne": "e", "z": "nz", "nz": "z", "b": "ae", "ae": "b", "a": "be",
          "be": "a", "l": "ge", "ge": "l", "g": "le", "le": "g", "c": "nc", "nc": "c",
          "s": "ns", "ns": "s", "o": "no", "no": "o", "p": "np", "np": "p"}
LOOPS = ("while", "for", "repeat")


def _error(msg, tok=None):
    raise GasemError(msg, None, tok.col if tok is not None else None)


class Block:
    def __init__(self, kind, loc):
        self.kind = kind
        self.loc = loc
        self.top = self.cont = self.end = self.next = None
        self.has_else = False
        self.var = None
        self.step = 1


class HighLevel:
    def __init__(self, parser):
        self.p = parser
        self.blocks = []
        self.counter = 0

    # ------------------------------------------------ вывод операторов

    def new_label(self, what):
        self.counter += 1
        return f"@{what}{self.counter}"

    def label(self, name, loc):
        self.p.statements.append(N.LabelStmt(name, loc))

    def instr(self, mn, ops, loc):
        self.p.emit(N.InstrStmt([], mn, ops, loc))

    def jump(self, mn, target, loc):
        self.instr(mn, [N.ImmOperand(N.Sym(target))], loc)

    # ------------------------------------------------ операторы

    def statement(self, word, word_tok, toks, loc):
        if word == "let":
            return self.let(word_tok, toks, loc)

        if word in ("else", "end", "repeat", "break", "continue") and toks:
            _error(f"после {word} ничего не пишется", toks[0])

        if word == "if":
            blk = self.push("if", loc)
            blk.end = self.new_label("endif")
            blk.next = self.new_label("else")
            signed, cond = self.condition(word_tok, toks)
            self.jump_if(cond, False, blk.next, signed, loc)

        elif word == "elif":
            blk = self.top("if", "elif")
            if blk.has_else:
                _error("elif после else", word_tok)
            self.jump("jmp", blk.end, loc)
            self.label(blk.next, loc)
            blk.next = self.new_label("else")
            signed, cond = self.condition(word_tok, toks)
            self.jump_if(cond, False, blk.next, signed, loc)

        elif word == "else":
            blk = self.top("if", "else")
            if blk.has_else:
                _error("второй else в одном if", word_tok)
            self.jump("jmp", blk.end, loc)
            self.label(blk.next, loc)
            blk.next = None
            blk.has_else = True

        elif word == "while":
            blk = self.push("while", loc)
            blk.top = blk.cont = self.new_label("while")
            blk.end = self.new_label("wend")
            self.label(blk.top, loc)
            signed, cond = self.condition(word_tok, toks)
            self.jump_if(cond, False, blk.end, signed, loc)

        elif word == "for":
            self.for_loop(word_tok, toks, loc)

        elif word == "repeat":
            blk = self.push("repeat", loc)
            blk.top = self.new_label("repeat")
            blk.cont = self.new_label("until")
            blk.end = self.new_label("rend")
            self.label(blk.top, loc)

        elif word == "until":
            if not self.blocks or self.blocks[-1].kind != "repeat":
                _error("until без repeat", word_tok)
            blk = self.blocks.pop()
            self.label(blk.cont, loc)
            signed, cond = self.condition(word_tok, toks)
            self.jump_if(cond, False, blk.top, signed, loc)
            self.label(blk.end, loc)

        elif word in ("break", "continue"):
            loop = next((b for b in reversed(self.blocks) if b.kind in LOOPS), None)
            if loop is None:
                _error(f"{word} вне цикла", word_tok)
            self.jump("jmp", loop.end if word == "break" else loop.cont, loc)

        elif word == "end":
            if not self.blocks:
                _error("end без if, while или for", word_tok)
            blk = self.blocks[-1]
            if blk.kind == "repeat":
                _error("repeat закрывается словом until, а не end", word_tok)
            self.blocks.pop()
            if blk.kind == "if":
                if blk.next is not None:
                    self.label(blk.next, loc)
                self.label(blk.end, loc)
            elif blk.kind == "while":
                self.jump("jmp", blk.top, loc)
                self.label(blk.end, loc)
            elif blk.kind == "for":
                self.label(blk.cont, loc)
                if blk.step == 1:
                    self.instr("inc", [blk.var], loc)
                elif blk.step == -1:
                    self.instr("dec", [blk.var], loc)
                elif blk.step > 0:
                    self.instr("add", [blk.var, N.ImmOperand(N.Num(blk.step))], loc)
                else:
                    self.instr("sub", [blk.var, N.ImmOperand(N.Num(-blk.step))], loc)
                self.jump("jmp", blk.top, loc)
                self.label(blk.end, loc)

    def cond_jump(self, target, word_tok, toks, loc, value=True):
        """Перейти на target, если условие из toks равно value."""
        signed, cond = self.condition(word_tok, toks)
        self.jump_if(cond, value, target, signed, loc)

    def loop_jump(self, word, word_tok, if_tok, toks, loc):
        """break if ... / continue if ... — один условный переход."""
        loop = next((b for b in reversed(self.blocks) if b.kind in LOOPS), None)
        if loop is None:
            _error(f"{word} вне цикла", word_tok)
        self.cond_jump(loop.end if word == "break" else loop.cont, if_tok, toks, loc)

    def push(self, kind, loc):
        blk = Block(kind, loc)
        self.blocks.append(blk)
        return blk

    def top(self, kind, word):
        if not self.blocks or self.blocks[-1].kind != kind:
            raise GasemError(f"{word} без {kind}")
        return self.blocks[-1]

    def close_file(self, depth, errors):
        """Конец файла: все блоки, открытые в нём, должны быть закрыты."""
        while len(self.blocks) > depth:
            blk = self.blocks.pop()
            closer = "until" if blk.kind == "repeat" else "end"
            errors.append(GasemError(f"{blk.kind} без {closer}", blk.loc))

    def for_loop(self, word_tok, toks, loc):
        signed = False
        if toks and toks[0].is_id("signed"):
            signed = True
            toks = toks[1:]
        groups = [[]]
        for t in toks:
            if t.kind == SEP:
                groups.append([])
            else:
                groups[-1].append(t)
        blk = self.push("for", loc)
        if len(groups) not in (3, 4) or not all(groups):
            _error("формат: for счётчик - начало - конец [- шаг]", word_tok)
        var = self.p.parse_operand(groups[0], loc)
        if not (isinstance(var, N.MemOperand)
                or (isinstance(var, N.RegOperand) and var.reg.kind == "gpr")):
            _error("счётчик for — регистр или переменная в памяти", groups[0][0])
        start = self.p.parse_operand(groups[1], loc)
        end = self.p.parse_operand(groups[2], loc)
        step = 1
        if len(groups) == 4:
            step = N.fold_const(self.p.parse_expr_tokens(groups[3], loc))
            if not step:
                _error("шаг for — ненулевое число (например, 2 или -1)", groups[3][0])
        blk.var, blk.step = var, step
        blk.top = self.new_label("for")
        blk.cont = self.new_label("next")
        blk.end = self.new_label("fend")
        self.instr("mov", [var, start], loc)
        self.label(blk.top, loc)
        cc = self.compare(var, end, "ge" if step > 0 else "le", signed, loc)
        self.jump("j" + cc, blk.end, loc)

    # ------------------------------------------------ условия

    def condition(self, word_tok, toks):
        """Разобрать условие → (signed, дерево)."""
        toks = [Token(OP, "-", t.col, t.space, "-") if t.kind == SEP else t for t in toks]
        signed = False
        if toks and toks[0].is_id("signed"):
            signed = True
            toks = toks[1:]
        if not toks:
            _error(f"после {word_tok.value} ожидается условие, например: {word_tok.value} al = 10", word_tok)
        ors = []
        for part in self.split_word(toks, "or", word_tok):
            ands = [self.simple(a) for a in self.split_word(part, "and", word_tok)]
            ors.append(ands[0] if len(ands) == 1 else ("and", ands))
        return signed, (ors[0] if len(ors) == 1 else ("or", ors))

    @staticmethod
    def split_word(toks, word, near):
        parts, depth = [[]], 0
        for t in toks:
            if t.is_op("(", "["):
                depth += 1
            elif t.is_op(")", "]"):
                depth -= 1
            if depth == 0 and t.is_id(word):
                parts.append([])
                continue
            parts[-1].append(t)
        for p in parts:
            if not p:
                _error(f"пустое условие рядом с {word}", near)
        return parts

    def simple(self, toks):
        neg = False
        if toks[0].is_id("not"):
            neg = True
            toks = toks[1:]
            if not toks:
                _error("после not ожидается условие")
        if len(toks) == 1 and toks[0].kind == ID and toks[0].value.lower() in FLAGS:
            return ("flag", FLAGS[toks[0].value.lower()], neg)
        depth, idx = 0, None
        for i, t in enumerate(toks):
            if t.is_op("(", "["):
                depth += 1
            elif t.is_op(")", "]"):
                depth -= 1
            elif depth == 0 and t.kind == OP and t.value in RELATIONS:
                idx = i
                break
        loc = self.p.current_loc
        if idx is None:
            return ("truth", self.p.parse_operand(toks, loc), neg)
        if neg:
            _error("not перед сравнением не нужен — используйте != или обратное сравнение", toks[0])
        if idx == 0 or idx == len(toks) - 1:
            _error("сравнению нужны две стороны: например al = 10", toks[idx])
        left = self.p.parse_operand(toks[:idx], loc)
        right = self.p.parse_operand(toks[idx + 1:], loc)
        return ("cmp", left, right, RELATIONS[toks[idx].value])

    def compare(self, left, right, rel, signed, loc):
        """Сравнить и вернуть код условия, при котором сравнение истинно."""
        if isinstance(left, N.ImmOperand) and not isinstance(right, N.ImmOperand):
            left, right, rel = right, left, MIRROR[rel]
        if isinstance(left, N.ImmOperand):
            raise GasemError("сравнивать два числа бессмысленно — слева должен быть регистр или память")
        if isinstance(right, N.ImmOperand) and right.from_string and (
                (isinstance(left, N.RegOperand) and left.reg.size == 8)
                or (isinstance(left, N.MemOperand) and left.size == 8)):
            raise GasemError("строка в двойных кавычках — это адрес строки; "
                             "символ записывается в одинарных кавычках: 'a'")
        if not isinstance(left, (N.RegOperand, N.MemOperand)) or isinstance(right, (N.FarOperand, N.A20Operand)):
            raise GasemError("в условии можно сравнивать регистры, память и числа")
        if (isinstance(left, N.RegOperand) and left.reg.kind == "gpr"
                and isinstance(right, N.ImmOperand) and N.fold_const(right.expr) == 0):
            self.instr("test", [left, left], loc)       # короче, чем cmp r - 0
        else:
            self.instr("cmp", [left, right], loc)
        return (SIGNED_CC if signed else UNSIGNED_CC)[rel]

    def cond_code(self, c, signed, loc):
        kind = c[0]
        if kind == "flag":
            return INVERT[c[1]] if c[2] else c[1]
        if kind == "truth":
            op = c[1]
            if isinstance(op, N.RegOperand) and op.reg.kind == "gpr":
                self.instr("test", [op, op], loc)
            elif isinstance(op, N.MemOperand):
                self.instr("cmp", [op, N.ImmOperand(N.Num(0))], loc)
            else:
                raise GasemError("условием может быть сравнение, флаг (zero, carry, sign, overflow, "
                                 "parity), регистр или память")
            return "z" if c[2] else "nz"
        return self.compare(c[1], c[2], c[3], signed, loc)

    def jump_if(self, c, value, target, signed, loc):
        """Перейти на target, если условие c равно value; иначе идти дальше."""
        kind = c[0]
        if kind in ("and", "or"):
            items = c[1]
            short = (kind == "or") == value          # одно подусловие решает всё
            if short:
                for sub in items:
                    self.jump_if(sub, value, target, signed, loc)
            else:
                skip = self.new_label("skip")
                for sub in items[:-1]:
                    self.jump_if(sub, not value, skip, signed, loc)
                self.jump_if(items[-1], value, target, signed, loc)
                self.label(skip, loc)
            return
        if kind == "truth" and isinstance(c[1], N.ImmOperand):
            v = N.fold_const(c[1].expr)
            if v is None:
                raise GasemError("условие-число должно быть числом (например: while 1)")
            if ((v != 0) != c[2]) == value:
                self.jump("jmp", target, loc)
            return
        cc = self.cond_code(c, signed, loc)
        if not value:
            cc = INVERT[cc]
        self.jump("j" + cc, target, loc)

    # ------------------------------------------------ let

    def let(self, word_tok, toks, loc):
        signed = False
        if toks and toks[0].is_id("signed"):
            signed = True
            toks = toks[1:]
        groups = [[]]
        for t in toks:
            if t.kind == SEP:
                groups.append([])
            else:
                groups[-1].append(t)
        fmt = 'формат: let приёмник - "выражение"'
        if len(groups) != 2 or not all(groups):
            _error(fmt, word_tok)
        a, b = groups
        if len(b) == 1 and b[0].kind == STR:
            dest_toks, text = a, b[0]
        elif len(a) == 1 and a[0].kind == STR:
            dest_toks, text = b, a[0]
        else:
            _error(fmt + " (выражение — в двойных кавычках)", word_tok)
        if text.quote != '"':
            _error("выражение let пишется в двойных кавычках", text)
        dest = self.p.parse_operand(dest_toks, loc)
        inner = text.value.decode("utf-8", errors="replace")
        itoks = tokenize(inner, loc, col_base=text.col + 1, seps=False)
        if not itoks:
            _error("пустое выражение let", text)
        expr = self.p.parse_expr_tokens(itoks, loc, mode="let")
        for mn, ops in LetCompiler(signed, self.p.bits).compile(expr, dest, text):
            self.instr(mn, ops, loc)


# ---------------------------------------------------------------- let

_NAMES = {
    8: ["al", "cl", "dl", "bl"],
    16: ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"],
    32: ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"],
}
_TEMP_ORDER = [3, 6, 7, 1, 2, 5, 0]       # ebx esi edi ecx edx ebp eax
_LOW8_ORDER = [3, 1, 2, 0]                # регистры, у которых есть младший байт
EAX, ECX, EDX, ESP = 0, 1, 2, 4


def family(r):
    """Семейство регистра: al/ah/ax/eax → 0, cl/ch/cx/ecx → 1 и т. д."""
    return r.num & 3 if r.size == 8 else r.num


_family = family


def _walk(node):
    yield node
    if isinstance(node, N.Unary):
        yield from _walk(node.x)
    elif isinstance(node, N.Binary):
        yield from _walk(node.a)
        yield from _walk(node.b)


def _mem_regs(mem):
    return [r for r in (mem.base, mem.index) if r is not None]


def _refs(node):
    """Семейства регистров (0=eax ... 7=edi), которые читает выражение."""
    out = set()
    for n in _walk(node):
        if isinstance(n, N.RegNode):
            out.add(_family(n.reg))
        elif isinstance(n, N.MemNode):
            out.update(_family(r) for r in _mem_regs(n.mem))
    return out


def _pow2(node):
    v = N.fold_const(node)
    if v is not None and v > 0 and v & (v - 1) == 0:
        return v.bit_length() - 1
    return None


class LetCompiler:
    """Выражение let → команды. Меняется только приёмник (и флаги):
    временные регистры сохраняются в стеке."""

    def __init__(self, signed, bits):
        self.signed = signed
        self.bits = bits
        self.out = []
        self.busy = set()

    def emit(self, mn, *ops):
        self.out.append((mn, list(ops)))

    def reg(self, fam, size=None):
        return N.RegOperand(x86.REGISTERS[_NAMES[size or self.width][fam]])

    def compile(self, expr, dest, tok):
        self.tok = tok
        # приёмник и ширина вычислений
        if isinstance(dest, N.RegOperand):
            d = dest.reg
            if d.kind != "gpr":
                _error("приёмник let — регистр общего назначения или память", tok)
            if d.size != 8 and d.num == ESP:
                _error("sp/esp нельзя использовать в let", tok)
            self.width = d.size if d.size in (16, 32) else self.bits
            dest_size = d.size
        elif isinstance(dest, N.MemOperand):
            if dest.size not in (8, 16, 32):
                _error("укажите размер приёмника: let dword [x] - \"...\"", tok)
            self.width = dest.size if dest.size in (16, 32) else self.bits
            dest_size = dest.size
        else:
            _error("приёмник let — регистр общего назначения или память", tok)

        # какие регистры читает выражение
        refs, count, self.var_shift, self.divides = set(), {}, False, False
        for node in _walk(expr):
            regs = []
            if isinstance(node, N.RegNode):
                if node.reg.kind != "gpr":
                    _error(f"в let можно использовать только регистры общего назначения ({node.reg.name})", tok)
                regs = [node.reg]
            elif isinstance(node, N.MemNode):
                regs = _mem_regs(node.mem)
            elif isinstance(node, N.Str) and len(node.value) > 4:
                _error("в let нет строк (только числа и символы вроде 'A')", tok)
            elif isinstance(node, N.Var):
                _error("[имя] в let — это чтение памяти; запишите его без do-синтаксиса", tok)
            for r in regs:
                if r.size != 8 and r.num == ESP:
                    _error("sp/esp нельзя использовать в let: временные значения хранятся в стеке", tok)
                refs.add(_family(r))
                count[_family(r)] = count.get(_family(r), 0) + 1
            if isinstance(node, N.Binary):
                if node.op in ("<<", ">>") and not N.is_const(node.b):
                    self.var_shift = True
        dest_regs = _mem_regs(dest) if isinstance(dest, N.MemOperand) else []
        for r in dest_regs:
            if r.num == ESP:
                _error("sp/esp нельзя использовать в let", tok)

        # простые случаи: число или одно значение
        if N.is_const(expr):
            self.emit("mov", dest, N.ImmOperand(expr))
            return self.out

        dest_fam = _family(dest.reg) if isinstance(dest, N.RegOperand) else None
        dest_full = dest_fam is not None and dest.reg.size == self.width
        # рабочий регистр держит значение всё время вычисления, поэтому он не должен
        # встречаться в выражении и в адресе приёмника
        avoid = refs | {_family(r) for r in dest_regs}
        if dest_fam is not None:
            avoid.add(dest_fam)

        leftmost = expr
        while isinstance(leftmost, (N.Unary, N.Binary)):
            leftmost = leftmost.x if isinstance(leftmost, N.Unary) else leftmost.a
        dest_safe = dest_fam not in refs or (
            count.get(dest_fam) == 1 and isinstance(leftmost, N.RegNode)
            and _family(leftmost.reg) == dest_fam and leftmost.reg.size == self.width)

        if dest_full and dest_safe and not (self.var_shift and dest_fam == ECX):
            work, saved = dest_fam, False
        else:
            work = self.alloc(avoid)
            saved = True
            self.emit("push", self.reg(work))
        self.busy.add(work)
        self.gen(expr, work)
        if isinstance(dest, N.MemOperand):
            dest = N.MemOperand(dest.size, dest.seg, dest.base, dest.index, dest.scale, dest.disp)
        if dest_size == 8 and work not in _LOW8_ORDER:
            # у esi/edi/ebp нет младшего байта — переносим через другой регистр
            # (выражение уже вычислено, так что любой регистр, кроме приёмника, свободен)
            spare = next(f for f in _LOW8_ORDER
                         if f != dest_fam and f not in {_family(r) for r in dest_regs})
            self.emit("push", self.reg(spare))
            self.emit("mov", self.reg(spare), self.reg(work))
            self.emit("mov", dest, self.reg(spare, 8))
            self.emit("pop", self.reg(spare))
        elif not (dest_full and work == dest_fam):
            self.emit("mov", dest, self.reg(work, dest_size))
        if saved:
            self.emit("pop", self.reg(work))
        return self.out

    def alloc(self, avoid):
        for fam in _TEMP_ORDER:
            if fam in self.busy or fam in avoid:
                continue
            if self.var_shift and fam == ECX:
                continue
            return fam
        _error("выражение let слишком сложное — не хватает свободных регистров; "
               "разбейте его на несколько let", self.tok)

    def temp(self, node, avoid=()):
        """Вычислить node во временный регистр (сохранив его в стеке).

        Регистр восстанавливается сразу после использования, поэтому ему
        достаточно не встречаться внутри самого node."""
        fam = self.alloc(_refs(node) | set(avoid))
        self.emit("push", self.reg(fam))
        self.busy.add(fam)
        self.gen(node, fam)
        return fam

    def release(self, fam):
        self.emit("pop", self.reg(fam))
        self.busy.discard(fam)

    def simple(self, node):
        """Операнд, который можно подставить в команду как есть."""
        if N.is_const(node):
            return N.ImmOperand(node)
        if isinstance(node, N.RegNode) and node.reg.size == self.width:
            return N.RegOperand(node.reg)
        if isinstance(node, N.MemNode) and node.mem.size in (None, self.width):
            m = node.mem
            return N.MemOperand(self.width, m.seg, m.base, m.index, m.scale, m.disp)
        return None

    def gen(self, node, fam):
        R = self.reg(fam)
        if N.is_const(node):
            if N.fold_const(node) == 0:
                self.emit("xor", R, R)
            else:
                self.emit("mov", R, N.ImmOperand(node))
        elif isinstance(node, N.RegNode):
            r = node.reg
            if r.size == self.width:
                if _family(r) != fam:
                    self.emit("mov", R, N.RegOperand(r))
            elif r.size < self.width:
                self.emit("movsx" if self.signed else "movzx", R, N.RegOperand(r))
            else:
                self.emit("mov", R, self.reg(_family(r)))
        elif isinstance(node, N.MemNode):
            m = node.mem
            size = m.size or self.width
            if size > self.width:
                _error(f"память размером {size} бит не помещается в {self.width}-битное вычисление", self.tok)
            mem = N.MemOperand(size, m.seg, m.base, m.index, m.scale, m.disp)
            if size == self.width:
                self.emit("mov", R, mem)
            else:
                self.emit("movsx" if self.signed else "movzx", R, mem)
        elif isinstance(node, N.Unary):
            self.gen(node.x, fam)
            if node.op == "-":
                self.emit("neg", R)
            elif node.op == "~":
                self.emit("not", R)
        elif isinstance(node, N.Binary):
            a, b, op = node.a, node.b, node.op
            # сложную часть считаем первой — так нужно меньше временных регистров.
            # Нельзя, если слева сам рабочий регистр (let eax - "eax * (...)"):
            # его значение должно быть прочитано до того, как он изменится.
            own = isinstance(a, N.RegNode) and _family(a.reg) == fam
            if self.simple(b) is None and self.simple(a) is not None and not own:
                if op in ("+", "*", "&", "|", "^"):
                    a, b = b, a
                elif op == "-":                     # a - b = -b + a
                    self.gen(b, fam)
                    self.emit("neg", R)
                    self.emit("add", R, self.simple(a))
                    return
            self.gen(a, fam)
            self.apply(op, fam, b)
        else:
            _error("в let недопустимо такое выражение", self.tok)

    def apply(self, op, fam, b):
        R = self.reg(fam)
        if op in ("+", "-", "&", "|", "^"):
            mn = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}[op]
            src = self.simple(b)
            if src is not None:
                self.emit(mn, R, src)
            else:
                t = self.temp(b)
                self.emit(mn, R, self.reg(t))
                self.release(t)
        elif op == "*":
            if N.is_const(b):
                k = _pow2(b)
                if k is not None:
                    if k:
                        self.emit("shl", R, N.ImmOperand(N.Num(k)))
                else:
                    self.emit("imul", R, R, N.ImmOperand(b))
                return
            src = self.simple(b)
            if src is not None:
                self.emit("imul", R, src)
            else:
                t = self.temp(b)
                self.emit("imul", R, self.reg(t))
                self.release(t)
        elif op in ("<<", ">>"):
            mn = "shl" if op == "<<" else ("sar" if self.signed else "shr")
            if N.is_const(b):
                self.emit(mn, R, N.ImmOperand(b))
                return
            t = self.temp(b, avoid=(ECX,))
            self.emit("push", self.reg(ECX))
            self.emit("mov", self.reg(ECX), self.reg(t))
            self.emit(mn, R, N.RegOperand(x86.REGISTERS["cl"]))
            self.emit("pop", self.reg(ECX))
            self.release(t)
        elif op in ("/", "%"):
            k = _pow2(b) if N.is_const(b) else None
            if k is not None and not self.signed:
                if op == "/":
                    if k:
                        self.emit("shr", R, N.ImmOperand(N.Num(k)))
                else:
                    self.emit("and", R, N.ImmOperand(N.Num((1 << k) - 1)))
                return
            t = self.temp(b, avoid=(EAX, EDX))
            if fam != EAX:
                self.emit("push", self.reg(EAX))
            if fam != EDX:
                self.emit("push", self.reg(EDX))
            if fam != EAX:
                self.emit("mov", self.reg(EAX), R)
            if self.signed:
                self.emit("cdq" if self.width == 32 else "cwd")
            else:
                self.emit("xor", self.reg(EDX), self.reg(EDX))
            self.emit("idiv" if self.signed else "div", self.reg(t))
            res = EAX if op == "/" else EDX
            if fam != res:
                self.emit("mov", R, self.reg(res))
            if fam != EDX:
                self.emit("pop", self.reg(EDX))
            if fam != EAX:
                self.emit("pop", self.reg(EAX))
            self.release(t)
        else:
            _error(f"операция {op} в let не поддерживается", self.tok)
