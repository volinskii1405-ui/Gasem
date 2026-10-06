"""Кодирование команд x86 в машинный код (режимы 16 и 32 бит)."""

from .errors import GasemError


# ---------------------------------------------------------------- регистры

class Reg:
    __slots__ = ("name", "size", "num", "kind")

    def __init__(self, name, size, num, kind):
        self.name = name
        self.size = size
        self.num = num
        self.kind = kind   # gpr / seg / cr / dr

    def __repr__(self):
        return self.name


REGISTERS = {}


def _add_regs(names, size, kind, nums=None):
    for i, name in enumerate(names):
        REGISTERS[name] = Reg(name, size, nums[i] if nums else i, kind)


_add_regs("al cl dl bl ah ch dh bh".split(), 8, "gpr")
_add_regs("ax cx dx bx sp bp si di".split(), 16, "gpr")
_add_regs("eax ecx edx ebx esp ebp esi edi".split(), 32, "gpr")
_add_regs("es cs ss ds fs gs".split(), 16, "seg")
_add_regs(["cr0", "cr2", "cr3", "cr4"], 32, "cr", [0, 2, 3, 4])
_add_regs([f"dr{i}" for i in range(8)], 32, "dr")

SEGMENTS = ("es", "cs", "ss", "ds", "fs", "gs")
SEG_PREFIX = {"es": 0x26, "cs": 0x2E, "ss": 0x36, "ds": 0x3E, "fs": 0x64, "gs": 0x65}
PREFIXES = {"lock": 0xF0, "rep": 0xF3, "repe": 0xF3, "repz": 0xF3, "repne": 0xF2, "repnz": 0xF2}


# ---------------------------------------------------------------- операнды

class Mem:
    __slots__ = ("size", "seg", "base", "index", "scale", "disp", "known", "has_disp", "jump")

    def __init__(self, size, seg, base, index, scale, disp, known=True, has_disp=True, jump=None):
        self.size = size
        self.seg = seg
        self.base = base
        self.index = index
        self.scale = scale
        self.disp = disp
        self.known = known
        self.has_disp = has_disp
        self.jump = jump

    @property
    def direct(self):
        """Адрес без регистров: [0x1234], [msg]."""
        return self.base is None and self.index is None


class Imm:
    __slots__ = ("value", "known", "size", "jump")

    def __init__(self, value, known=True, size=None, jump=None):
        self.value = value
        self.known = known
        self.size = size
        self.jump = jump


class Far:
    __slots__ = ("seg", "off", "known", "size")

    def __init__(self, seg, off, known=True, size=None):
        self.seg = seg
        self.off = off
        self.known = known
        self.size = size


class A20Op:
    pass


A20 = A20Op()


# ---------------------------------------------------------------- таблицы

CC = {
    "o": 0, "no": 1, "b": 2, "c": 2, "nae": 2, "nb": 3, "nc": 3, "ae": 3,
    "e": 4, "z": 4, "ne": 5, "nz": 5, "be": 6, "na": 6, "nbe": 7, "a": 7,
    "s": 8, "ns": 9, "p": 10, "pe": 10, "np": 11, "po": 11,
    "l": 12, "nge": 12, "nl": 13, "ge": 13, "le": 14, "ng": 14, "nle": 15, "g": 15,
}

# Команды без операндов.
SIMPLE = {
    "nop": b"\x90", "hlt": b"\xf4", "cli": b"\xfa", "sti": b"\xfb", "cld": b"\xfc",
    "std": b"\xfd", "clc": b"\xf8", "stc": b"\xf9", "cmc": b"\xf5", "lahf": b"\x9f",
    "sahf": b"\x9e", "int3": b"\xcc", "into": b"\xce", "leave": b"\xc9",
    "xlatb": b"\xd7", "wait": b"\x9b", "daa": b"\x27", "das": b"\x2f", "aaa": b"\x37",
    "aas": b"\x3f", "cpuid": b"\x0f\xa2", "rdtsc": b"\x0f\x31", "rdmsr": b"\x0f\x32",
    "wrmsr": b"\x0f\x30", "wbinvd": b"\x0f\x09", "invd": b"\x0f\x08", "clts": b"\x0f\x06",
    "ud2": b"\x0f\x0b", "pause": b"\xf3\x90",
}

