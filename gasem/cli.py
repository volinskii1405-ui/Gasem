"""Командная строка: gasem программа.gsm [-o файл.bin] [-l листинг.lst] [--run]"""

import argparse
import os
import shutil
import subprocess
import sys

from . import __version__
from .assembler import compile_file
from .errors import GasemErrors


def _setup_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _find_qemu():
    for name in ("qemu-system-i386", "qemu-system-x86_64"):
        path = shutil.which(name)
        if path:
            return path
    return None


def main(argv=None):
    _setup_streams()
    ap = argparse.ArgumentParser(
        prog="gasem",
        description="Компилятор языка Gasem: исходник .gsm → двоичный файл с машинным кодом x86.",
    )
    ap.add_argument("input", help="исходный файл (.gsm)")
    ap.add_argument("-o", "--output", help="выходной файл (по умолчанию — имя исходника с расширением .bin)")
    ap.add_argument("-l", "--listing", help="записать листинг (адреса, байты, исходные строки)")
    ap.add_argument("--run", action="store_true", help="после компиляции запустить результат в QEMU")
    ap.add_argument("-q", "--quiet", action="store_true", help="не печатать сообщение об успехе")
    ap.add_argument("-V", "--version", action="version", version=f"gasem {__version__}")
    args = ap.parse_args(argv)

    try:
        result = compile_file(args.input)
    except GasemErrors as e:
        for err in e.errors:
            print(err.format(), file=sys.stderr)
        n = len(e.errors)
        print(f"gasem: компиляция не удалась ({n} {_plural(n, 'ошибка', 'ошибки', 'ошибок')})", file=sys.stderr)
        return 1

    output = args.output or os.path.splitext(args.input)[0] + ".bin"
    try:
        with open(output, "wb") as f:
            f.write(result.code)
        if args.listing:
            with open(args.listing, "w", encoding="utf-8") as f:
                f.write(result.listing)
    except OSError as e:
        print(f"gasem: не удалось записать '{e.filename}': {e.strerror}", file=sys.stderr)
        return 1

    if not args.quiet:
        n = len(result.code)
        print(f"gasem: {args.input} -> {output} ({n} {_plural(n, 'байт', 'байта', 'байт')})")

    if args.run:
        qemu = _find_qemu()
        if qemu is None:
            print("gasem: QEMU не найден (нужен qemu-system-i386 или qemu-system-x86_64)", file=sys.stderr)
            return 1
        return subprocess.call([qemu, "-drive", f"format=raw,file={output}"])
    return 0


def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
