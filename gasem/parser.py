"""Парсер Gasem: строки исходника → список операторов (nodes.*)."""

import os

from . import nodes as N
from . import x86
from . import hints
from .errors import GasemError, GasemErrors, GasemWarning, SourceLoc
from .highlevel import CONTROL_WORDS, HighLevel, family
from .lexer import Token, tokenize, NUM, STR, ID, OP, SEP

DATA_UNITS = {"b": 1, "w": 2, "d": 4, "q": 8, "s": 1}   # s: — строка с нулём в конце
SIZE_WORDS = {"byte": 8, "word": 16, "dword": 32, "qword": 64, "b": 8, "w": 16, "d": 32, "q": 64}
JUMP_WORDS = {"short", "near", "far"}
DIRECTIVES = {"og", "align", "incbin", "incprog", "include", "do", "equ", "pool", "args"}
BLOCK_WORDS = {"macro", "proc", "struct", "at", "local", "return"}
RESERVED = (set(x86.REGISTERS) | set(SIZE_WORDS) | JUMP_WORDS | {"a20", "s", "rel", "abs"} | DIRECTIVES
            | BLOCK_WORDS)
# слова, которые открывают блок, закрываемый end (until — для repeat)
OPENERS = {"if", "while", "for", "repeat", "macro", "proc", "struct", "at"}
MAX_MACRO_DEPTH = 32
MAX_MACRO_LINES = 100_000      # всего строк, развёрнутых из макросов (защита от взрывного роста)
# в какие регистры попадают аргументы call, если для подпрограммы нет args
DEFAULT_ARGS = {16: ["ax", "bx", "cx", "dx", "si", "di"],
                32: ["eax", "ebx", "ecx", "edx", "esi", "edi"],
                64: ["rax", "rbx", "rcx", "rdx", "rsi", "rdi"]}

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


def minus_seps(toks):
    """Там, где нет операндов (константы, условия), ' - ' означает минус."""
    return [Token(OP, "-", t.col, t.space, "-") if t.kind == SEP else t for t in toks]


def string_data(entries, loc):
    """Метки и данные строк: (метка, bytes) → @strN: s: "..." """
    out = []
    for label, value in entries:
        out.append(N.LabelStmt(label, loc))
        out.append(N.DataStmt(1, "list", None, [N.Str(value), N.Num(0)], loc, "s"))
    return out


def reads(op):
    """Какие регистры (семейства) читает операнд."""
    if isinstance(op, N.RegOperand) and op.reg.kind == "gpr":
        return {family(op.reg)}
    if isinstance(op, N.MemOperand):
        return {family(r) for r in (op.base, op.index) if r is not None}
    return set()


def check_string_operands(ops, loc, col):
    """'a' — символ, "a" — адрес строки. Ловим путаницу с байтовым операндом."""
    if any(isinstance(o, N.ImmOperand) and o.from_string for o in ops) and any(
            (isinstance(o, N.RegOperand) and o.reg.size == 8)
            or (isinstance(o, N.MemOperand) and o.size == 8) for o in ops):
        raise GasemError("строка в двойных кавычках — это адрес строки; "
                         "символ записывается в одинарных кавычках: 'a'", loc, col)


def statement_word(toks):
    """Первое слово оператора после меток «имя:» (в нижнем регистре) или ""."""
    i = 0
    while (i + 1 < len(toks) and toks[i].kind == ID and toks[i + 1].is_op(":")
           and toks[i].value.lower() not in DATA_UNITS):
        i += 2
    return toks[i].value.lower() if i < len(toks) and toks[i].kind == ID else ""


def _is_id_char(c):
    return c.isalnum() or c in "_."


class MacroLimit(GasemError):
    """Превышен предел макросов: раскрытие прерывается целиком (одна ошибка, а не тысячи)."""


