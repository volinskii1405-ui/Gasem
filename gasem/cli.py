"""Командная строка Gasem.

    gasem программа.gsm [-o файл.bin] [-l листинг] [-m карта]   собрать
    gasem build программа.gsm ...                                то же самое
    gasem run программа.gsm                                      собрать и запустить в QEMU
    gasem debug программа.gsm -b метка                           запустить под отладчиком GDB
    gasem fmt файлы.gsm [--check] [--diff]                       выровнять оформление
"""

import argparse
import difflib
import os
import subprocess
import sys

from . import __version__
from .assembler import compile_file
from .errors import GasemErrors

COMMANDS = ("build", "run", "debug", "fmt")


def _setup_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ---------------------------------------------------------------- сборка

def _add_build_options(p):
    p.add_argument("input", help="исходный файл (.gsm)")
    p.add_argument("-o", "--output", help="выходной файл (по умолчанию — имя исходника с .bin)")
    p.add_argument("-w", "--no-warnings", action="store_true", help="не показывать предупреждения")
    p.add_argument("-q", "--quiet", action="store_true", help="не печатать сообщение об успехе")


def _compile(args):
    """Собрать и записать файлы. → (результат, путь к образу) или (None, None)."""
    try:
        result = compile_file(args.input)
    except GasemErrors as e:
        for err in e.errors:
            print(err.format(), file=sys.stderr)
        n = len(e.errors)
        print(f"gasem: компиляция не удалась ({n} {_plural(n, 'ошибка', 'ошибки', 'ошибок')})",
              file=sys.stderr)
        return None, None
    if not args.no_warnings:
        for w in result.warnings:
            print(w.format(), file=sys.stderr)
    output = args.output or os.path.splitext(args.input)[0] + ".bin"
    try:
        with open(output, "wb") as f:
            f.write(result.code)
        if getattr(args, "listing", None):
            with open(args.listing, "w", encoding="utf-8") as f:
                f.write(result.listing)
        if getattr(args, "map", None):
            with open(args.map, "w", encoding="utf-8") as f:
                f.write("; адрес     имя\n")
                for name, value in sorted(result.symbols.items(), key=lambda kv: (kv[1], kv[0])):
                    if not name.startswith("@"):
                        f.write(f"{value & 0xFFFFFFFF:08X}  {name}\n")
    except OSError as e:
        print(f"gasem: не удалось записать '{e.filename}': {e.strerror}", file=sys.stderr)
        return None, None
    if not args.quiet:
        n = len(result.code)
        print(f"gasem: {args.input} -> {output} ({n} {_plural(n, 'байт', 'байта', 'байт')})")
    return result, output


def cmd_build(argv):
    p = argparse.ArgumentParser(prog="gasem build", description="Собрать программу Gasem.")
    _add_build_options(p)
    p.add_argument("-l", "--listing", help="записать листинг (адреса, байты, исходные строки)")
    p.add_argument("-m", "--map", help="записать карту символов (адрес и имя каждой метки)")
    p.add_argument("--run", action="store_true", help="после сборки запустить в QEMU")
    args = p.parse_args(argv)
    result, output = _compile(args)
    if result is None:
        return 1
    if args.run:
        return _run_qemu(output)
    return 0


def _check_bootable(result):
    """Предупредить, если BIOS не станет загружать такой образ."""
    code = result.code
    if result.origin == 0x7C00 and (len(code) < 512 or code[510:512] != b"\x55\xaa"):
        print("gasem: предупреждение: образ не загрузочный — в конце первого сектора нет "
              "сигнатуры 0xAA55 (добавьте: && 510-($-$$) b: 0 / w: 0xAA55)", file=sys.stderr)


def _run_qemu(image, extra=()):
    from .debug import find_qemu
    qemu = find_qemu()
    if qemu is None:
        print("gasem: QEMU не найден (нужен qemu-system-i386 или qemu-system-x86_64)", file=sys.stderr)
        return 1
    return subprocess.call([qemu, "-drive", f"format=raw,file={image}", *extra])


def cmd_run(argv):
    p = argparse.ArgumentParser(prog="gasem run", description="Собрать и запустить в QEMU.")
    _add_build_options(p)
    p.add_argument("qemu_args", nargs="*", help="дополнительные параметры QEMU (после --)")
    args = p.parse_args(argv)
    result, output = _compile(args)
    if result is None:
        return 1
    _check_bootable(result)
    return _run_qemu(output, args.qemu_args)


def cmd_debug(argv):
    p = argparse.ArgumentParser(
        prog="gasem debug",
        description="Запустить в QEMU под отладчиком GDB. В GDB доступны команды "
                    "gbreak <метка>, gstep (одна строка исходника) и gwhere.")
    _add_build_options(p)
    p.add_argument("-b", "--break", dest="breaks", action="append", default=[], metavar="МЕТКА",
                   help="точка останова на метке (можно несколько раз)")
    p.add_argument("--batch", action="store_true",
                   help="без окон: дойти до первой точки останова, показать состояние и выйти")
    args = p.parse_args(argv)
    result, output = _compile(args)
    if result is None:
        return 1
    _check_bootable(result)
    from .debug import debug
    try:
        return debug(result, output, args.breaks, batch=args.batch)
    except RuntimeError as e:
        print(f"gasem: {e}", file=sys.stderr)
        return 1


def cmd_fmt(argv):
    p = argparse.ArgumentParser(
        prog="gasem fmt",
        description="Выровнять оформление: отступы в блоках if/while/for и столбец комментариев. "
                    "Меняются только пробелы.")
    p.add_argument("files", nargs="+", help="файлы .gsm")
    p.add_argument("--check", action="store_true", help="только проверить, ничего не менять")
    p.add_argument("--diff", action="store_true", help="показать изменения, ничего не менять")
    args = p.parse_args(argv)
    from .fmt import format_text
    unformatted = 0
    for path in args.files:
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            print(f"gasem: не удалось открыть '{path}': {e.strerror}", file=sys.stderr)
            return 1
        new = format_text(text)
        if new == text:
            continue
        unformatted += 1
        if args.diff:
            sys.stdout.writelines(difflib.unified_diff(
                text.splitlines(True), new.splitlines(True), path, path + " (gasem fmt)"))
        elif args.check:
            print(f"нужно выровнять: {path}")
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            print(f"выровнено: {path}")
    if (args.check or args.diff) and unformatted:
        return 1
    return 0


USAGE = f"""gasem {__version__} — компилятор языка Gasem

  gasem программа.gsm [-o файл.bin] [-l листинг] [-m карта]   собрать
  gasem run программа.gsm                                      собрать и запустить в QEMU
  gasem debug программа.gsm -b метка                           запустить под отладчиком GDB
  gasem fmt файлы.gsm [--check] [--diff]                       выровнять оформление

Подробнее: gasem <команда> --help
"""


def main(argv=None):
    _setup_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    if argv[0] in ("-V", "--version"):
        print(f"gasem {__version__}")
        return 0
    command = argv.pop(0) if argv[0] in COMMANDS else "build"
    return {"build": cmd_build, "run": cmd_run, "debug": cmd_debug, "fmt": cmd_fmt}[command](argv)
