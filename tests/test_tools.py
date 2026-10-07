"""Инструменты: gasem fmt, подсветка для VS Code, командная строка, отладчик."""

import contextlib
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

from gasem import compile_file, compile_source
from gasem.cli import main
from gasem.fmt import format_text
from gasem.lexer import tokenize

from emu import run_boot, unicorn
import impl

ROOT = os.path.join(os.path.dirname(__file__), "..")
SOURCES = sorted(glob.glob(os.path.join(ROOT, "examples", "*.gsm")) + glob.glob(os.path.join(ROOT, "os", "*.gsm")))


def tokens(text):
    return [[(t.kind, t.value) for t in tokenize(line, None)] for line in text.split("\n")]


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class FormatterTest(unittest.TestCase):
    def test_blocks_and_comments(self):
        src = ("start:\n"
               "  if al = 1   ; один\n"
               "        inc bx ; плюс\n"
               "  else\n"
               "inc cx\n"
               "     end\n"
               "    mov eax - 1 ; комментарий с ; внутри и \"кавычками\"\n"
               "    mov esi - \"a;b\"   ; строка с ;\n")
        def at33(code, comment):              # комментарий в столбце 33
            return code + " " * (33 - len(code)) + comment

        self.assertEqual(format_text(src), "\n".join([
            "start:",
            at33("  if al = 1", "; один"),
            at33("      inc bx", "; плюс"),
            "  else",
            "      inc cx",
            "  end",
            at33("    mov eax - 1", '; комментарий с ; внутри и "кавычками"'),
            at33('    mov esi - "a;b"', "; строка с ;"),
            ""]))

    def test_repository_sources(self):
        """На всех исходниках: меняются только пробелы, повторный запуск ничего не меняет."""
        for path in SOURCES:
            with self.subTest(path=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                out = format_text(src)
                self.assertEqual(tokens(out), tokens(src))
                self.assertEqual(format_text(out), out)

    def test_same_binary(self):
        """Отформатированные пример и вся GasemOS собираются в те же байты."""
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("examples", "os"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                                ignore=shutil.ignore_patterns("*.png", "*.py"))
            for path in glob.glob(os.path.join(tmp, "*", "*.gsm")):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(format_text(text))
            for rel in ("examples/do.gsm", "examples/pmode.gsm", "os/gasemos.gsm"):
                self.assertEqual(compile_file(os.path.join(tmp, rel)).code,
                                 compile_file(os.path.join(ROOT, rel)).code, rel)

    def test_cli_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.gsm")
            with open(path, "w", encoding="utf-8") as f:
                f.write("if al = 1\nnop\nend\n")
            self.assertEqual(cli("fmt", "--check", path)[0], 1)
            self.assertEqual(cli("fmt", path)[0], 0)
            self.assertEqual(cli("fmt", "--check", path)[0], 0)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "if al = 1\n    nop\nend\n")


class VSCodeTest(unittest.TestCase):
    DIR = os.path.join(ROOT, "editors", "vscode")

    def test_grammar_is_generated(self):
        """Грамматика в репозитории совпадает с тем, что генерируется из таблиц компилятора."""
        sys.path.insert(0, self.DIR)
        import generate
        with open(os.path.join(self.DIR, "syntaxes", "gasem.tmLanguage.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), generate.grammar(),
                             "запустите python3 editors/vscode/generate.py")

    def test_json_files(self):
        for name in ("package.json", "language-configuration.json", "snippets/gasem.json"):
            with open(os.path.join(self.DIR, name), encoding="utf-8") as f:
                json.load(f)

    def test_snippets_compile(self):
        with open(os.path.join(self.DIR, "snippets", "gasem.json"), encoding="utf-8") as f:
            snippets = json.load(f)
        for name, sn in snippets.items():
            body = "\n".join(sn["body"])
            body = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", body)      # ${1:текст} → текст
            body = re.sub(r"\$\d+", "nop", body)                   # $0 → nop
            if name == "Подпрограмма с аргументами":
                body = body.replace("name", "f")
            with self.subTest(snippet=name):
                compile_source("b 32\n" + body + ("\nx d: 0\ny d: 0" if "[x]" in body else ""))

    @unittest.skipUnless(unicorn, "unicorn не установлен")
    def test_boot_snippet_runs(self):
        with open(os.path.join(self.DIR, "snippets", "gasem.json"), encoding="utf-8") as f:
            body = "\n".join(json.load(f)["Загрузочный сектор"]["body"])
        body = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", body)
        self.assertEqual(run_boot(compile_source(body).code).output, b"Hello from Gasem!")

    @unittest.skipUnless(shutil.which("node") and impl.go_compiler() is not None, "нужны node и go")
    def test_language_server_client(self):
        """extension.js и «gasem lsp» через ту же библиотеку JSON-RPC, что у VS Code."""
        import subprocess
        if not os.path.isdir(os.path.join(self.DIR, "node_modules")):
            self.skipTest("нужен npm install в editors/vscode")
        r = subprocess.run(["node", "test-extension.js", impl.go_compiler().gasem], cwd=self.DIR,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gasem lsp через vscode-jsonrpc", r.stdout)


class CliTest(unittest.TestCase):
    def test_build_map_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "p.gsm")
            with open(src, "w", encoding="utf-8") as f:
                f.write("start:\n    call f\n    ret\nf: ret\nlost: nop\n")
            code, _, err = cli(src, "-q", "-m", os.path.join(tmp, "p.map"))
            self.assertEqual(code, 0)
            self.assertIn("p.gsm:5: предупреждение: метка 'lost' нигде не используется", err)
            with open(os.path.join(tmp, "p.map"), encoding="utf-8") as f:
                self.assertIn("00000004  f", f.read())
            code, _, err = cli("build", src, "-q", "-w")
            self.assertEqual((code, err), (0, ""))

    def test_errors_with_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "p.gsm")
            with open(src, "w", encoding="utf-8") as f:
                f.write("mvo ax - 1\njmp prnit\nprint: ret\n")
            code, _, err = cli(src)
            self.assertEqual(code, 1)
            self.assertIn("неизвестная команда 'mvo' — может быть, 'mov'?", err)

    def test_usage(self):
        code, out, _ = cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("gasem debug", out)


@unittest.skipUnless(shutil.which("gdb") and (shutil.which("qemu-system-i386") or shutil.which("qemu-system-x86_64")),
                     "нужны gdb и QEMU")
class DebugTest(unittest.TestCase):
    def test_breakpoint_on_label(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "hello.gsm")
            shutil.copy(os.path.join(ROOT, "examples", "hello.gsm"), src)
            r = subprocess.run([*impl.GASEM_CMD, "debug", src, "-b", "print", "--batch", "-q"],
                               cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertIn("=> hello.gsm:8    mov ah - 0x0E", r.stdout)
            self.assertIn("esi=00007c12", r.stdout)


if __name__ == "__main__":
    unittest.main()
