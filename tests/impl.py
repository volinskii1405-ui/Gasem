"""Какую реализацию компилятора проверяют тесты.

По умолчанию — Python-версию (пакет gasem). С переменной окружения
GASEM_IMPL=go те же тесты проверяют Go-версию (каталог go/): функции
compile_source, compile_file, format_text, tokenize и gasem.cli.main
подменяются вызовами Go-компилятора. Go-программы собираются автоматически
(нужен go), либо берутся из каталога GASEM_GO_BIN.

    GASEM_IMPL=go python3 -m pytest tests
"""

import atexit
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

import gasem
from gasem.errors import GasemError, GasemErrors, GasemWarning, SourceLoc

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IMPL = os.environ.get("GASEM_IMPL", "python")

# команда, которой запускается компилятор из командной строки
GASEM_CMD = [sys.executable, "-m", "gasem"]

# Python-версия — до подмены (с ней сверяется Go-версия в test_go.py)
PY_COMPILE_SOURCE = gasem.compile_source
PY_COMPILE_FILE = gasem.compile_file


def build_go(out_dir=None):
    """Собрать gasem и gasemapi из go/ → каталог с программами."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="gasem-go-")
    exe = ".exe" if os.name == "nt" else ""
    for name in ("gasem", "gasemapi"):
        subprocess.run(["go", "build", "-o", os.path.join(out_dir, name + exe), "./cmd/" + name],
                       cwd=os.path.join(ROOT, "go"), check=True)
    return out_dir


class GoError(GasemError):
    """Ошибка, найденная Go-компилятором: текст сообщения — его собственный."""

    def __init__(self, data):
        loc = SourceLoc(data["file"], data["line"], data["text"]) if data["file"] is not None else None
        super().__init__(data["message"], loc, data["col"])
        self.formatted = data["formatted"]

    def format(self):
        return self.formatted


class GoWarning(GoError, GasemWarning):
    KIND = GasemWarning.KIND


def _error(data):
    return (GoWarning if data["warning"] else GoError)(data)


class GoToken:
    def __init__(self, kind, value):
        self.kind = kind
        self.value = value


class GoCompiler:
    def __init__(self, bin_dir):
        exe = ".exe" if os.name == "nt" else ""
        self.gasem = os.path.join(bin_dir, "gasem" + exe)
        self.api_path = os.path.join(bin_dir, "gasemapi" + exe)
        self.proc = None

    def request(self, **req):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen([self.api_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         encoding="utf-8")
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("Go-компилятор завершился аварийно")
        resp = json.loads(line)
        if "crash" in resp:
            raise RuntimeError("Go-компилятор: " + resp["crash"])
        return resp

    def close(self):
        if self.proc is not None:
            self.proc.stdin.close()
            self.proc.wait()
            self.proc.stdout.close()

    def result(self, resp):
        if not resp["ok"]:
            raise GasemErrors([_error(e) for e in resp["errors"]])
        lines = [(int(a), size, bits, SourceLoc(f, n, t)) for a, size, bits, f, n, t in resp["lines"]]
        return gasem.CompileResult(base64.b64decode(resp["code"]), int(resp["origin"]),
                                   {name: int(v) for name, v in resp["symbols"]}, resp["listing"],
                                   lines, [_error(w) for w in resp["warnings"]])

    def compile_source(self, text, filename="<источник>", base_dir=None):
        return self.result(self.request(op="compile_source", text=text, filename=filename, base_dir=base_dir))

    def compile_file(self, path):
        return self.result(self.request(op="compile_file", path=path))

    def format_text(self, text):
        return self.request(op="format_text", text=text)["text"]

    def tokenize(self, text, loc=None, col_base=0, seps=True):
        assert col_base == 0 and seps, "через Go доступен только tokenize(строка, место)"
        resp = self.request(op="tokenize", text=text)
        if not resp["ok"]:
            e = _error(resp["errors"][0])
            e.loc = loc
            raise e
        out = []
        for kind, value in resp["tokens"]:
            if isinstance(value, dict):
                value = int(value["int"]) if "int" in value else base64.b64decode(value["bytes"])
            out.append(GoToken(kind, value))
        return out

    def main(self, argv=None):
        argv = list(sys.argv[1:] if argv is None else argv)
        r = subprocess.run([self.gasem, *argv], capture_output=True, text=True, encoding="utf-8")
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode


_go = None


def go_compiler():
    """Go-компилятор (собирается при первом обращении) или None, если нет go."""
    global _go
    if _go is None:
        bin_dir = os.environ.get("GASEM_GO_BIN")
        if not bin_dir:
            if shutil.which("go") is None:
                return None
            bin_dir = build_go()
        _go = GoCompiler(bin_dir)
        atexit.register(_go.close)
    return _go


def install():
    """Подменить Python-компилятор на Go-версию (GASEM_IMPL=go)."""
    global GASEM_CMD
    go = go_compiler()
    if go is None:
        raise RuntimeError("GASEM_IMPL=go: не найден go (или укажите GASEM_GO_BIN)")

    import gasem.assembler
    import gasem.cli
    import gasem.fmt
    import gasem.lexer
    for mod in (gasem, gasem.assembler):
        mod.compile_source = go.compile_source
        mod.compile_file = go.compile_file
    gasem.fmt.format_text = go.format_text
    gasem.lexer.tokenize = go.tokenize
    gasem.cli.main = go.main
    GASEM_CMD = [go.gasem]
    return go


def record(path):
    """Записывать входные данные компилятора в path (JSONL) — для сверки Go и Python."""
    import gasem.assembler
    import gasem.fmt

    def wrap(name, fn):
        def inner(*args, **kw):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"op": name, "args": args, "kw": kw}, ensure_ascii=False) + "\n")
            return fn(*args, **kw)
        return inner

    for mod in (gasem, gasem.assembler):
        mod.compile_source = wrap("compile_source", gasem.assembler.compile_source)
        mod.compile_file = wrap("compile_file", gasem.assembler.compile_file)
    gasem.fmt.format_text = wrap("format_text", gasem.fmt.format_text)


GO = install() if IMPL == "go" else None
if os.environ.get("GASEM_RECORD"):
    record(os.environ["GASEM_RECORD"])
