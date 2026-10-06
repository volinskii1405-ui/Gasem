"""Парсер Gasem: строки исходника → список операторов (nodes.*)."""

import os

from . import nodes as N
from . import x86
from .errors import GasemError, SourceLoc
from .lexer import tokenize, NUM, STR, ID, OP, SEP

DATA_UNITS = {"b": 1, "w": 2, "d": 4, "q": 8}
SIZE_WORDS = {"byte": 8, "word": 16, "dword": 32, "qword": 64, "b": 8, "w": 16, "d": 32, "q": 64}
JUMP_WORDS = {"short", "near", "far"}
DIRECTIVES = {"og", "align", "incbin", "include", "do", "equ"}
RESERVED = set(x86.REGISTERS) | set(SIZE_WORDS) | JUMP_WORDS | {"a20"} | DIRECTIVES

# Приоритеты бинарных операторов (от низшего к высшему).
BINARY_LEVELS = [("|",), ("^",), ("&",), ("<<", ">>"), ("+", "-"), ("*", "/", "%")]

SEP_HINT = ("разделитель операндов в Gasem — дефис с пробелами ' - '; "
            "в выражениях пишите минус без пробелов (510-($-$$)) или в скобках")


class TokenStream:
    def __init__(self, toks, loc):
        self.toks = toks
        self.pos = 0
        self.loc = loc

    def peek(self, k=0):
        j = self.pos + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def next(self):
        t = self.peek()
        if t is None:
            self.error("неожиданный конец строки")
        self.pos += 1
        return t

    def at_end(self):
        return self.pos >= len(self.toks)

    def rest(self):
        r = self.toks[self.pos:]
        self.pos = len(self.toks)
        return r

    def error(self, msg, tok=None):
        if tok is None:
            tok = self.peek()
        if tok is None and self.toks:
            last = self.toks[-1]
            col = last.col + len(last.text)
        else:
            col = tok.col if tok is not None else None
        raise GasemError(msg, self.loc, col)

    def expect_end(self, what="строки"):
        t = self.peek()
        if t is not None:
            if t.kind == SEP:
                self.error(f"лишний разделитель ' - ' ({SEP_HINT})", t)
            if t.is_op(","):
                self.error("операнды разделяются ' - ' (дефис с пробелами), а не запятой", t)
            self.error(f"неожиданное '{t.text}' — ожидался конец {what}", t)


def split_seps(toks):
    groups = [[]]
    for t in toks:
        if t.kind == SEP:
            groups.append([])
        else:
            groups[-1].append(t)
    return groups