class Macro:
    def __init__(self, name, params, loc):
        self.name = name
        self.params = params
        self.loc = loc
        self.body = []          # (место, текст строки)
        self.depth = 1          # вложенность блоков при сборе тела
        self.internal = set()   # метки и константы, объявленные внутри (у каждого вызова свои)
        self.broken = False     # в заголовке была ошибка: тело пропускается

    def finish(self):
        for _, text in self.body:
            try:
                toks = tokenize(text, None)
            except GasemError:
                continue
            i = 0
            while (i + 1 < len(toks) and toks[i].kind == ID and toks[i + 1].is_op(":")
                   and toks[i].value.lower() not in DATA_UNITS):
                self.internal.add(toks[i].value)
                i += 2
            if i + 1 < len(toks) and toks[i].kind == ID and (
                    toks[i + 1].is_op("=") or toks[i + 1].is_id("equ")
                    or (toks[i + 1].kind == ID and toks[i + 1].value.lower() in DATA_UNITS
                        and i + 2 < len(toks) and toks[i + 2].kind == OP
                        and toks[i + 2].value in (":", "-", "/") and not toks[i + 2].space)):
                self.internal.add(toks[i].value)
        self.internal -= set(self.params)

    def substitute(self, text, args):
        """Подставить аргументы вместо параметров (и в выражения do/let в кавычках)."""
        if not args:
            return text
        try:
            toks = tokenize(text, None)
        except GasemError:
            return text
        spans = []
        is_let = statement_word(toks) == "let"
        for k, t in enumerate(toks):
            if t.kind == ID and t.value in args:
                spans.append((t.col, t.col + len(t.text), args[t.value]))
            elif t.kind == STR and t.quote == '"' and (is_let or (k > 0 and toks[k - 1].is_id("do"))):
                inner = t.text[1:-1]
                i = 0
                while i < len(inner):
                    c = inner[i]
                    if (c.isalpha() or c in "_.") and (i == 0 or not _is_id_char(inner[i - 1])):
                        j = i + 1
                        while j < len(inner) and _is_id_char(inner[j]):
                            j += 1
                        if inner[i:j] in args:
                            spans.append((t.col + 1 + i, t.col + 1 + j, args[inner[i:j]]))
                        i = j
                    elif c.isalnum():
                        while i < len(inner) and _is_id_char(inner[i]):
                            i += 1
                    else:
                        i += 1
        for start, end, value in sorted(spans, reverse=True):
            text = text[:start] + value + text[end:]
        return text


class Struct:
    def __init__(self, name, loc):
        self.name = name
        self.loc = loc
        self.fields = []        # (имя, смещение, размер элемента, количество, структура или None)
        self.size = 0
        self.consts = []        # ("x", смещение), ("pos.x", смещение) ... — с вложенными полями


