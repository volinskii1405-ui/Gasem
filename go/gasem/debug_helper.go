package gasem

// Код на Python, который выполняется внутри GDB (тот же, что в Python-версии Gasem).
// __SYMBOLS__ и __LINES__ заменяются таблицами меток и строк программы.

const gdbHelper = `
import bisect
import gdb

SYMBOLS = __SYMBOLS__
LINES = __LINES__          # (адрес, размер, режим, файл, строка, текст), по возрастанию
STARTS = [l[0] for l in LINES]
STATE = {"bits": None}


def find(pc):
    i = bisect.bisect_right(STARTS, pc) - 1
    if i >= 0 and LINES[i][0] <= pc < LINES[i][0] + LINES[i][1]:
        return LINES[i]
    return None


def location():
    pc = int(gdb.parse_and_eval("$pc")) & 0xFFFFFFFF
    line = find(pc)
    if line is None:                              # реальный режим: адрес = cs*16 + ip
        cs = int(gdb.parse_and_eval("$cs")) & 0xFFFF
        line = find((cs << 4) + (pc & 0xFFFF))
    return pc, line


def set_mode(bits):
    if bits != STATE["bits"]:
        STATE["bits"] = bits
        gdb.execute("set architecture " + ("i8086" if bits == 16 else "i386"), to_string=True)


def show(_event=None):
    pc, line = location()
    if line is None:
        print("=> %#x (вне программы)" % pc)
        return
    set_mode(line[2])
    print("=> %s:%d    %s" % (line[3], line[4], line[5].strip()))
    names = ["eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"]
    regs = "  ".join("%s=%08x" % (n, int(gdb.parse_and_eval("$" + n)) & 0xFFFFFFFF) for n in names)
    print("   " + regs)


class GBreak(gdb.Command):
    """gbreak метка — точка останова на метке программы Gasem."""

    def __init__(self):
        super().__init__("gbreak", gdb.COMMAND_BREAKPOINTS)

    def invoke(self, arg, from_tty):
        name = arg.strip()
        if name not in SYMBOLS:
            import difflib
            close = difflib.get_close_matches(name, list(SYMBOLS), n=1)
            hint = (" — может быть, '%s'?" % close[0]) if close else ""
            raise gdb.GdbError("неизвестная метка '%s'%s" % (name, hint))
        gdb.execute("break *%#x" % SYMBOLS[name])


class GStep(gdb.Command):
    """gstep — выполнить одну строку исходника Gasem."""

    def __init__(self):
        super().__init__("gstep", gdb.COMMAND_RUNNING)

    def invoke(self, arg, from_tty):
        _, start = location()
        for _ in range(100000):
            gdb.execute("stepi", to_string=True)
            _, now = location()
            if now is None or start is None or now[3:5] != start[3:5]:
                break
        show()


class GWhere(gdb.Command):
    """gwhere — текущая строка исходника Gasem."""

    def __init__(self):
        super().__init__("gwhere", gdb.COMMAND_STATUS)

    def invoke(self, arg, from_tty):
        show()


GBreak()
GStep()
GWhere()
gdb.events.stop.connect(show)
`