class Parser:
    MAX_INCLUDE_DEPTH = 32

    def __init__(self):
        self.statements = []
        self.lines = []           # все строки исходника (для листинга)
        self.var_defs = {}        # имя переменной -> DataStmt (для [имя] в do)
        self.errors = []
        self.last_global = None
        self.pending_labels = []  # метки, за которыми ещё не было оператора
        self.include_stack = []

    # ------------------------------------------------ файлы

    def parse_file(self, path, loc=None):
        apath = os.path.abspath(path)
        if apath in self.include_stack:
            raise GasemError(f"циклическое подключение файла '{path}'", loc)
        if len(self.include_stack) >= self.MAX_INCLUDE_DEPTH:
            raise GasemError("слишком глубокая вложенность include", loc)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            raise GasemError(f"не удалось открыть файл '{path}': {e.strerror}", loc)
        except UnicodeDecodeError:
            raise GasemError(f"файл '{path}' не в кодировке UTF-8", loc)
        self.include_stack.append(apath)
        try:
            self.parse_text(text, path, os.path.dirname(apath))
        finally:
            self.include_stack.pop()

    def parse_text(self, text, filename="<источник>", base_dir=None):
        if base_dir is None:
            base_dir = os.getcwd()
        for lineno, line in enumerate(text.splitlines(), 1):
            loc = SourceLoc(filename, lineno, line)
            self.lines.append(loc)
            try:
                toks = tokenize(line, loc)
                if toks:
                    self.parse_line(toks, loc, base_dir)
            except GasemError as e:
                if e.loc is None:
                    e.loc = loc
                self.errors.append(e)

    # ------------------------------------------------ имена

    def check_name(self, tok):
        if tok.value.lower() in RESERVED:
            raise GasemError(f"'{tok.value}' — зарезервированное слово, его нельзя использовать как имя",
                             None, tok.col)

    def full_name(self, tok):
        name = tok.value
        if name.startswith("."):
            if self.last_global is None:
                raise GasemError(f"локальная метка '{name}' без предшествующей глобальной метки", None, tok.col)
            return self.last_global + name
        return name

    def define_label(self, tok, loc):
        self.check_name(tok)
        name = self.full_name(tok)
        if not tok.value.startswith("."):
            self.last_global = name
        st = N.LabelStmt(name, loc, tok.col)
        self.statements.append(st)
        self.pending_labels.append(name)
        return st

    def emit(self, st):
        if isinstance(st, N.DataStmt):
            for name in self.pending_labels:
                self.var_defs[name] = st
        self.pending_labels = []
        self.statements.append(st)

    # ------------------------------------------------ строки

    def parse_line(self, toks, loc, base_dir):
        ts = TokenStream(toks, loc)
        # метки "имя:" в начале строки
        while True:
            t0, t1 = ts.peek(), ts.peek(1)
            if (t0 is not None and t0.kind == ID and t1 is not None and t1.is_op(":")
                    and t0.value.lower() not in DATA_UNITS):
                self.define_label(t0, loc)
                ts.pos += 2
                continue
            break
        if ts.at_end():
            return
        t0, t1 = ts.peek(), ts.peek(1)
        # константа: ИМЯ = выражение  /  ИМЯ equ выражение
        if t0.kind == ID and t1 is not None and (t1.is_op("=") or t1.is_id("equ")):
            self.check_name(t0)
            name = self.full_name(t0)
            ts.pos += 2
            expr = self.parse_expr(ts)
            ts.expect_end()
            self.emit(N.ConstStmt(name, expr, loc, t0.col))
            return
        self.parse_statement(ts, loc, base_dir, emit=True)

    def is_data(self, ts, k):
        t, s = ts.peek(k), ts.peek(k + 1)
        return (t is not None and t.kind == ID and t.value.lower() in DATA_UNITS
                and s is not None and s.kind == OP and s.value in (":", "-", "/") and not s.space)

    def parse_statement(self, ts, loc, base_dir, emit):
        """Разобрать один оператор. emit=False — вернуть его (для &&)."""
        t = ts.peek()

        if t.is_op("&&"):
            ts.next()
            if ts.at_end():
                ts.error("после && ожидается число повторений")
            count = self.parse_expr(ts)
            if ts.at_end():
                ts.error("после числа повторений ожидается команда или данные: && N b: 0")
            if ts.peek().kind == SEP:
                ts.error(SEP_HINT)
            body = self.parse_statement(ts, loc, base_dir, emit=False)
            if body is None:
                ts.error("&& повторяет одну команду или директиву данных")
            st = N.TimesStmt(count, body, loc)
            if emit:
                self.emit(st)
            return st

        if self.is_data(ts, 0):
            st = self.parse_data(ts, loc)
            if emit:
                self.emit(st)
            return st

        # переменная без двоеточия: msg b: "Hello" - 0
        if t.kind == ID and self.is_data(ts, 1):
            if not emit:
                ts.error("внутри && нельзя объявлять метку")
            self.define_label(ts.next(), loc)
            st = self.parse_data(ts, loc)
            self.emit(st)
            return st

        if t.kind != ID:
            ts.error(f"ожидалась команда, а встретилось '{t.text}'")
        word = t.value.lower()

        if word == "og":
            ts.next()
            expr = self.parse_expr(ts)
            ts.expect_end()
            return self._finish(N.OrgStmt(expr, loc), emit)

        if word == "b":   # b 16 / b 32 — режим
            ts.next()
            if ts.at_end():
                ts.error("после b ожидается режим (b 16 или b 32), либо данные (b: ...)")
            expr = self.parse_expr(ts)
            ts.expect_end()
            return self._finish(N.BitsStmt(expr, loc), emit)

        if word == "align":
            ts.next()
            groups = split_seps(ts.rest())
            if len(groups) not in (1, 2) or not all(groups):
                ts.error("формат: align N  или  align N - байт_заполнения")
            exprs = [self.parse_expr_tokens(g, loc) for g in groups]
            return self._finish(N.AlignStmt(exprs[0], exprs[1] if len(exprs) > 1 else None, loc), emit)

        if word == "include":
            if not emit:
                ts.error("include нельзя повторять через &&")
            ts.next()
            path = self.parse_path(ts, "include")
            ts.expect_end()
            self.parse_file(self.resolve_path(path, base_dir), loc)
            return None

        if word == "incbin":
            ts.next()
            path = self.parse_path(ts, "incbin")
            extra = []
            if not ts.at_end():
                if ts.peek().kind != SEP:
                    ts.error("формат: incbin \"файл\" [- смещение [- длина]]")
                ts.next()
                extra = [self.parse_expr_tokens(g, loc) for g in split_seps(ts.rest())]
            full = self.resolve_path(path, base_dir)
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError as e:
                raise GasemError(f"не удалось открыть файл '{path}': {e.strerror}", loc)
            vals = []
            for e in extra:
                v = N.fold_const(e)
                if v is None or v < 0:
                    raise GasemError("смещение и длина в incbin должны быть неотрицательными числами", loc)
                vals.append(v)
            if len(vals) > 2:
                raise GasemError("формат: incbin \"файл\" [- смещение [- длина]]", loc)
            if vals:
                data = data[vals[0]:]
            if len(vals) > 1:
                data = data[:vals[1]]
            return self._finish(N.IncbinStmt(data, loc), emit)

        if word == "do":
            return self._finish(self.parse_do(ts, loc), emit)

        # префиксы: rep, lock, сегментные
        prefixes = []
        while t is not None and t.kind == ID:
            low = t.value.lower()
            nxt = ts.peek(1)
            if low in x86.PREFIXES and nxt is not None:
                prefixes.append(x86.PREFIXES[low])
            elif low in x86.SEG_PREFIX and nxt is not None and nxt.kind == ID:
                prefixes.append(x86.SEG_PREFIX[low])
            else:
                break
            ts.next()
            t = ts.peek()
        if t is None or t.kind != ID:
            ts.error("после префикса ожидается команда")

        mn = x86.canonical(t.value)
        if mn is None:
            nxt = ts.peek(1)
            hint = ""
            if nxt is None and t.value.lower() not in RESERVED:
                hint = f" (если это метка — добавьте двоеточие: {t.value}:)"
            elif nxt is not None and nxt.is_op("-") and not nxt.space:
                hint = " (разделитель операндов — ' - ' с пробелами)"
            ts.error(f"неизвестная команда '{t.value}'{hint}", t)
        ts.next()
        operands = self.parse_operands(ts.rest(), loc, ts)
        return self._finish(N.InstrStmt(prefixes, mn, operands, loc, t.value), emit)

    def _finish(self, st, emit):
        if emit:
            self.emit(st)
        return st

    def parse_path(self, ts, what):
        t = ts.peek()
        if t is None or t.kind != STR:
            ts.error(f"после {what} ожидается имя файла в кавычках")
        ts.next()
        return t.value.decode("utf-8", errors="replace")

    @staticmethod
    def resolve_path(path, base_dir):
        if os.path.isabs(path):
            return path
        return os.path.join(base_dir, path)

    # ------------------------------------------------ данные

    def parse_data(self, ts, loc):
        ut = ts.next()
        name = ut.value.lower()
        unit = DATA_UNITS[name]
        kind_tok = ts.next()
        if kind_tok.value == ":":
            items = self.parse_items(ts.rest(), loc)
            return N.DataStmt(unit, "list", None, items, loc, name)
        if kind_tok.value == "-":
            if ts.at_end():
                ts.error(f"после {name}- ожидается количество: {name}-N")
            count = self.parse_expr(ts)
            if not ts.at_end() and ts.peek().kind == SEP:
                ts.error(f"{name}-N заполняет нулями и не принимает значений; "
                         f"для значений используйте {name}/ N: значения")
            ts.expect_end()
            return N.DataStmt(unit, "fill", count, [], loc, name)
        # b/ N: значения
        if ts.at_end():
            ts.error(f"после {name}/ ожидается количество: {name}/ N: значения")
        count = self.parse_expr(ts)
        if not ts.at_end() and (ts.peek().is_op(":") or ts.peek().kind == SEP):
            ts.next()
        items = self.parse_items(ts.rest(), loc)
        if not items:
            raise GasemError(f"{name}/ N требует инициализации: {name}/ N: значения", loc, kind_tok.col)
        return N.DataStmt(unit, "fixed", count, items, loc, name)

    def parse_items(self, toks, loc):
        if not toks:
            return []
        items = []
        for g in split_seps(toks):
            if not g:
                raise GasemError("пустое значение (лишний разделитель ' - ')", loc, toks[0].col)
            items.append(self.parse_expr_tokens(g, loc))
        return items

    # ------------------------------------------------ do

    def parse_do(self, ts, loc):
        do_tok = ts.next()
        t = ts.peek()
        if t is None or t.kind != STR or t.quote != '"':
            ts.error('после do ожидается выражение в двойных кавычках: do "4+4" - ax', t or do_tok)
        ts.next()
        sep = ts.peek()
        if sep is None or sep.kind != SEP:
            ts.error("после выражения do ожидается ' - ' и приёмник: do \"4+4\" - ax", sep)
        ts.next()
        dest_toks = ts.rest()
        if not dest_toks:
            raise GasemError("не указан приёмник результата do", loc, sep.col)
        if any(x.kind == SEP for x in dest_toks):
            raise GasemError("у do только один приёмник", loc,
                             next(x.col for x in dest_toks if x.kind == SEP))
        dest = self.parse_operand(dest_toks, loc)
        inner = t.value.decode("utf-8", errors="replace")
        itoks = tokenize(inner, loc, col_base=t.col + 1, seps=False)
        if not itoks:
            raise GasemError("пустое выражение do", loc, t.col)
        its = TokenStream(itoks, loc)
        expr = self.parse_expr(its, mode="do")
        its.expect_end("выражения")
        return N.DoStmt(expr, dest, loc)

    # ------------------------------------------------ операнды

    def parse_operands(self, toks, loc, ts):
        if not toks:
            return []
        for t in toks:
            if t.is_op(","):
                raise GasemError("операнды разделяются ' - ' (дефис с пробелами), а не запятой", loc, t.col)
        groups = split_seps(toks)
        ops = []
        for g in groups:
            if not g:
                bad = next(t for t in toks if t.kind == SEP)
                raise GasemError("пустой операнд (лишний разделитель ' - ')", loc, bad.col)
            ops.append(self.parse_operand(g, loc))
        return ops

    def parse_operand(self, toks, loc):
        ts = TokenStream(toks, loc)
        size = None
        jump = None
        while True:
            t = ts.peek()
            if t is not None and t.kind == ID and ts.peek(1) is not None:
                low = t.value.lower()
                if low in SIZE_WORDS:
                    if size is not None:
                        ts.error("размер указан дважды")
                    size = SIZE_WORDS[low]
                    ts.next()
                    continue
                if low in JUMP_WORDS:
                    if jump is not None:
                        ts.error("тип перехода указан дважды")
                    jump = low
                    ts.next()
                    continue
            break
        t = ts.peek()
        if t is None:
            ts.error("ожидался операнд")

        if t.is_id("a20") and ts.peek(1) is None:
            return N.A20Operand()

        if t.is_op("["):
            mem = self.parse_mem(ts, size)
            mem.jump = jump
            ts.expect_end("операнда")
            return mem

        low = t.value.lower() if t.kind == ID else None
        if low in x86.REGISTERS:
            reg = x86.REGISTERS[low]
            nxt = ts.peek(1)
            if nxt is None:
                if size is not None and reg.kind == "gpr" and size != reg.size:
                    ts.error(f"размер не совпадает с регистром {reg.name}", t)
                return N.RegOperand(reg)
            # es:[di] — сегмент перед скобкой
            if reg.kind == "seg" and nxt.is_op(":") and ts.peek(2) is not None and ts.peek(2).is_op("["):
                ts.pos += 2
                mem = self.parse_mem(ts, size)
                if mem.seg is not None:
                    ts.error("сегмент указан дважды")
                mem.seg = reg
                mem.jump = jump
                ts.expect_end("операнда")
                return mem

        expr = self.parse_expr(ts)
        if ts.peek() is not None and ts.peek().is_op(":"):
            ts.next()
            off = self.parse_expr(ts)
            ts.expect_end("операнда")
            for part in (expr, off):
                if N.has_reg(part):
                    raise GasemError("в дальнем адресе сегмент:смещение не может быть регистров",
                                     loc, N.first_reg(part).col)
            return N.FarOperand(expr, off, size)
        ts.expect_end("операнда")
        if N.has_reg(expr):
            r = N.first_reg(expr)
            msg = f"регистр {r.reg.name} нельзя использовать в выражении"
            if any(x.is_op("-") for x in toks):
                msg += f" ({SEP_HINT})"
            raise GasemError(msg, loc, r.col)
        return N.ImmOperand(expr, size, jump)

    def parse_mem(self, ts, size):
        open_tok = ts.next()   # '['
        seg = None
        t0, t1 = ts.peek(), ts.peek(1)
        if (t0 is not None and t0.kind == ID and t0.value.lower() in x86.SEGMENTS
                and t1 is not None and t1.is_op(":")):
            seg = x86.REGISTERS[t0.value.lower()]
            ts.pos += 2
        if ts.peek() is not None and ts.peek().is_op("]"):
            ts.error("пустой адрес []")
        expr = self.parse_expr(ts, mode="mem")
        close = ts.peek()
        if close is None or not close.is_op("]"):
            ts.error("ожидалась ']'", close)
        ts.next()
        base, index, scale, disp = self.split_address(expr, ts.loc, open_tok.col)
        return N.MemOperand(size, seg, base, index, scale, disp)

    def split_address(self, node, loc, col):
        regs = []
        consts = []

        def fail(msg, n=None):
            raise GasemError(msg, loc, (n.col if n is not None and n.col is not None else col))

        def walk(n, sign):
            if isinstance(n, N.RegNode):
                if sign < 0:
                    fail("регистр в адресе нельзя вычитать", n)
                regs.append((n.reg, 1, n))
                return
            if isinstance(n, N.Binary) and n.op in ("+", "-"):
                walk(n.a, sign)
                walk(n.b, sign if n.op == "+" else -sign)
                return
            if isinstance(n, N.Binary) and n.op == "*" and N.has_reg(n):
                r, k = (n.a, n.b) if isinstance(n.a, N.RegNode) else (n.b, n.a)
                if not isinstance(r, N.RegNode) or N.has_reg(k):
                    fail("недопустимое выражение адреса", n)
                if sign < 0:
                    fail("регистр в адресе нельзя вычитать", r)
                scale = N.fold_const(k)
                if scale not in (1, 2, 4, 8):
                    fail("масштаб должен быть числом 1, 2, 4 или 8", k)
                regs.append((r.reg, scale, r))
                return
            if N.has_reg(n):
                fail("недопустимое выражение адреса: регистры можно только складывать "
                     "и умножать на 1, 2, 4, 8", N.first_reg(n))
            consts.append((sign, n))

        walk(node, 1)

        for reg, _, n in regs:
            if reg.kind != "gpr" or reg.size == 8:
                fail(f"регистр {reg.name} нельзя использовать в адресе", n)
        if len(regs) > 2:
            fail("в адресе может быть не больше двух регистров", regs[2][2])
        if len({r.size for r, _, _ in regs}) > 1:
            fail("в адресе нельзя смешивать 16- и 32-битные регистры", regs[1][2])

        base = index = None
        scale = 1
        if len(regs) == 1:
            r, s, _ = regs[0]
            if s == 1:
                base = r
            else:
                index, scale = r, s
        elif len(regs) == 2:
            (r1, s1, n1), (r2, s2, n2) = regs
            if s1 != 1 and s2 != 1:
                fail("масштаб может быть только у одного регистра", n2)
            if s1 != 1:
                base, index, scale = r2, r1, s1
            else:
                base, index, scale = r1, r2, s2
            if index.name == "esp":
                if scale == 1 and base.name != "esp":
                    base, index = index, base
                else:
                    fail("esp нельзя использовать как индексный регистр", n2)
        if index is not None and index.size == 16 and scale != 1:
            fail("масштаб (*2, *4, *8) недоступен в 16-битной адресации")

        disp = None
        for sign, n in consts:
            term = n if sign > 0 else N.Unary("-", n, n.col)
            disp = term if disp is None else N.Binary("+", disp, term, n.col)
        return base, index, scale, disp

    # ------------------------------------------------ выражения

    def parse_expr_tokens(self, toks, loc):
        ts = TokenStream(toks, loc)
        e = self.parse_expr(ts)
        ts.expect_end("выражения")
        return e

    def parse_expr(self, ts, mode=None):
        """mode: None — обычное выражение, 'mem' — адрес, 'do' — выражение do."""
        return self._binary(ts, 0, mode)

    def _binary(self, ts, level, mode):
        if level == len(BINARY_LEVELS):
            return self._unary(ts, mode)
        left = self._binary(ts, level + 1, mode)
        while True:
            t = ts.peek()
            if t is not None and t.kind == OP and t.value in BINARY_LEVELS[level]:
                ts.next()
                right = self._binary(ts, level + 1, mode)
                left = N.Binary(t.value, left, right, t.col)
            else:
                return left

    def _unary(self, ts, mode):
        t = ts.peek()
        if t is None:
            ts.error("неожиданный конец выражения")
        if t.kind == OP and t.value in ("-", "+", "~"):
            ts.next()
            return N.Unary(t.value, self._unary(ts, mode), t.col)
        return self._primary(ts, mode)

    def _primary(self, ts, mode):
        t = ts.peek()
        if t is None:
            ts.error("неожиданный конец выражения")
        if t.kind == SEP:
            ts.error(f"ожидалось значение ({SEP_HINT})")
        ts.next()
        if t.kind == NUM:
            return N.Num(t.value, t.col)
        if t.kind == STR:
            return N.Str(t.value, t.col)
        if t.is_op("("):
            e = self.parse_expr(ts, mode)
            c = ts.peek()
            if c is None or not c.is_op(")"):
                ts.error("ожидалась ')'", c)
            ts.next()
            return e
        if t.is_op("$"):
            return N.Here(t.col)
        if t.is_op("$$"):
            return N.Start(t.col)
        if t.is_op("[") and mode == "do":
            nt = ts.peek()
            if nt is None or nt.kind != ID:
                ts.error("внутри [ ] в do ожидается имя переменной: [msg]", nt)
            ts.next()
            c = ts.peek()
            if c is None or not c.is_op("]"):
                ts.error("в do внутри [ ] пишется только имя переменной: [msg]", c)
            ts.next()
            return N.Var(self.full_name(nt), t.col)
        if t.kind == ID:
            low = t.value.lower()
            if low in x86.REGISTERS:
                return N.RegNode(x86.REGISTERS[low], t.col)
            if low in RESERVED:
                ts.error(f"'{t.value}' нельзя использовать в выражении", t)
            return N.Sym(self.full_name(t), t.col)
        if t.is_op(","):
            ts.error("операнды разделяются ' - ' (дефис с пробелами), а не запятой", t)
        ts.error(f"неожиданное '{t.text}' в выражении", t)