def widen_move(r, a, loc):
    """mov r - a; меньший регистр расширяется нулями (из 32 в 64 бита — mov в 32-битную часть)."""
    if isinstance(a, N.RegOperand) and a.reg.kind == "gpr" and a.reg.size < r.size:
        if a.reg.size == 32:
            low = x86.REGISTERS[x86.FAMILY_NAMES[32][r.num]]
            return N.InstrStmt([], "mov", [N.RegOperand(low), a], loc)
        return N.InstrStmt([], "movzx", [N.RegOperand(r), a], loc)
    return N.InstrStmt([], "mov", [N.RegOperand(r), a], loc)


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
        self.bits = 16            # текущий режим (b 16 / b 32) — нужен для let
        self.current_loc = None
        self.hl = HighLevel(self)  # if/while/for/let
        self.call_args = {}       # подпрограмма -> регистры аргументов (args)
        self.pending_strings = {} # строки из команд после последнего pool: bytes -> метка
        self.string_first_use = {}  # метка строки -> первый оператор, где она нужна
        self.strings_waiting = [] # строки текущей строки, ещё не привязанные к оператору
        self.string_counter = 0
        self.referenced = set()   # имена, на которые есть ссылки
        self.defined = []         # метки программы: (имя, место, столбец)
        self.first_label = None   # точка входа — о ней не предупреждаем
        self.warnings = []
        self.finished = False
        self.macros = {}          # имя -> Macro
        self.collecting = None    # макрос, тело которого сейчас собирается
        self.macro_depth = 0
        self.macro_counter = 0
        self.macro_lines = 0
        self.structs = {}         # имя -> Struct
        self.programs = ()        # программы, которые собирают эту через incprog

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
        depth = len(self.hl.blocks)
        for lineno, line in enumerate(text.splitlines(), 1):
            loc = SourceLoc(filename, lineno, line)
            self.lines.append(loc)
            self.current_loc = loc
            self.strings_waiting = []
            if self.collecting is not None:
                self.collect_macro_line(line, loc)
                continue
            try:
                toks = tokenize(line, loc)
                if toks:
                    self.parse_line(toks, loc, base_dir)
            except GasemError as e:
                if e.loc is None:
                    e.loc = loc
                self.errors.append(e)
        if self.collecting is not None:
            self.errors.append(GasemError("macro без end", self.collecting.loc))
            self.collecting = None
        self.hl.close_file(depth, self.errors)

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
        if not tok.value.startswith((".", "@")):     # @… — метки внутри макросов
            self.last_global = name
        st = N.LabelStmt(name, loc, tok.col)
        self.statements.append(st)
        self.pending_labels.append(name)
        self.defined.append((name, loc, tok.col))
        if self.first_label is None and not name.startswith("@"):
            self.first_label = name
        return st

    def emit(self, st):
        if isinstance(st, N.DataStmt):
            for name in self.pending_labels:
                self.var_defs[name] = st
        self.pending_labels = []
        for label in self.strings_waiting:
            self.string_first_use.setdefault(label, st)
        self.strings_waiting = []
        self.statements.append(st)

    # ------------------------------------------------ строки

    def parse_line(self, toks, loc, base_dir):
        blk = self.hl.blocks[-1] if self.hl.blocks else None
        if blk is not None and blk.kind == "struct" and statement_word(toks) != "end":
            self.struct_field(blk, toks, loc)
            return
        if blk is not None and blk.kind == "proc" and blk.header and not toks[0].is_id("local"):
            self.hl.prologue(blk, loc)
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
            ts.toks = minus_seps(ts.toks)      # у константы нет операндов: ' - ' — это минус
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

        if emit and not t.is_op("&&"):   # условие в конце строки: ret if carry
            idx = next((j for j in range(ts.pos + 1, len(ts.toks)) if ts.toks[j].is_id("if")), None)
            if idx is not None:
                return self.parse_postfix_if(ts, idx, loc, base_dir)

        if t.is_op("&&"):
            ts.next()
            if ts.at_end():
                ts.error("после && ожидается число повторений")
            count = self.parse_expr(ts)
            if ts.at_end():
                ts.error("после числа повторений ожидается команда или данные: && N b: 0")
            if ts.peek().kind == SEP:
                ts.error(SEP_HINT)
            if not ts.peek().is_id("if") and any(x.is_id("if") for x in ts.toks[ts.pos:]):
                ts.error("условие в конце строки нельзя сочетать с &&")
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

        if t.value in self.macros:
            if not emit:
                ts.error("макрос нельзя повторять через && — повторите команды внутри макроса")
            ts.next()
            self.expand_macro(self.macros[t.value], t, ts.rest(), loc, base_dir)
            return None

        if t.value in self.structs and not (ts.peek(1) is not None and ts.peek(1).is_op(":")):
            ts.next()
            return self.struct_instance(self.structs[t.value], t, ts.rest(), loc, emit)

        if word in BLOCK_WORDS:
            if not emit:
                ts.error(f"{word} нельзя повторять через &&")
            ts.next()
            getattr(self, "word_" + word)(t, ts.rest(), loc)
            return None

        if word in CONTROL_WORDS:
            if not emit:
                ts.error(f"{word} нельзя повторять через &&")
            ts.next()
            self.hl.statement(word, t, ts.rest(), loc)
            return None

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
            if N.fold_const(expr) in (16, 32, 64):
                self.bits = N.fold_const(expr)
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

        if word == "incprog":
            if not emit:
                ts.error("incprog нельзя повторять через &&")
            ts.next()
            path = self.parse_path(ts, "incprog")
            ts.expect_end()
            data = self.compile_program(self.resolve_path(path, base_dir), path, loc)
            return self._finish(N.IncbinStmt(data, loc), emit)

        if word == "do":
            return self._finish(self.parse_do(ts, loc), emit)

        if word == "pool":   # сюда кладутся строки из команд выше
            if not emit:
                ts.error("pool нельзя повторять через &&")
            ts.next()
            ts.expect_end()
            st = N.PoolStmt(loc)
            st.entries = [(label, value) for value, label in self.pending_strings.items()]
            self.pending_strings = {}
            self.emit(st)
            return None

        if word == "args":   # args puts - esi
            if not emit:
                ts.error("args нельзя повторять через &&")
            ts.next()
            self.parse_args(ts, loc)
            return None

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
            hint = hints.suggest_command(t.value)
            if hint:
                pass
            elif nxt is None and t.value.lower() not in RESERVED:
                hint = f" (если это метка — добавьте двоеточие: {t.value}:)"
            elif nxt is not None and nxt.is_op("-") and not nxt.space:
                hint = " (разделитель операндов — ' - ' с пробелами)"
            ts.error(f"неизвестная команда '{t.value}'{hint}", t)
        ts.next()
        operands = self.parse_operands(ts.rest(), loc, ts)
        check_string_operands(operands, loc, t.col)
        if mn in ("push", "pop") and len(operands) > 1:
            # push eax - ebx - ecx; pop снимает в обратном порядке: pop eax - ebx - ecx
            if not emit:
                ts.error("&& повторяет одну команду — список в push/pop здесь нельзя", t)
            for op in (operands if mn == "push" else operands[::-1]):
                self.emit(N.InstrStmt(prefixes, mn, [op], loc, t.value))
            return None
        if mn in ("call", "jmp") and len(operands) > 1:
            if not emit:
                ts.error("вызов с аргументами нельзя повторять через &&", t)
            st = N.CallStmt(mn, prefixes, operands[0], operands[1:], self.bits, loc)
            self.emit(st)
            return st
        return self._finish(N.InstrStmt(prefixes, mn, operands, loc, t.value), emit)

    def parse_postfix_if(self, ts, idx, loc, base_dir):
        """команда if условие — выполнить команду, только если условие истинно."""
        stmt, if_tok, cond = ts.toks[ts.pos:idx], ts.toks[idx], ts.toks[idx + 1:]
        if not cond:
            raise GasemError("после if ожидается условие: ret if carry", loc, if_tok.col)
        first = stmt[0]
        word = first.value.lower() if first.kind == ID else ""
        if word in ("break", "continue") and len(stmt) == 1:
            self.hl.loop_jump(word, first, if_tok, cond, loc)
            return None
        if word == "return":
            blk = self.hl.proc_block(first)
            acc = x86.FAMILY_NAMES[blk.bits][0]
            if len(stmt) == 1 or (len(stmt) == 2 and stmt[1].is_id(acc)):   # return [eax] if … — один переход
                self.hl.cond_jump(blk.exit, if_tok, cond, loc)
                return None
        if ((word in CONTROL_WORDS and word != "let") or word in ("macro", "proc", "struct", "at", "local")
                or first.is_op("&&")):
            raise GasemError(f"'{first.text}' нельзя дополнить условием в конце строки", loc, first.col)
        if x86.canonical(word) == "jmp" and len(stmt) > 1 and not any(x.kind == SEP for x in stmt):
            op = self.parse_operand(stmt[1:], loc)
            if isinstance(op, N.ImmOperand) and isinstance(op.expr, N.Sym) and op.jump != "far":
                self.hl.cond_jump(op.expr.name, if_tok, cond, loc)     # один условный переход
                return None
        skip = self.hl.new_label("skip")
        self.hl.cond_jump(skip, if_tok, cond, loc, value=False)
        self.parse_statement(TokenStream(stmt, loc), loc, base_dir, emit=True)
        self.hl.label(skip, loc)
        return None

    # ------------------------------------------------ macro

    def word_macro(self, word, toks, loc):
        # тело собирается даже при ошибке в заголовке — иначе оно разобралось бы как обычный код
        self.collecting = Macro("", [], loc)
        self.collecting.broken = True
        if self.hl.blocks or self.macro_depth:
            raise GasemError("macro объявляется отдельно — не внутри if/while/for/proc и не внутри макроса",
                             None, word.col)
        groups = split_seps(toks)
        fmt = "формат: macro имя - параметр - параметр ..."
        if not groups[0] or len(groups[0]) != 1 or groups[0][0].kind != ID or not all(groups):
            raise GasemError(fmt, None, word.col)
        name_tok = groups[0][0]
        name = name_tok.value
        self.check_name(name_tok)
        if name.startswith("."):
            raise GasemError("имя макроса не может начинаться с точки", None, name_tok.col)
        if x86.canonical(name) or name.lower() in CONTROL_WORDS:
            raise GasemError(f"'{name}' — команда Gasem, макрос так назвать нельзя", None, name_tok.col)
        if name in self.macros:
            raise GasemError(f"макрос '{name}' уже объявлен", None, name_tok.col)
        params = []
        for g in groups[1:]:
            if len(g) != 1 or g[0].kind != ID or g[0].value.startswith("."):
                raise GasemError("параметр макроса — одно имя (например: macro print - text - color)", None, g[0].col)
            self.check_name(g[0])
            if g[0].value in params:
                raise GasemError(f"параметр '{g[0].value}' повторяется", None, g[0].col)
            params.append(g[0].value)
        self.collecting = Macro(name, params, loc)

    def collect_macro_line(self, line, loc):
        m = self.collecting
        try:
            word = statement_word(tokenize(line, loc))
        except GasemError:
            word = ""
        if word in OPENERS:
            m.depth += 1
        elif word in ("end", "until"):
            m.depth -= 1
            if m.depth == 0:
                if not m.broken:
                    m.finish()
                    self.macros[m.name] = m
                self.collecting = None
                return
        m.body.append((loc, line))

    def expand_macro(self, m, name_tok, toks, loc, base_dir):
        if self.macro_depth >= MAX_MACRO_DEPTH:
            raise MacroLimit("слишком глубокая вложенность макросов (макрос вызывает сам себя?)")
        self.macro_lines += len(m.body)
        if self.macro_lines > MAX_MACRO_LINES:
            raise MacroLimit(f"из макросов получается слишком много строк (больше {MAX_MACRO_LINES}) — "
                             f"макрос вызывает сам себя?")
        groups = split_seps(toks) if toks else []
        if len(groups) != len(m.params) or not all(groups):
            names = " - ".join(m.params) or "без параметров"
            raise GasemError(f"макрос {m.name} принимает {len(m.params)} параметр(а) ({names}), "
                             f"а передано {len(groups)}", None, name_tok.col)
        text = loc.text
        args = {p: text[g[0].col:g[-1].col + len(g[-1].text)] for p, g in zip(m.params, groups)}
        self.macro_counter += 1
        prefix = f"@m{self.macro_counter}."
        via = f"в макросе '{m.name}', вызванном в {loc.file}:{loc.line}"
        if loc.via:
            via += "; " + loc.via
        depth = len(self.hl.blocks)
        saved = self.current_loc
        self.macro_depth += 1
        try:
            for body_loc, line in m.body:
                nloc = SourceLoc(body_loc.file, body_loc.line, m.substitute(line, args), loc.top, via)
                self.current_loc = nloc
                self.strings_waiting = []
                try:
                    toks = tokenize(nloc.text, nloc)
                    for t in toks:
                        if t.kind == ID and t.value in m.internal:
                            t.value = prefix + t.value
                    if toks:
                        self.parse_line(toks, nloc, base_dir)
                except MacroLimit:
                    raise
                except GasemError as e:
                    if e.loc is None:
                        e.loc = nloc
                    self.errors.append(e)
            while len(self.hl.blocks) > depth:
                blk = self.hl.blocks.pop()
                self.errors.append(GasemError(f"{blk.kind} без end в макросе '{m.name}'", blk.loc))
        except MacroLimit:
            del self.hl.blocks[depth:]
            raise
        finally:
            self.macro_depth -= 1
            self.current_loc = saved

    # ------------------------------------------------ proc

    def word_proc(self, word, toks, loc):
        if self.hl.blocks:
            raise GasemError("proc объявляется отдельно — не внутри if/while/for/proc", None, word.col)
        idx = next((i for i, t in enumerate(toks) if t.is_id("uses")), None)
        head, uses_toks = (toks, None) if idx is None else (toks[:idx], toks[idx + 1:])
        groups = split_seps(head)
        if not groups[0] or len(groups[0]) != 1 or groups[0][0].kind != ID or not all(groups):
            raise GasemError("формат: proc имя [- регистры аргументов] [uses регистры]", None, word.col)
        name_tok = groups[0][0]
        if name_tok.value.startswith("."):
            raise GasemError("имя процедуры не может начинаться с точки", None, name_tok.col)
        params = [self.reg_word(g, "аргументы proc — регистры общего назначения", sizes=(8, 16, 32, 64))
                  for g in groups[1:]]
        uses = []
        if uses_toks is not None:
            ugroups = split_seps(uses_toks)
            if not all(ugroups):
                raise GasemError("после uses перечисляются регистры: uses eax - ebx", None, toks[idx].col)
            uses = [self.reg_word(g, "в uses перечисляются 16-, 32- или 64-битные регистры", sizes=(16, 32, 64))
                    for g in ugroups]
        self.define_label(name_tok, loc)
        name = self.full_name(name_tok)
        if params:
            if name in self.call_args:
                raise GasemError(f"аргументы '{name}' уже объявлены", None, name_tok.col)
            self.call_args[name] = params
        blk = self.hl.push("proc", loc)
        blk.name, blk.uses, blk.bits = name, uses, self.bits
        blk.locals, blk.frame_size, blk.header = {}, 0, True
        blk.exit = self.hl.new_label("ret")

    def reg_word(self, g, msg, sizes):
        reg = x86.REGISTERS.get(g[0].value.lower()) if len(g) == 1 and g[0].kind == ID else None
        if reg is None or reg.kind != "gpr" or reg.size not in sizes:
            raise GasemError(msg, None, g[0].col)
        return reg

    def word_local(self, word, toks, loc):
        blk = self.hl.blocks[-1] if self.hl.blocks else None
        if blk is None or blk.kind != "proc" or not blk.header:
            raise GasemError("local пишется в начале proc, до первой команды", None, word.col)
        groups = split_seps(toks)
        if len(groups) != 2 or not all(groups) or len(groups[0]) != 1 or groups[0][0].kind != ID:
            raise GasemError("формат: local имя - тип (byte, word, dword, qword, число байт или структура)",
                             None, word.col)
        name_tok = groups[0][0]
        name = name_tok.value
        self.check_name(name_tok)
        if name.startswith("."):
            raise GasemError("имя локальной переменной не может начинаться с точки", None, name_tok.col)
        if name in blk.locals:
            raise GasemError(f"локальная переменная '{name}' уже объявлена", None, name_tok.col)
        tt = groups[1]
        word_size = blk.bits // 8
        elem, mem_size, rest = 1, None, tt
        if tt[0].kind == ID and tt[0].value.lower() in SIZE_WORDS:
            mem_size = SIZE_WORDS[tt[0].value.lower()]
            elem, rest = mem_size // 8, tt[1:]
        elif tt[0].kind == ID and tt[0].value in self.structs:
            elem, rest = self.structs[tt[0].value].size, tt[1:]
        count = 1
        if rest:
            count = N.fold_const(self.parse_expr_tokens(rest, loc))
            if count is None or count <= 0:
                raise GasemError("размер локальной переменной — положительное число", None, rest[0].col)
        size = elem * count
        align = elem if elem in (1, 2, 4, 8) else word_size
        align = min(align, word_size)
        cur = blk.frame_size + size
        cur = (cur + align - 1) // align * align
        blk.frame_size = cur
        blk.locals[name] = (cur, mem_size)

    def local_var(self, name):
        """(смещение, размер) локальной переменной текущей proc или None."""
        for blk in self.hl.blocks:
            if blk.kind == "proc" and not blk.header:
                return blk.locals.get(name)
        return None

    def frame_reg(self):
        blk = next(b for b in self.hl.blocks if b.kind == "proc")
        return x86.REGISTERS[{16: "bp", 32: "ebp", 64: "rbp"}[blk.bits]]

    def word_return(self, word, toks, loc):
        blk = self.hl.proc_block(word)
        if toks:
            op = self.parse_operand(toks, loc)
            acc = x86.REGISTERS[x86.FAMILY_NAMES[blk.bits][0]]
            if any(family(r) == 0 for r in blk.uses):
                raise GasemError(f"return значение: {acc.name} восстанавливается из uses и затрёт результат",
                                 None, word.col)
            if not (isinstance(op, N.RegOperand) and op.reg is acc):
                self.emit(widen_move(acc, op, loc))
        self.hl.jump("jmp", blk.exit, loc)

    # ------------------------------------------------ struct

    def word_struct(self, word, toks, loc):
        if self.hl.blocks:
            raise GasemError("struct объявляется отдельно — не внутри if/while/for/proc", None, word.col)
        if len(toks) != 1 or toks[0].kind != ID:
            raise GasemError("формат: struct Имя (дальше поля, в конце end)", None, word.col)
        self.check_name(toks[0])
        name = toks[0].value
        if name.startswith("."):
            raise GasemError("имя структуры не может начинаться с точки", None, toks[0].col)
        if name in self.structs:
            raise GasemError(f"структура '{name}' уже объявлена", None, toks[0].col)
        blk = self.hl.push("struct", loc)
        blk.struct = Struct(name, loc)

    def struct_field(self, blk, toks, loc):
        st = blk.struct
        fmt = "поле структуры: имя: тип [количество] — тип b, w, d, q или другая структура (например: x: w)"
        if len(toks) < 3 or toks[0].kind != ID or not toks[1].is_op(":") or toks[2].kind != ID:
            raise GasemError(fmt, loc, toks[0].col)
        name = toks[0].value
        if name.startswith(".") or name == "size":
            raise GasemError(f"поле не может называться '{name}'", loc, toks[0].col)
        if any(f[0] == name for f in st.fields):
            raise GasemError(f"поле '{name}' уже есть в {st.name}", loc, toks[0].col)
        t = toks[2]
        low = t.value.lower()
        nested = None
        if low in DATA_UNITS and low != "s":
            elem = DATA_UNITS[low]
        elif low in SIZE_WORDS:
            elem = SIZE_WORDS[low] // 8
        elif t.value in self.structs:
            nested = self.structs[t.value]
            elem = nested.size
        else:
            raise GasemError(fmt, loc, t.col)
        count = 1
        if len(toks) > 3:
            count = N.fold_const(self.parse_expr_tokens(toks[3:], loc))
            if count is None or count <= 0:
                raise GasemError("количество элементов поля — положительное число", loc, toks[3].col)
        st.fields.append((name, st.size, elem, count, nested))
        st.consts.append((name, st.size))
        if nested is not None:
            st.consts += [(f"{name}.{sub}", st.size + off) for sub, off in nested.consts]
        st.size += elem * count

    def end_struct(self, blk, loc):
        st = blk.struct
        for suffix, off in st.consts:
            self.emit(N.ConstStmt(f"{st.name}.{suffix}", N.Num(off), loc))
        self.emit(N.ConstStmt(f"{st.name}.size", N.Num(st.size), loc))
        self.structs[st.name] = st

    def struct_instance(self, st, name_tok, toks, loc, emit):
        """Point 1 - 2 — данные по образцу структуры (неуказанные поля — нули)."""
        if not toks:
            return self._finish(N.DataStmt(1, "fill", N.Num(st.size), [], loc, "b"), emit)
        if not emit:
            raise GasemError("&& повторяет одну команду — структуру со значениями здесь задать нельзя",
                             None, name_tok.col)
        groups = split_seps(toks)
        if not all(groups):
            raise GasemError("пустое значение (лишний разделитель ' - ')", None, toks[0].col)
        if len(groups) > len(st.fields):
            raise GasemError(f"в структуре {st.name} {len(st.fields)} пол(я/ей), а значений {len(groups)}",
                             None, groups[len(st.fields)][0].col)
        unit_names = {1: "b", 2: "w", 4: "d", 8: "q"}
        for i, (fname, _, elem, count, nested) in enumerate(st.fields):
            size = elem * count
            if i >= len(groups):
                self.emit(N.DataStmt(1, "fill", N.Num(size), [], loc, "b"))
                continue
            if nested is not None or elem not in unit_names:
                raise GasemError(f"поле {fname} — структура, его нельзя задать одним значением",
                                 None, groups[i][0].col)
            value = self.parse_expr_tokens(groups[i], loc)
            if count == 1:
                self.emit(N.DataStmt(elem, "list", None, [value], loc, unit_names[elem]))
            else:
                self.emit(N.DataStmt(elem, "fixed", N.Num(count), [value], loc, unit_names[elem]))
        return None

    # ------------------------------------------------ at

    def word_at(self, word, toks, loc):
        if any(b.kind not in ("at",) for b in self.hl.blocks):
            raise GasemError("at объявляется отдельно — не внутри if/while/for/proc", None, word.col)
        if not toks:
            raise GasemError("формат: at адрес (дальше код, в конце end)", None, word.col)
        expr = self.parse_expr_tokens(minus_seps(toks), loc)
        self.emit(N.AtStmt(expr, loc))
        self.hl.push("at", loc)

    def parse_args(self, ts, loc):
        groups = split_seps(ts.rest())
        fmt = "формат: args подпрограмма - регистр - регистр ... (например: args puts - esi)"
        if len(groups) < 2 or not all(groups) or len(groups[0]) != 1 or groups[0][0].kind != ID:
            raise GasemError(fmt, loc)
        name = self.full_name(groups[0][0])
        regs = []
        for g in groups[1:]:
            reg = x86.REGISTERS.get(g[0].value.lower()) if len(g) == 1 and g[0].kind == ID else None
            if reg is None or reg.kind != "gpr":
                raise GasemError("в args перечисляются регистры общего назначения", loc, g[0].col)
            regs.append(reg)
        if name in self.call_args:
            raise GasemError(f"аргументы '{name}' уже объявлены", loc, groups[0][0].col)
        self.call_args[name] = regs

    def intern_string(self, value):
        """Строка из команды → метка. Одинаковые строки хранятся один раз."""
        label = self.pending_strings.get(value)
        if label is None:
            self.string_counter += 1
            label = f"@str{self.string_counter}"
            self.pending_strings[value] = label
        if label not in self.string_first_use:
            self.strings_waiting.append(label)
        return label

    # ------------------------------------------------ после разбора

    def finish(self):
        """Разместить строки, развернуть вызовы с аргументами, собрать предупреждения."""
        if self.finished:
            return
        self.finished = True
        inline = {}                       # строки без pool — прямо в код с обходом
        for value, label in self.pending_strings.items():
            st = self.string_first_use.get(label)
            if st is not None:
                inline.setdefault(id(st), []).append((label, value))
        out = []
        for st in self.statements:
            entries = inline.get(id(st))
            if entries:
                over = entries[0][0] + ".over"
                out.append(N.InstrStmt([], "jmp", [N.ImmOperand(N.Sym(over))], st.loc))
                out.extend(string_data(entries, st.loc))
                out.append(N.LabelStmt(over, st.loc))
            if isinstance(st, N.PoolStmt):
                out.extend(string_data(st.entries, st.loc))
            elif isinstance(st, N.CallStmt):
                try:
                    out.extend(self.expand_call(st))
                except GasemError as e:
                    if e.loc is None:
                        e.loc = st.loc
                    self.errors.append(e)
            else:
                out.append(st)
        self.statements = out
        for name, loc, col in self.defined:
            if not name.startswith("@") and name != self.first_label and name not in self.referenced:
                self.warnings.append(GasemWarning(f"метка '{name}' нигде не используется", loc, col))

    def expand_call(self, st):
        """call f - a - b → mov рег1 - a / mov рег2 - b / call f"""
        target = st.target
        name = target.expr.name if isinstance(target, N.ImmOperand) and isinstance(target.expr, N.Sym) else None
        if name in self.call_args:
            regs = self.call_args[name]
        else:
            defaults = DEFAULT_ARGS[st.bits]
            if len(st.args) > len(defaults):
                raise GasemError(f"слишком много аргументов (больше {len(defaults)})")
            regs = [x86.REGISTERS[r] for r in defaults[:len(st.args)]]
        if len(regs) != len(st.args):
            names = " - ".join(r.name for r in regs)
            raise GasemError(f"{name} принимает {len(regs)} аргумент(а) ({names}), а передано {len(st.args)}")
        moves = [(r, a) for r, a in zip(regs, st.args)
                 if not (isinstance(a, N.RegOperand) and a.reg is r)]
        ordered = []
        while moves:            # сначала те, чей регистр больше никому не нужен
            for i, (r, a) in enumerate(moves):
                if all(family(r) not in reads(a2) for j, (_, a2) in enumerate(moves) if j != i):
                    ordered.append(moves.pop(i))
                    break
            else:
                raise GasemError("аргументы зависят друг от друга по кругу (например, call f - ebx - eax "
                                 "при args f - eax - ebx) — передайте их через другие регистры")
        if {family(r) for r, _ in ordered} & reads(target):
            raise GasemError("адрес вызова зависит от регистра, в который передаётся аргумент")
        out = []
        for r, a in ordered:
            out.append(widen_move(r, a, st.loc))
        out.append(N.InstrStmt(st.prefixes, st.mnemonic, [target], st.loc))
        return out

    def _finish(self, st, emit):
        if emit:
            self.emit(st)
        return st

    def compile_program(self, full, path, loc):
        """incprog: собрать отдельную программу (со своим og и своими именами) → байты.
        Её ошибки и предупреждения добавляются к ошибкам этой программы."""
        from .assembler import Assembler
        chain = self.programs + tuple(self.include_stack[:1])
        if os.path.abspath(full) in chain:
            raise GasemError(f"программа '{path}' собирает саму себя через incprog", loc)
        if len(chain) >= self.MAX_INCLUDE_DEPTH:
            raise GasemError("слишком глубокая вложенность incprog", loc)
        sub = Parser()
        sub.programs = chain
        sub.parse_file(full, loc)
        sub.finish()
        if sub.errors:
            self.errors.extend(sub.errors)
            return b""
        try:
            result = Assembler(sub).assemble()
        except GasemErrors as e:
            self.errors.extend(e.errors)
            return b""
        self.warnings.extend(sub.warnings)
        return result.code

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
        if name == "s":
            if kind_tok.value != ":":
                ts.error("s: — строка с нулём в конце; для N нулевых байт используйте b-N", kind_tok)
            items = self.parse_items(ts.rest(), loc) + [N.Num(0)]
            return N.DataStmt(1, "list", None, items, loc, "s")
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

        if t.kind == STR and t.quote == '"' and ts.peek(1) is None:
            # "строка" в команде — адрес строки с нулём в конце (её кладёт компилятор)
            label = self.intern_string(t.value)
            return N.ImmOperand(N.Sym(label, t.col), size, jump, from_string=True)

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
            if r.local:
                acc = x86.FAMILY_NAMES[r.reg.size][0]
                raise GasemError(f"'{r.local}' — локальная переменная (лежит в стеке): значение — [{r.local}], "
                                 f"адрес — lea {acc} - [{r.local}]", loc, r.col)
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
        mode = None
        t0, t1 = ts.peek(), ts.peek(1)
        if t0 is not None and t0.is_id("rel", "abs") and t1 is not None and not t1.is_op("]"):
            mode = t0.value.lower()                 # [abs 0xB8000], [rel msg] — для режима b 64
            ts.pos += 1
        if ts.peek() is not None and ts.peek().is_op("]"):
            ts.error("пустой адрес []")
        t0, t1 = ts.peek(), ts.peek(1)
        if size is None and t0 is not None and t0.kind == ID and t1 is not None and t1.is_op("]"):
            var = self.local_var(t0.value)
            if var is not None:
                size = var[1]                     # [count] — размер из local count - dword
        expr = self.parse_expr(ts, mode="mem")
        close = ts.peek()
        if close is None or not close.is_op("]"):
            ts.error("ожидалась ']'", close)
        ts.next()
        base, index, scale, disp = self.split_address(expr, ts.loc, open_tok.col)
        mem = N.MemOperand(size, seg, base, index, scale, disp)
        mem.mode = mode
        return mem

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
            if index.name in ("esp", "rsp"):
                if scale == 1 and base.name not in ("esp", "rsp"):
                    base, index = index, base
                else:
                    fail(f"{index.name} нельзя использовать как индексный регистр", n2)
        if index is not None and index.size == 16 and scale != 1:
            fail("масштаб (*2, *4, *8) недоступен в 16-битной адресации")

        disp = None
        for sign, n in consts:
            term = n if sign > 0 else N.Unary("-", n, n.col)
            disp = term if disp is None else N.Binary("+", disp, term, n.col)
        return base, index, scale, disp

    # ------------------------------------------------ выражения

    def parse_expr_tokens(self, toks, loc, mode=None):
        ts = TokenStream(toks, loc)
        e = self.parse_expr(ts, mode)
        ts.expect_end("выражения")
        return e

    def parse_expr(self, ts, mode=None):
        """mode: None — обычное выражение, 'mem' — адрес, 'do' — выражение do,
        'let' — выражение let (можно читать память: [x], byte [esi])."""
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
            name = self.full_name(nt)
            self.referenced.add(name)
            return N.Var(name, t.col)
        if mode == "let" and (t.is_op("[") or (t.kind == ID and t.value.lower() in SIZE_WORDS
                                                and ts.peek() is not None and ts.peek().is_op("["))):
            size = None
            if t.kind == ID:
                size = SIZE_WORDS[t.value.lower()]
            else:
                ts.pos -= 1
            return N.MemNode(self.parse_mem(ts, size), t.col)
        if t.kind == ID:
            low = t.value.lower()
            if low in x86.REGISTERS:
                return N.RegNode(x86.REGISTERS[low], t.col)
            if low in RESERVED:
                ts.error(f"'{t.value}' нельзя использовать в выражении", t)
            var = self.local_var(t.value)
            if var is not None:                       # локальная переменная proc → [ebp-смещение]
                return N.Binary("+", N.RegNode(self.frame_reg(), t.col, t.value), N.Num(-var[0], t.col), t.col)
            name = self.full_name(t)
            self.referenced.add(name)
            return N.Sym(name, t.col)
        if t.is_op(","):
            ts.error("операнды разделяются ' - ' (дефис с пробелами), а не запятой", t)
        ts.error(f"неожиданное '{t.text}' в выражении", t)
