"""Многопроходная сборка программы Gasem в плоский двоичный файл.

Проходы повторяются, пока адреса меток не перестанут меняться. Короткие
формы команд (переходы на 2 байта, числа в 1 байт) выбираются оптимистично
и только растут от прохода к проходу, поэтому процесс всегда сходится.
"""

import os

from . import hints
from . import nodes as N
from . import x86
from .errors import GasemError, GasemErrors
from .expr import Evaluator, to_int
from .parser import Parser

MAX_PASSES = 100
MAX_OUTPUT = 64 * 1024 * 1024
MAX_ERRORS = 50


class CompileResult:
    def __init__(self, code, origin, symbols, listing, lines=(), warnings=()):
        self.code = code          # bytes — готовый машинный код
        self.origin = origin      # адрес загрузки (og)
        self.symbols = symbols    # имя -> значение
        self.listing = listing    # текст листинга
        self.lines = list(lines)  # (адрес, размер, режим, место в исходнике) — для отладчика
        self.warnings = list(warnings)


class EncodeCtx:
    """То, что кодировщику команд нужно знать о текущем проходе."""

    def __init__(self, asm, st, rep, addr):
        self.asm = asm
        self.st = st
        self.rep = rep
        self.addr = addr
        self.bits = asm.bits
        self.final = asm.final

    def level(self, key, need):
        """Выбранный размер кодировки (0 — самый короткий). Только растёт."""
        k = (self.rep, key)
        prev = self.st.flags.get(k, 0)
        if need > prev:
            self.st.flags[k] = need
            self.asm.changed = True
            return need
        return prev