# Команды без операндов, у которых важен размер операнда (префикс 0x66).
SIZED = {
    "pusha": (0x60, None), "popa": (0x61, None), "pushf": (0x9C, None), "popf": (0x9D, None),
    "iret": (0xCF, None),
    "pushad": (0x60, 32), "popad": (0x61, 32), "pushfd": (0x9C, 32), "popfd": (0x9D, 32),
    "iretd": (0xCF, 32),
    "cbw": (0x98, 16), "cwde": (0x98, 32), "cwd": (0x99, 16), "cdq": (0x99, 32),
    "movsb": (0xA4, 8), "movsw": (0xA5, 16), "movsd": (0xA5, 32),
    "cmpsb": (0xA6, 8), "cmpsw": (0xA7, 16), "cmpsd": (0xA7, 32),
    "stosb": (0xAA, 8), "stosw": (0xAB, 16), "stosd": (0xAB, 32),
    "lodsb": (0xAC, 8), "lodsw": (0xAD, 16), "lodsd": (0xAD, 32),
    "scasb": (0xAE, 8), "scasw": (0xAF, 16), "scasd": (0xAF, 32),
    "insb": (0x6C, 8), "insw": (0x6D, 16), "insd": (0x6D, 32),
    "outsb": (0x6E, 8), "outsw": (0x6F, 16), "outsd": (0x6F, 32),
}

ALU = {"add": 0, "or": 1, "adc": 2, "sbb": 3, "and": 4, "sub": 5, "xor": 6, "cmp": 7}
SHIFT = {"rol": 0, "ror": 1, "rcl": 2, "rcr": 3, "shl": 4, "sal": 4, "shr": 5, "sar": 7}
GROUP3 = {"not": 2, "neg": 3, "mul": 4, "div": 6, "idiv": 7}
BIT_TEST = {"bt": (0xA3, 4), "bts": (0xAB, 5), "btr": (0xB3, 6), "btc": (0xBB, 7)}
SEG_LOAD = {"lds": b"\xc5", "les": b"\xc4", "lss": b"\x0f\xb2", "lfs": b"\x0f\xb4", "lgs": b"\x0f\xb5"}
DESCRIPTOR = {
    # имя: (опкод, /n, можно ли 16-битный регистр)
    "sgdt": (b"\x0f\x01", 0, False), "sidt": (b"\x0f\x01", 1, False),
    "lgdt": (b"\x0f\x01", 2, False), "lidt": (b"\x0f\x01", 3, False),
    "smsw": (b"\x0f\x01", 4, True), "lmsw": (b"\x0f\x01", 6, True),
    "invlpg": (b"\x0f\x01", 7, False),
    "sldt": (b"\x0f\x00", 0, True), "str": (b"\x0f\x00", 1, True),
    "lldt": (b"\x0f\x00", 2, True), "ltr": (b"\x0f\x00", 3, True),
}
SEG_PUSH = {"es": b"\x06", "cs": b"\x0e", "ss": b"\x16", "ds": b"\x1e", "fs": b"\x0f\xa0", "gs": b"\x0f\xa8"}
SEG_POP = {"es": b"\x07", "ss": b"\x17", "ds": b"\x1f", "fs": b"\x0f\xa1", "gs": b"\x0f\xa9"}

OTHER = {
    "mov", "test", "inc", "dec", "imul", "push", "pop", "xchg", "lea", "movzx", "movsx",
    "jmp", "call", "ret", "retf", "loop", "loope", "loopne", "jcxz", "jecxz", "int",
    "in", "out", "enter", "bsf", "bsr", "bswap",
}

# Псевдонимы Gasem и синонимы x86.
ALIASES = {
    "nxtb": "lodsb", "nxtw": "lodsw", "nxtd": "lodsd",
    "chk": "test",
    "retn": "ret", "loopz": "loope", "loopnz": "loopne", "xlat": "xlatb",
}

MNEMONICS = (set(SIMPLE) | set(SIZED) | set(ALU) | set(SHIFT) | set(GROUP3) | set(BIT_TEST)
             | set(SEG_LOAD) | set(DESCRIPTOR) | OTHER
             | {"j" + cc for cc in CC} | {"set" + cc for cc in CC} | {"cmov" + cc for cc in CC})


def canonical(word):
    """Имя команды → каноническое имя x86 (или None, если команды нет).

    Gasem-псевдонимы: nxtb = lodsb, chk = test, jf<усл> = j<усл> (jfnz = jnz).
    """
    w = word.lower()
    w = ALIASES.get(w, w)
    if w.startswith("jf") and w[2:] in CC:
        w = "j" + w[2:]
    return w if w in MNEMONICS else None


# ---------------------------------------------------------------- кодировщик

_RM16 = {
    frozenset(["bx", "si"]): 0, frozenset(["bx", "di"]): 1,
    frozenset(["bp", "si"]): 2, frozenset(["bp", "di"]): 3,
    frozenset(["si"]): 4, frozenset(["di"]): 5, frozenset(["bp"]): 6, frozenset(["bx"]): 7,
}
_SCALE = {1: 0, 2: 1, 4: 2, 8: 3}


def _err(msg):
    raise GasemError(msg)


def _gpr(o):
    return isinstance(o, Reg) and o.kind == "gpr"


def _rm(o):
    return _gpr(o) or isinstance(o, Mem)


