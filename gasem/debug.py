"""gasem debug — запуск программы в QEMU под отладчиком GDB.

QEMU стартует остановленным и ждёт GDB. GDB получает от Gasem таблицу меток
и строк исходника, поэтому точки останова ставятся по именам меток, а при
каждой остановке печатается строка .gsm, на которой стоит процессор.

Команды, которые добавляются в GDB:
    gbreak метка     точка останова на метке
    gstep            выполнить одну строку исходника (даже если в ней несколько команд)
    gwhere           показать текущую строку исходника
"""

import os
import shutil
import socket
import subprocess
import tempfile

GDB_HELPER = r'''
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
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_qemu():
    for name in ("qemu-system-i386", "qemu-system-x86_64"):
        path = shutil.which(name)
        if path:
            return path
    return None


def gdb_script(result, port, breaks, batch):
    symbols = {k: v for k, v in result.symbols.items() if not k.startswith("@")}
    lines = sorted((addr, size, bits, os.path.basename(loc.file), loc.line, loc.text)
                   for addr, size, bits, loc in result.lines)
    helper = (GDB_HELPER.replace("__SYMBOLS__", repr(symbols))
              .replace("__LINES__", repr(lines)))
    first_bits = lines[0][2] if lines else 16
    out = [
        "set pagination off",
        "set confirm off",
        "set disassembly-flavor intel",
        "set architecture " + ("i8086" if first_bits == 16 else "i386"),
        f"target remote localhost:{port}",
        "python",
        helper,
        "end",
    ]
    out += [f"gbreak {name}" for name in breaks]
    if batch:
        out += ["continue", "x/3i $pc", "kill"]
    else:
        out += ['echo \\nGasem: gbreak <метка>, gstep, gwhere; continue — продолжить, q — выход\\n']
    return "\n".join(out) + "\n"


def debug(result, image, breaks, batch=False, qemu_args=(), timeout=30):
    """Запустить образ image в QEMU под GDB. Возвращает код выхода GDB.
    В режиме batch GDB ждёт точку останова не дольше timeout секунд."""
    qemu = find_qemu()
    gdb_path = shutil.which("gdb")
    if qemu is None:
        raise RuntimeError("QEMU не найден (нужен qemu-system-i386)")
    if gdb_path is None:
        raise RuntimeError("GDB не найден (установите gdb)")
    for name in breaks:
        if name not in result.symbols:
            raise RuntimeError(f"неизвестная метка '{name}'")
    port = free_port()
    cmd = [qemu, "-drive", f"format=raw,file={image}", "-S", "-gdb", f"tcp:127.0.0.1:{port}",
           "-no-reboot", *qemu_args]
    if batch:
        cmd += ["-display", "none"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "gasem.gdb")
            with open(script, "w", encoding="utf-8") as f:
                f.write(gdb_script(result, port, breaks, batch))
            args = [gdb_path, "-q", "-x", script]
            if not batch:
                return subprocess.call(args)
            args.insert(2, "-batch")
            try:
                return subprocess.call(args, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"за {timeout} с программа не дошла до точки останова")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