class Assembler:
    def __init__(self, parser):
        self.stmts = parser.statements
        self.var_defs = parser.var_defs
        self.lines = parser.lines
        self.prev = {}
        self.final = False

    # ------------------------------------------------ проходы

    def assemble(self):
        for _ in range(MAX_PASSES):
            self.run_pass(final=False)
            if self.errors:
                raise GasemErrors(self.errors)
            stable = not self.changed and self.cur == self.prev
            self.prev = self.cur
            if not stable:
                continue
            self.run_pass(final=True)
            if self.errors:
                raise GasemErrors(self.errors)
            if not self.changed and self.cur == self.prev:
                return CompileResult(bytes(self.out), self.org, dict(self.cur), self.make_listing(),
                                     self.line_map)
            self.prev = self.cur
        raise GasemErrors([GasemError(
            f"размеры команд не стабилизировались за {MAX_PASSES} проходов "
            f"(возможно, адреса зависят друг от друга по кругу)")])

    def run_pass(self, final):
        self.final = final
        self.cur = {}
        self.changed = False
        self.errors = []
        self.out = bytearray()
        self.org = 0
        self.org_seen = False
        self.bits = 16
        self.do_strings = {}
        self.records = [] if final else None
        self.line_map = []
        self.shift = 0            # at: адрес, по которому код работает, минус адрес в файле
        self.at_stack = []        # (прежний shift, адрес блока at)
        for st in self.stmts:
            start = len(self.out)
            addr = self.here()
            try:
                self.exec(st, ())
            except GasemError as e:
                if e.loc is None:
                    e.loc = st.loc
                self.errors.append(e)
                del self.out[start:]
                if len(self.errors) >= MAX_ERRORS:
                    break
            if final:
                if isinstance(st, (N.OrgStmt, N.AtStmt, N.AtEndStmt)):
                    addr = self.here()
                self.records.append((st.loc.top, addr, bytes(self.out[start:])))
                if len(self.out) > start:
                    self.line_map.append((addr, len(self.out) - start, self.bits, st.loc.top))

    # ------------------------------------------------ символы

    def lookup(self, name):
        if name in self.cur:
            return self.cur[name]
        return self.prev.get(name)

    def define(self, name, value, col):
        if name in self.cur:
            raise GasemError(f"имя '{name}' уже определено", None, col)
        self.cur[name] = value

    def here(self):
        return self.org + len(self.out) + self.shift

    def start(self):
        """$$ — начало программы (og) или текущего блока at."""
        return self.at_stack[-1][1] if self.at_stack else self.org

    def eval_int(self, expr, here, need_known=False, what="значение"):
        ev = Evaluator(self, here)
        v = to_int(ev.eval(expr), expr.col)
        if need_known and not ev.known:
            raise GasemError(f"{what} должно быть известно в этом месте (без ссылок вперёд)", None, expr.col)
        return v, ev.known

    # ------------------------------------------------ выполнение операторов

    def exec(self, st, rep):
        here = self.here()

        if isinstance(st, N.LabelStmt):
            self.define(st.name, here, st.col)

        elif isinstance(st, N.ConstStmt):
            v, _ = self.eval_int(st.expr, here)
            self.define(st.name, v, st.col)

        elif isinstance(st, N.OrgStmt):
            if self.at_stack:
                v, _ = self.eval_int(st.expr, here, need_known=True, what="адрес og")
                if v != self.at_stack[-1][1]:
                    raise GasemError(f"og внутри at должен совпадать с адресом at "
                                     f"({self.at_stack[-1][1]:#x}), указано {v:#x}")
                return
            if self.org_seen:
                raise GasemError("og можно указать только один раз")
            if self.out:
                raise GasemError("og должна стоять до первой команды или данных")
            v, _ = self.eval_int(st.expr, here, need_known=True, what="адрес og")
            if v < 0:
                raise GasemError("адрес og не может быть отрицательным")
            self.org = v
            self.org_seen = True

        elif isinstance(st, N.BitsStmt):
            v, _ = self.eval_int(st.expr, here, need_known=True, what="режим")
            if v not in (16, 32):
                raise GasemError(f"поддерживаются режимы b 16 и b 32 (указано {v})")
            self.bits = v

        elif isinstance(st, N.AlignStmt):
            n, _ = self.eval_int(st.expr, here, need_known=True, what="выравнивание")
            if n <= 0 or n & (n - 1):
                raise GasemError(f"выравнивание должно быть степенью двойки (указано {n})")
            fill = 0x90
            if st.fill is not None:
                fill, _ = self.eval_int(st.fill, here)
                if not 0 <= fill <= 255:
                    raise GasemError("байт заполнения должен быть от 0 до 255")
            self.emit(bytes([fill]) * ((-here) % n))

        elif isinstance(st, N.DataStmt):
            self.emit(self.data_bytes(st, here))

        elif isinstance(st, N.TimesStmt):
            n, known = self.eval_int(st.count, here)
            if n < 0:
                if self.final:
                    raise GasemError(
                        f"отрицательное число повторений ({n}): код не помещается в отведённое место "
                        f"(например, загрузочный сектор длиннее 510 байт)")
                n = 0
            if n * 1 > MAX_OUTPUT:
                raise GasemError(f"слишком большое число повторений ({n})")
            for i in range(n):
                self.exec(st.body, rep + (i,))

        elif isinstance(st, N.InstrStmt):
            ctx = EncodeCtx(self, st, rep, here)
            ops = [self.resolve(o, here) for o in st.operands]
            self.emit(x86.encode(st.mnemonic, ops, st.prefixes, ctx))

        elif isinstance(st, N.DoStmt):
            self.exec_do(st, rep, here)

        elif isinstance(st, N.IncbinStmt):
            self.emit(st.data)

        elif isinstance(st, N.AtStmt):
            self.at_stack.append((self.shift, here))     # при ошибке в адресе блок остаётся на месте
            v, _ = self.eval_int(st.expr, here, need_known=True, what="адрес at")
            if v < 0:
                raise GasemError("адрес at не может быть отрицательным")
            self.at_stack[-1] = (self.shift, v)
            self.shift = v - (self.org + len(self.out))

        elif isinstance(st, N.AtEndStmt):
            self.shift = self.at_stack.pop()[0]

        else:
            raise AssertionError(st)

    def emit(self, data):
        if len(self.out) + len(data) > MAX_OUTPUT:
            raise GasemError("программа получается слишком большой (больше 64 МБ)")
        self.out += data

    def resolve(self, op, here):
        """Операнд из дерева → операнд для кодировщика (с вычисленными числами)."""
        if isinstance(op, N.RegOperand):
            return op.reg
        if isinstance(op, N.A20Operand):
            return x86.A20
        if isinstance(op, N.MemOperand):
            if op.disp is not None:
                disp, known = self.eval_int(op.disp, here)
            else:
                disp, known = 0, True
            return x86.Mem(op.size, op.seg, op.base, op.index, op.scale, disp, known,
                           op.disp is not None, op.jump)
        if isinstance(op, N.ImmOperand):
            v, known = self.eval_int(op.expr, here)
            return x86.Imm(v, known, op.size, op.jump)
        if isinstance(op, N.FarOperand):
            seg, k1 = self.eval_int(op.seg, here)
            off, k2 = self.eval_int(op.off, here)
            return x86.Far(seg, off, k1 and k2, op.size)
        raise AssertionError(op)

    # ------------------------------------------------ данные

    def item_bytes(self, node, unit, ev, name):
        if isinstance(node, N.Str):
            data = node.value
            if len(data) % unit:
                data += bytes(unit - len(data) % unit)
            return data
        v = to_int(ev.eval(node), node.col)
        bits = unit * 8
        if self.final and not -(1 << (bits - 1)) <= v < (1 << bits):
            raise GasemError(f"значение {v} не помещается в {name}: ({bits} бит)", None, node.col)
        return (v & ((1 << bits) - 1)).to_bytes(unit, "little")

    def data_bytes(self, st, here):
        ev = Evaluator(self, here)
        if st.kind == "list":
            return b"".join(self.item_bytes(it, st.unit, ev, st.name) for it in st.items)
        n, _ = self.eval_int(st.count, here)
        if n * st.unit > MAX_OUTPUT:
            raise GasemError(f"слишком большой размер данных ({n})", None, st.count.col)
        if st.kind == "fill":
            if n < 0:
                if self.final:
                    raise GasemError(f"{st.name}-N: количество не может быть отрицательным ({n})",
                                     None, st.count.col)
                n = 0
            return bytes(n * st.unit)
        # fixed: b/ N: значения
        if n <= 0:
            if self.final:
                raise GasemError(f"{st.name}/ N: N должно быть больше нуля (указано {n})", None, st.count.col)
            n = 0
        data = b"".join(self.item_bytes(it, st.unit, ev, st.name) for it in st.items)
        size = n * st.unit
        if len(data) > size:
            if self.final:
                raise GasemError(f"инициализатор ({len(data)} байт) не помещается в {st.name}/ {n} "
                                 f"({size} байт)", None, st.count.col)
            data = data[:size]
        return data + bytes(size - len(data))

    def var_value(self, name, ev, node):
        """Значение переменной для [имя] в do."""
        st = self.var_defs.get(name)
        if st is None:
            if self.lookup(name) is not None:
                raise GasemError(f"'{name}' — не переменная с данными; адрес получается без скобок: {name}",
                                 None, node.col)
            if not self.final:
                ev.known = False
                return 0
            hint = hints.suggest_name(name, self.var_defs)
            raise GasemError(f"неизвестная переменная '{name}'{hint}", None, node.col)
        if st.kind == "fill":
            return 0
        items = st.items
        if not items:
            return b""
        sub = Evaluator(self, ev.here)
        try:
            if any(isinstance(it, N.Str) for it in items) or (st.unit == 1 and len(items) > 1):
                data = b"".join(self.item_bytes(it, st.unit, sub, st.name) for it in items)
                return data.split(b"\0", 1)[0]
            if len(items) == 1:
                return to_int(sub.eval(items[0]), items[0].col)
        finally:
            ev.known = ev.known and sub.known
        raise GasemError(f"переменная '{name}' содержит несколько чисел — в do её значение "
                         f"можно использовать, только если это строка или одно число", None, node.col)

    # ------------------------------------------------ do

    def exec_do(self, st, rep, here):
        ev = Evaluator(self, here, mode="do")
        value = ev.eval(st.expr)
        dest = self.resolve(st.dest, here)
        ctx = EncodeCtx(self, st, rep, here)
        if isinstance(value, int):
            self.emit(x86.encode("mov", [dest, x86.Imm(value, ev.known)], [], ctx))
            return
        # Строка: кладём её прямо в код (с обходом) и загружаем её адрес.
        text = value + b"\0"
        addr = self.do_strings.get(text)
        inline = b""
        if addr is None:
            n = len(text)
            if n <= 127:
                jump = bytes([0xEB, n])
            else:
                jump = b"\xe9" + n.to_bytes(2 if self.bits == 16 else 4, "little")
            addr = here + len(jump)
            self.do_strings[text] = addr
            inline = jump + text
        ctx.addr = here + len(inline)
        code = x86.encode("mov", [dest, x86.Imm(addr, True)], [], ctx)
        self.emit(inline + code)

    # ------------------------------------------------ листинг

    def make_listing(self):
        grouped = {}
        for loc, addr, data in self.records:
            entry = grouped.get(id(loc))
            if entry is None:
                grouped[id(loc)] = [addr, bytearray(data)]
            else:
                entry[1] += data
        rows = []
        width = 8
        multi_file = len({loc.file for loc in self.lines}) > 1
        for loc in self.lines:
            entry = grouped.get(id(loc))
            addr_s, hex_s = "", ""
            if entry is not None:
                addr_s = f"{entry[0]:08X}"
                data = entry[1]
                hex_s = data[:width].hex().upper()
                if len(data) > width:
                    hex_s += f"+{len(data) - width}"
            prefix = f"{os.path.basename(loc.file)}:" if multi_file else ""
            rows.append(f"{prefix}{loc.line:<5} {addr_s:<8}  {hex_s:<20}  {loc.text.rstrip()}")
        if self.cur:
            rows.append("")
            rows.append("Символы:")
            for name, value in sorted(self.cur.items(), key=lambda kv: (kv[1], kv[0])):
                if name.startswith("@"):
                    continue                       # служебные метки if/while/for
                rows.append(f"  {value & 0xFFFFFFFF:08X}  {name}")
        return "\n".join(rows) + "\n"


# ---------------------------------------------------------------- API

def _assemble(parser):
    parser.finish()
    if parser.errors:
        raise GasemErrors(parser.errors)
    result = Assembler(parser).assemble()
    result.warnings = parser.warnings
    return result


def compile_source(text, filename="<источник>", base_dir=None):
    """Скомпилировать текст программы. Возвращает CompileResult.

    При ошибках выбрасывает GasemErrors.
    """
    parser = Parser()
    parser.parse_text(text, filename, base_dir)
    return _assemble(parser)


def compile_file(path):
    parser = Parser()
    try:
        parser.parse_file(path)
    except GasemError as e:
        raise GasemErrors([e])
    return _assemble(parser)