def _acc(o):
    return _gpr(o) and o.num == 0


def _pack(value, nbytes):
    return (value & ((1 << (8 * nbytes)) - 1)).to_bytes(nbytes, "little")


def fits_s8(value, size):
    """Помещается ли значение в знаковый байт (с учётом размера операнда)."""
    m = value & ((1 << size) - 1)
    return m < 0x80 or m >= (1 << size) - 0x80


def encode(mnemonic, operands, prefixes, ctx):
    """Закодировать одну команду. ctx — контекст прохода ассемблера."""
    return Encoder(ctx, prefixes, mnemonic).encode(operands)


class Encoder:
    def __init__(self, ctx, prefixes, mnemonic):
        self.ctx = ctx
        self.bits = ctx.bits
        self.pre = bytes(prefixes)
        self.mn = mnemonic

    # ------------------------------------------------ вспомогательное

    def bad(self):
        _err(f"недопустимые операнды для {self.mn}")

    def nops(self, ops, *counts):
        if len(ops) not in counts:
            want = " или ".join(str(c) for c in counts)
            _err(f"{self.mn}: неверное число операндов ({len(ops)}), нужно {want}")

    def check_range(self, value, bits, signed=True):
        if not self.ctx.final:
            return
        lo = -(1 << (bits - 1)) if signed else 0
        hi = (1 << bits) - 1
        if not lo <= value <= hi:
            _err(f"значение {value} не помещается в {bits} бит")

    def imm(self, o, size, signed=True):
        if not isinstance(o, Imm):
            self.bad()
        self.check_range(o.value, size, signed)
        return _pack(o.value, size // 8)

    def imm_s8(self, o, size):
        self.check_range(o.value, size)
        if self.ctx.final and not fits_s8(o.value, size):
            _err(f"значение {o.value} не помещается в знаковый байт")
        return bytes([o.value & 0xFF])

    def short_imm(self, o, size):
        """Можно ли закодировать число одним байтом со знаковым расширением."""
        if o.size == 8:
            return True
        need = 0 if (not o.known or fits_s8(o.value, size)) else 1
        return self.ctx.level("imm", need) == 0

    def op_size(self, *ops):
        sizes = {o.size for o in ops if (_gpr(o) or isinstance(o, Mem)) and o.size}
        if len(sizes) > 1:
            _err(f"{self.mn}: размеры операндов не совпадают")
        if not sizes:
            sizes = {o.size for o in ops if isinstance(o, Imm) and o.size}
        if not sizes:
            _err(f"{self.mn}: не удаётся определить размер операнда — укажите byte, word или dword "
                 f"(например: {self.mn} word [x] - 1)")
        size = sizes.pop()
        if size not in (8, 16, 32):
            _err(f"{self.mn}: размер {size} бит здесь недопустим")
        return size

    def check_mem_size(self, m, size):
        if isinstance(m, Mem) and m.size is not None and m.size != size:
            _err(f"{self.mn}: размер памяти ({m.size} бит) не совпадает с регистром ({size} бит)")

    # ------------------------------------------------ адресация

    def disp_level(self, m, asize, force8):
        need = 0
        if m.has_disp and m.known:
            d = m.disp
            if asize == 16:
                d &= 0xFFFF
                if d >= 0x8000:
                    d -= 0x10000
            if d != 0:
                need = 1 if -128 <= d <= 127 else 2
        if force8:
            need = max(need, 1)
        return self.ctx.level("disp", need)

    def disp_bytes(self, m, lvl, asize):
        if lvl == 0:
            return b""
        if lvl == 1:
            return bytes([m.disp & 0xFF])
        self.check_range(m.disp, asize)
        return _pack(m.disp, asize // 8)

    def mem_encode(self, reg, m):
        """→ (размер адреса, байты ModRM [+ SIB] [+ смещение])."""
        regs = [r for r in (m.base, m.index) if r is not None]
        for r in regs:
            if r.kind != "gpr" or r.size == 8:
                _err(f"регистр {r.name} нельзя использовать в адресе")
        sizes = {r.size for r in regs}
        if len(sizes) > 1:
            _err("в адресе нельзя смешивать 16- и 32-битные регистры")
        asize = sizes.pop() if sizes else self.bits

        if asize == 16:
            if not regs:
                self.check_range(m.disp, 16)
                return 16, bytes([(reg << 3) | 6]) + _pack(m.disp, 2)
            if m.index is not None and m.scale != 1:
                _err("масштаб (*2, *4, *8) недоступен в 16-битной адресации")
            names = frozenset(r.name for r in regs)
            if len(names) != len(regs) or names not in _RM16:
                written = "+".join(r.name for r in regs)
                _err(f"недопустимый 16-битный адрес [{written}]; можно: bx, bp, si, di, "
                     f"bx+si, bx+di, bp+si, bp+di (плюс смещение)")
            rm = _RM16[names]
            lvl = self.disp_level(m, 16, force8=(rm == 6))
            return 16, bytes([(lvl << 6) | (reg << 3) | rm]) + self.disp_bytes(m, lvl, 16)

        base, index, scale = m.base, m.index, m.scale
        if index is not None and base is None and scale == 2:
            base, scale = index, 1          # [eax*2] → [eax+eax]: короче
        if base is None and index is None:
            self.check_range(m.disp, 32)
            return 32, bytes([(reg << 3) | 5]) + _pack(m.disp, 4)
        if index is None:
            lvl = self.disp_level(m, 32, force8=(base.num == 5))
            if base.num == 4:   # esp требует SIB
                return 32, bytes([(lvl << 6) | (reg << 3) | 4, 0x24]) + self.disp_bytes(m, lvl, 32)
            return 32, bytes([(lvl << 6) | (reg << 3) | base.num]) + self.disp_bytes(m, lvl, 32)
        if index.num == 4:
            _err("esp нельзя использовать как индексный регистр")
        if scale not in _SCALE:
            _err("масштаб должен быть 1, 2, 4 или 8")
        sib_hi = (_SCALE[scale] << 6) | (index.num << 3)
        if base is None:
            self.check_range(m.disp, 32)
            return 32, bytes([(reg << 3) | 4, sib_hi | 5]) + _pack(m.disp, 4)
        lvl = self.disp_level(m, 32, force8=(base.num == 5))
        return 32, bytes([(lvl << 6) | (reg << 3) | 4, sib_hi | base.num]) + self.disp_bytes(m, lvl, 32)

    def build(self, opcode, size=None, rm=None, reg=0, imm=b""):
        """Собрать команду: префиксы + опкод + ModRM/SIB/смещение + число."""
        out = bytearray(self.pre)
        body = b""
        asize = None
        if isinstance(rm, Mem):
            asize, body = self.mem_encode(reg, rm)
            if rm.seg is not None:
                out.append(SEG_PREFIX[rm.seg.name])
        elif isinstance(rm, Reg):
            body = bytes([0xC0 | (reg << 3) | rm.num])
        if size in (16, 32) and size != self.bits:
            out.append(0x66)
        if asize is not None and asize != self.bits:
            out.append(0x67)
        return bytes(out) + opcode + body + imm

    def moffs(self, opcode, size, m):
        """mov al/ax/eax <-> [адрес] — короткая форма без ModRM."""
        out = bytearray(self.pre)
        if m.seg is not None:
            out.append(SEG_PREFIX[m.seg.name])
        if size in (16, 32) and size != self.bits:
            out.append(0x66)
        self.check_range(m.disp, self.bits)
        return bytes(out) + bytes([opcode]) + _pack(m.disp, self.bits // 8)

    # ------------------------------------------------ переходы

    def rel_jump(self, target, short_op, near_op, hint):
        if not isinstance(target, Imm):
            self.bad()
        a = self.ctx.addr
        nd = 2 if self.bits == 16 else 4
        rel_s = None
        if short_op is not None:
            rel_s = target.value - (a + len(self.pre) + len(short_op) + 1)
        if near_op is None or hint == "short":
            lvl = 0
        elif short_op is None or hint == "near":
            lvl = 1
        else:
            need = 0 if (not target.known or -128 <= rel_s <= 127) else 1
            lvl = self.ctx.level("jmp", need)
        if lvl == 0:
            if self.ctx.final and not -128 <= rel_s <= 127:
                _err(f"цель слишком далеко для короткого перехода ({rel_s:+d} байт, "
                     f"допустимо от -128 до +127)")
            return self.pre + short_op + bytes([rel_s & 0xFF])
        rel = target.value - (a + len(self.pre) + len(near_op) + nd)
        return self.pre + near_op + _pack(rel, nd)

    def far_ptr(self, opcode, o):
        size = o.size or self.bits
        if size not in (16, 32):
            _err(f"{self.mn}: смещение дальнего адреса может быть word или dword")
        self.check_range(o.seg, 16, signed=False)
        self.check_range(o.off, size, signed=False)
        return self.build(bytes([opcode]), size, imm=_pack(o.off, size // 8) + _pack(o.seg, 2))

    def indirect(self, o, near_n, far_n):
        if isinstance(o, Mem) and o.jump == "far":
            return self.build(b"\xff", None, rm=o, reg=far_n)
        if _gpr(o) or isinstance(o, Mem):
            size = o.size or self.bits
            if size not in (16, 32):
                self.bad()
            return self.build(b"\xff", size, rm=o, reg=near_n)
        self.bad()

    # ------------------------------------------------ диспетчер

    def encode(self, ops):
        mn = self.mn
        if mn in SIMPLE:
            self.nops(ops, 0)
            return self.pre + SIMPLE[mn]
        if mn in SIZED:
            self.nops(ops, 0)
            opcode, size = SIZED[mn]
            return self.build(bytes([opcode]), size)
        if mn in ALU:
            return self.op_alu(ops, ALU[mn])
        if mn in SHIFT:
            return self.op_shift(ops, SHIFT[mn])
        if mn in GROUP3:
            return self.op_group3(ops, GROUP3[mn])
        if mn in BIT_TEST:
            return self.op_bt(ops, *BIT_TEST[mn])
        if mn in SEG_LOAD:
            return self.op_segload(ops, SEG_LOAD[mn])
        if mn in DESCRIPTOR:
            return self.op_descriptor(ops, *DESCRIPTOR[mn])
        if mn.startswith("set") and mn[3:] in CC:
            return self.op_setcc(ops, CC[mn[3:]])
        if mn.startswith("cmov") and mn[4:] in CC:
            return self.op_cmovcc(ops, CC[mn[4:]])
        if mn.startswith("j") and mn[1:] in CC:
            self.nops(ops, 1)
            cc = CC[mn[1:]]
            hint = ops[0].jump if isinstance(ops[0], Imm) else None
            return self.rel_jump(ops[0], bytes([0x70 + cc]), bytes([0x0F, 0x80 + cc]), hint)
        return getattr(self, "op_" + mn)(ops)

    # ------------------------------------------------ пересылка

    def op_mov(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if d is A20:
            return self.a20(s)
        if s is A20:
            _err("линию a20 можно только переключать: mov a20 - 1 или mov a20 - 0")

        # управляющие и отладочные регистры
        if isinstance(d, Reg) and d.kind in ("cr", "dr"):
            if not (_gpr(s) and s.size == 32):
                _err(f"mov {d.name} - ...: источник должен быть 32-битным регистром (например eax)")
            return self.build(b"\x0f\x22" if d.kind == "cr" else b"\x0f\x23", None, rm=s, reg=d.num)
        if isinstance(s, Reg) and s.kind in ("cr", "dr"):
            if not (_gpr(d) and d.size == 32):
                _err(f"mov ... - {s.name}: приёмник должен быть 32-битным регистром (например eax)")
            return self.build(b"\x0f\x20" if s.kind == "cr" else b"\x0f\x21", None, rm=d, reg=s.num)

        # сегментные регистры
        if isinstance(d, Reg) and d.kind == "seg":
            if d.name == "cs":
                _err("cs нельзя загрузить через mov — используйте дальний переход jmp сегмент:смещение")
            if _gpr(s) and s.size != 8:
                return self.build(b"\x8e", None, rm=s, reg=d.num)
            if isinstance(s, Mem) and s.size in (None, 16):
                return self.build(b"\x8e", None, rm=s, reg=d.num)
            if isinstance(s, Imm):
                _err(f"число нельзя загрузить прямо в {d.name}: сначала mov ax - число, затем mov {d.name} - ax")
            self.bad()
        if isinstance(s, Reg) and s.kind == "seg":
            if _gpr(d) and d.size != 8:
                return self.build(b"\x8c", d.size, rm=d, reg=s.num)
            if isinstance(d, Mem) and d.size in (None, 16):
                return self.build(b"\x8c", None, rm=d, reg=s.num)
            self.bad()

        w = None
        if _gpr(d):
            size = d.size
            w = 0 if size == 8 else 1
            if _gpr(s):
                if s.size != size:
                    _err(f"mov: размеры регистров {d.name} и {s.name} не совпадают")
                return self.build(bytes([0x88 + w]), size, rm=d, reg=s.num)
            if isinstance(s, Mem):
                self.check_mem_size(s, size)
                if d.num == 0 and s.direct:
                    return self.moffs(0xA0 + w, size, s)
                return self.build(bytes([0x8A + w]), size, rm=s, reg=d.num)
            if isinstance(s, Imm):
                return self.build(bytes([(0xB0 if w == 0 else 0xB8) + d.num]), size,
                                  imm=self.imm(s, size))
            self.bad()
        if isinstance(d, Mem):
            if _gpr(s):
                size = s.size
                w = 0 if size == 8 else 1
                self.check_mem_size(d, size)
                if s.num == 0 and d.direct:
                    return self.moffs(0xA2 + w, size, d)
                return self.build(bytes([0x88 + w]), size, rm=d, reg=s.num)
            if isinstance(s, Imm):
                size = self.op_size(d, s)
                w = 0 if size == 8 else 1
                return self.build(bytes([0xC6 + w]), size, rm=d, reg=0, imm=self.imm(s, size))
            if isinstance(s, Mem):
                _err("mov: нельзя переслать память в память — используйте регистр")
        self.bad()

    def a20(self, s):
        if not isinstance(s, Imm) or s.value not in (0, 1):
            _err("a20 принимает только 0 (выключить) или 1 (включить)")
        o32 = b"\x66" if self.bits == 32 else b""
        push, pop = o32 + b"\x50", o32 + b"\x58"     # сохраняем ax/eax
        if s.value:
            # in al, 0x92 / or al, 2 / and al, 0xFE / out 0x92, al
            body = b"\xe4\x92\x0c\x02\x24\xfe\xe6\x92"
        else:
            # in al, 0x92 / and al, 0xFC / out 0x92, al
            body = b"\xe4\x92\x24\xfc\xe6\x92"
        return self.pre + push + body + pop

    def op_xchg(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if _gpr(d) and _gpr(s):
            if d.size != s.size:
                _err("xchg: размеры регистров не совпадают")
            if d.size != 8 and (d.num == 0 or s.num == 0):
                other = s if d.num == 0 else d
                return self.build(bytes([0x90 + other.num]), d.size)
            return self.build(b"\x86" if d.size == 8 else b"\x87", d.size, rm=s, reg=d.num)
        if _gpr(d) and isinstance(s, Mem):
            d, s = s, d
        if isinstance(d, Mem) and _gpr(s):
            self.check_mem_size(d, s.size)
            return self.build(b"\x86" if s.size == 8 else b"\x87", s.size, rm=d, reg=s.num)
        self.bad()

    def op_lea(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if not (_gpr(d) and d.size != 8 and isinstance(s, Mem)):
            _err("lea: нужно lea регистр16/32 - [адрес]")
        return self.build(b"\x8d", d.size, rm=s, reg=d.num)

    def op_segload(self, ops, opcode):
        self.nops(ops, 2)
        d, s = ops
        if not (_gpr(d) and d.size != 8 and isinstance(s, Mem)):
            self.bad()
        return self.build(opcode, d.size, rm=s, reg=d.num)

    def movx(self, ops, op8, op16):
        self.nops(ops, 2)
        d, s = ops
        if not (_gpr(d) and d.size != 8 and _rm(s)):
            self.bad()
        if s.size is None:
            _err(f"{self.mn}: укажите размер источника: byte или word")
        if s.size == 8:
            return self.build(op8, d.size, rm=s, reg=d.num)
        if s.size == 16 and d.size == 32:
            return self.build(op16, d.size, rm=s, reg=d.num)
        self.bad()

    def op_movzx(self, ops):
        return self.movx(ops, b"\x0f\xb6", b"\x0f\xb7")

    def op_movsx(self, ops):
        return self.movx(ops, b"\x0f\xbe", b"\x0f\xbf")

    def op_push(self, ops):
        self.nops(ops, 1)
        o = ops[0]
        if isinstance(o, Reg):
            if o.kind == "seg":
                return self.pre + SEG_PUSH[o.name]
            if o.kind == "gpr" and o.size != 8:
                return self.build(bytes([0x50 + o.num]), o.size)
            self.bad()
        if isinstance(o, Imm):
            size = o.size if o.size in (16, 32) else self.bits
            if self.short_imm(o, size):
                return self.build(b"\x6a", size, imm=self.imm_s8(o, size))
            return self.build(b"\x68", size, imm=self.imm(o, size))
        if isinstance(o, Mem):
            if o.size not in (16, 32):
                _err("push: укажите размер памяти: word или dword")
            return self.build(b"\xff", o.size, rm=o, reg=6)
        self.bad()

    def op_pop(self, ops):
        self.nops(ops, 1)
        o = ops[0]
        if isinstance(o, Reg):
            if o.kind == "seg":
                if o.name == "cs":
                    _err("pop cs недопустим")
                return self.pre + SEG_POP[o.name]
            if o.kind == "gpr" and o.size != 8:
                return self.build(bytes([0x58 + o.num]), o.size)
            self.bad()
        if isinstance(o, Mem):
            if o.size not in (16, 32):
                _err("pop: укажите размер памяти: word или dword")
            return self.build(b"\x8f", o.size, rm=o, reg=0)
        self.bad()

    # ------------------------------------------------ арифметика и логика

    def op_alu(self, ops, n):
        self.nops(ops, 2)
        d, s = ops
        if isinstance(s, Imm) and _rm(d):
            size = self.op_size(d, s)
            if size == 8:
                if _acc(d):
                    return self.build(bytes([n * 8 + 4]), 8, imm=self.imm(s, 8))
                return self.build(b"\x80", 8, rm=d, reg=n, imm=self.imm(s, 8))
            if self.short_imm(s, size):
                return self.build(b"\x83", size, rm=d, reg=n, imm=self.imm_s8(s, size))
            if _acc(d):
                return self.build(bytes([n * 8 + 5]), size, imm=self.imm(s, size))
            return self.build(b"\x81", size, rm=d, reg=n, imm=self.imm(s, size))
        if _gpr(s) and _rm(d):
            size = self.op_size(d, s)
            return self.build(bytes([n * 8 + (0 if size == 8 else 1)]), size, rm=d, reg=s.num)
        if _gpr(d) and isinstance(s, Mem):
            size = self.op_size(d, s)
            return self.build(bytes([n * 8 + (2 if size == 8 else 3)]), size, rm=s, reg=d.num)
        if isinstance(d, Mem) and isinstance(s, Mem):
            _err(f"{self.mn}: нельзя использовать два операнда в памяти")
        self.bad()

    def op_test(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if isinstance(d, Imm) and _rm(s):
            d, s = s, d
        if isinstance(s, Imm) and _rm(d):
            size = self.op_size(d, s)
            w = 0 if size == 8 else 1
            if _acc(d):
                return self.build(bytes([0xA8 + w]), size, imm=self.imm(s, size))
            return self.build(bytes([0xF6 + w]), size, rm=d, reg=0, imm=self.imm(s, size))
        if _gpr(d) and isinstance(s, Mem):
            d, s = s, d
        if _gpr(s) and _rm(d):
            size = self.op_size(d, s)
            return self.build(bytes([0x84 + (0 if size == 8 else 1)]), size, rm=d, reg=s.num)
        self.bad()

    def incdec(self, ops, n):
        self.nops(ops, 1)
        o = ops[0]
        if not _rm(o):
            self.bad()
        size = self.op_size(o)
        if _gpr(o) and size != 8:
            return self.build(bytes([0x40 + 8 * n + o.num]), size)
        return self.build(b"\xfe" if size == 8 else b"\xff", size, rm=o, reg=n)

    def op_inc(self, ops):
        return self.incdec(ops, 0)

    def op_dec(self, ops):
        return self.incdec(ops, 1)

    def op_group3(self, ops, n):
        self.nops(ops, 1)
        o = ops[0]
        if not _rm(o):
            self.bad()
        size = self.op_size(o)
        return self.build(b"\xf6" if size == 8 else b"\xf7", size, rm=o, reg=n)

    def op_imul(self, ops):
        self.nops(ops, 1, 2, 3)
        if len(ops) == 1:
            return self.op_group3(ops, 5)
        if len(ops) == 2 and isinstance(ops[1], Imm):
            ops = [ops[0], ops[0], ops[1]]
        d, s = ops[0], ops[1]
        if not (_gpr(d) and d.size != 8 and _rm(s)):
            self.bad()
        size = self.op_size(d, s)
        if len(ops) == 2:
            return self.build(b"\x0f\xaf", size, rm=s, reg=d.num)
        k = ops[2]
        if not isinstance(k, Imm):
            self.bad()
        if self.short_imm(k, size):
            return self.build(b"\x6b", size, rm=s, reg=d.num, imm=self.imm_s8(k, size))
        return self.build(b"\x69", size, rm=s, reg=d.num, imm=self.imm(k, size))

    def op_shift(self, ops, n):
        self.nops(ops, 2)
        d, s = ops
        if not _rm(d):
            self.bad()
        size = self.op_size(d)
        w = 0 if size == 8 else 1
        if isinstance(s, Reg) and s.name == "cl":
            return self.build(bytes([0xD2 + w]), size, rm=d, reg=n)
        if isinstance(s, Imm):
            if s.known and s.value == 1:
                return self.build(bytes([0xD0 + w]), size, rm=d, reg=n)
            self.check_range(s.value, 8, signed=False)
            return self.build(bytes([0xC0 + w]), size, rm=d, reg=n, imm=bytes([s.value & 0xFF]))
        _err(f"{self.mn}: величина сдвига — число или регистр cl")

    def op_bt(self, ops, opcode, n):
        self.nops(ops, 2)
        d, s = ops
        if not _rm(d):
            self.bad()
        if _gpr(s):
            size = self.op_size(d, s)
            if size == 8:
                self.bad()
            return self.build(bytes([0x0F, opcode]), size, rm=d, reg=s.num)
        if isinstance(s, Imm):
            size = self.op_size(d)
            if size == 8:
                self.bad()
            return self.build(b"\x0f\xba", size, rm=d, reg=n, imm=self.imm(s, 8, signed=False))
        self.bad()

    def bitscan(self, ops, opcode):
        self.nops(ops, 2)
        d, s = ops
        if not (_gpr(d) and d.size != 8 and _rm(s)):
            self.bad()
        size = self.op_size(d, s)
        return self.build(opcode, size, rm=s, reg=d.num)

    def op_bsf(self, ops):
        return self.bitscan(ops, b"\x0f\xbc")

    def op_bsr(self, ops):
        return self.bitscan(ops, b"\x0f\xbd")

    def op_bswap(self, ops):
        self.nops(ops, 1)
        o = ops[0]
        if not (_gpr(o) and o.size == 32):
            _err("bswap: нужен 32-битный регистр")
        return self.build(bytes([0x0F, 0xC8 + o.num]), 32)

    def op_setcc(self, ops, cc):
        self.nops(ops, 1)
        o = ops[0]
        if not _rm(o) or (o.size not in (None, 8)):
            _err(f"{self.mn}: нужен 8-битный регистр или byte [память]")
        return self.build(bytes([0x0F, 0x90 + cc]), None, rm=o, reg=0)

    def op_cmovcc(self, ops, cc):
        self.nops(ops, 2)
        d, s = ops
        if not (_gpr(d) and d.size != 8 and _rm(s)):
            self.bad()
        size = self.op_size(d, s)
        return self.build(bytes([0x0F, 0x40 + cc]), size, rm=s, reg=d.num)

    # ------------------------------------------------ переходы и вызовы

    def op_jmp(self, ops):
        self.nops(ops, 1)
        o = ops[0]
        if isinstance(o, Far):
            return self.far_ptr(0xEA, o)
        if isinstance(o, Imm):
            if o.jump == "far":
                _err("дальний переход записывается так: jmp сегмент:смещение")
            return self.rel_jump(o, b"\xeb", b"\xe9", o.jump)
        return self.indirect(o, 4, 5)

    def op_call(self, ops):
        self.nops(ops, 1)
        o = ops[0]
        if isinstance(o, Far):
            return self.far_ptr(0x9A, o)
        if isinstance(o, Imm):
            if o.jump in ("far", "short"):
                _err("call бывает ближним (call метка) или дальним (call сегмент:смещение)")
            return self.rel_jump(o, None, b"\xe8", "near")
        return self.indirect(o, 2, 3)

    def loopish(self, ops, opcode, addr32=False):
        self.nops(ops, 1)
        if not isinstance(ops[0], Imm):
            self.bad()
        op = (b"\x67" if addr32 else b"") + bytes([opcode])
        return self.rel_jump(ops[0], op, None, "short")

    def op_loop(self, ops):
        return self.loopish(ops, 0xE2)

    def op_loope(self, ops):
        return self.loopish(ops, 0xE1)

    def op_loopne(self, ops):
        return self.loopish(ops, 0xE0)

    def op_jcxz(self, ops):
        return self.loopish(ops, 0xE3, addr32=(self.bits == 32))

    def op_jecxz(self, ops):
        return self.loopish(ops, 0xE3, addr32=(self.bits == 16))

    def ret(self, ops, op0, opn):
        self.nops(ops, 0, 1)
        if not ops:
            return self.pre + bytes([op0])
        return self.pre + bytes([opn]) + self.imm(ops[0], 16, signed=False)

    def op_ret(self, ops):
        return self.ret(ops, 0xC3, 0xC2)

    def op_retf(self, ops):
        return self.ret(ops, 0xCB, 0xCA)

    def op_int(self, ops):
        self.nops(ops, 1)
        return self.pre + b"\xcd" + self.imm(ops[0], 8, signed=False)

    def op_enter(self, ops):
        self.nops(ops, 2)
        return self.pre + b"\xc8" + self.imm(ops[0], 16, signed=False) + self.imm(ops[1], 8, signed=False)

    # ------------------------------------------------ порты ввода-вывода

    def op_in(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if not _acc(d):
            _err("in: приёмник должен быть al, ax или eax")
        w = 0 if d.size == 8 else 1
        if isinstance(s, Imm):
            return self.build(bytes([0xE4 + w]), d.size, imm=self.imm(s, 8, signed=False))
        if isinstance(s, Reg) and s.name == "dx":
            return self.build(bytes([0xEC + w]), d.size)
        _err("in: номер порта — число 0..255 или регистр dx")

    def op_out(self, ops):
        self.nops(ops, 2)
        d, s = ops
        if not _acc(s):
            _err("out: источник должен быть al, ax или eax")
        w = 0 if s.size == 8 else 1
        if isinstance(d, Imm):
            return self.build(bytes([0xE6 + w]), s.size, imm=self.imm(d, 8, signed=False))
        if isinstance(d, Reg) and d.name == "dx":
            return self.build(bytes([0xEE + w]), s.size)
        _err("out: номер порта — число 0..255 или регистр dx")

    # ------------------------------------------------ системные

    def op_descriptor(self, ops, opcode, n, allow_reg):
        self.nops(ops, 1)
        o = ops[0]
        if isinstance(o, Mem) or (allow_reg and _gpr(o) and o.size == 16):
            return self.build(opcode, None, rm=o, reg=n)
        _err(f"{self.mn}: нужен операнд в памяти" + (" или 16-битный регистр" if allow_reg else ""))
