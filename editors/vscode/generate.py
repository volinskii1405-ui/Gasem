#!/usr/bin/env python3
"""Сгенерировать грамматику подсветки (TextMate) из таблиц компилятора Gasem.

    python3 editors/vscode/generate.py

Так подсветка всегда знает ровно те команды и регистры, что и компилятор.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from gasem import x86  # noqa: E402
from gasem.highlevel import CONTROL_WORDS  # noqa: E402

GASEM_ALIASES = {"nxtb", "nxtw", "nxtd", "chk"} | {"jf" + cc for cc in x86.CC}
MNEMONICS = sorted(x86.MNEMONICS | set(x86.ALIASES), key=lambda m: (-len(m), m))
REGISTERS = sorted(x86.REGISTERS, key=lambda r: (-len(r), r))
PREFIXES = sorted(set(x86.PREFIXES) | set(x86.SEG_PREFIX))
CONTROL = sorted(CONTROL_WORDS - {"let"} | {"signed"})
DIRECTIVES = ["og", "align", "include", "incbin", "pool", "args", "equ"]
SIZES = ["byte", "word", "dword", "qword", "short", "near", "far"]

ID = r"[A-Za-z_.\p{L}][\w.\p{L}]*"
STMT_START = r"(?:^|(?<=:))\s*"            # начало строки или сразу после метки «имя:»


def words(ws):
    return r"\b(?:" + "|".join(ws) + r")\b"


def grammar():
    expr = [
        {"include": "#string"},
        {"include": "#char"},
        {"include": "#number"},
        {"name": "variable.language.register.gasem", "match": "(?i)" + words(REGISTERS)},
        {"name": "variable.language.location.gasem", "match": r"\$\$?"},
        {"name": "keyword.operator.gasem", "match": r"<<|>>|<=|>=|!=|==|[+\-*/%&|^~<>=]"},
    ]
    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "name": "Gasem",
        "scopeName": "source.gasem",
        "fileTypes": ["gsm"],
        "patterns": [
            {"include": "#comment"},
            {"include": "#constant-definition"},
            {"include": "#data-label"},
            {"include": "#label"},
            {"include": "#expression-statement"},
            {"include": "#statement"},
            {"include": "#data"},
            {"include": "#operands"},
        ],
        "repository": {
            "comment": {"name": "comment.line.semicolon.gasem", "match": ";.*$"},
            "string": {
                "name": "string.quoted.double.gasem", "begin": '"', "end": '"',
                "patterns": [{"name": "constant.character.escape.gasem",
                              "match": r"\\(?:x[0-9A-Fa-f]{2}|.)"}],
            },
            "char": {
                "name": "string.quoted.single.gasem", "begin": "'", "end": "'",
                "patterns": [{"name": "constant.character.escape.gasem",
                              "match": r"\\(?:x[0-9A-Fa-f]{2}|.)"}],
            },
            "number": {
                "name": "constant.numeric.gasem",
                "match": r"\b(?:0[xX][0-9A-Fa-f_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9A-Fa-f_]*[hH]|[0-9][0-9_]*)\b",
            },
            "constant-definition": {
                "match": rf"^\s*({ID})\s*(=|\bequ\b)",
                "captures": {"1": {"name": "entity.name.constant.gasem"},
                             "2": {"name": "keyword.operator.assignment.gasem"}},
            },
            "data-label": {
                "match": rf"^\s*(?!(?i:[bwdqs])\s*[:\-/])({ID})\s+(?=[bwdqsBWDQS](?::|-|/))",
                "captures": {"1": {"name": "entity.name.variable.gasem"}},
            },
            "label": {
                "match": rf"^\s*(?!(?i:[bwdqs]):)({ID})\s*(:)",
                "captures": {"1": {"name": "entity.name.function.label.gasem"},
                             "2": {"name": "punctuation.definition.label.gasem"}},
            },
            "data": {
                "match": r"(?i)\b([bwdqs])(:|-|/)",
                "captures": {"1": {"name": "storage.type.data.gasem"},
                             "2": {"name": "storage.type.data.gasem"}},
            },
            "expression-statement": {
                "comment": "do и let: выражение в кавычках подсвечивается как код",
                "begin": STMT_START + r"(?i)\b(do|let)\b",
                "beginCaptures": {"1": {"name": "keyword.control.gasem"}},
                "end": r"(?=;)|$",
                "patterns": [
                    {"name": "keyword.control.gasem", "match": r"(?i)\bsigned\b"},
                    {"name": "meta.embedded.expression.gasem",
                     "begin": '"', "end": '"',
                     "beginCaptures": {"0": {"name": "punctuation.definition.string.begin.gasem"}},
                     "endCaptures": {"0": {"name": "punctuation.definition.string.end.gasem"}},
                     "patterns": [{"include": "#char"}, {"include": "#number"},
                                  {"name": "storage.modifier.gasem", "match": "(?i)" + words(SIZES)},
                                  *expr[3:]]},
                    {"include": "#operands"},
                ],
            },
            "statement": {
                "comment": "команда, директива или управляющее слово в начале строки",
                "match": STMT_START + r"(?i)(?:\b(" + "|".join(PREFIXES) + r")\s+)*(?:"
                         + r"\b(" + "|".join(sorted(GASEM_ALIASES, key=lambda m: (-len(m), m))) + r")\b"
                         + r"|\b(" + "|".join(MNEMONICS) + r")\b"
                         + r"|\b(" + "|".join(CONTROL) + r")\b"
                         + r"|\b(" + "|".join(DIRECTIVES) + r")\b"
                         + r"|\b(b)\s+(?=\d)"
                         + r"|(&&))",
                "captures": {
                    "1": {"name": "keyword.other.prefix.gasem"},
                    "2": {"name": "keyword.mnemonic.gasem.alias"},
                    "3": {"name": "keyword.mnemonic.gasem"},
                    "4": {"name": "keyword.control.gasem"},
                    "5": {"name": "keyword.control.directive.gasem"},
                    "6": {"name": "keyword.control.directive.gasem"},
                    "7": {"name": "keyword.operator.repeat.gasem"},
                },
            },
            "operands": {
                "patterns": [
                    {"include": "#comment"},
                    {"name": "punctuation.separator.operand.gasem", "match": r"(?<=\s)-(?=\s|$)"},
                    {"name": "keyword.control.gasem", "match": r"(?i)\b(?:if|and|or|not|signed)\b"},
                    {"name": "support.constant.flag.gasem",
                     "match": r"(?i)\b(?:zero|carry|sign|overflow|parity)\b"},
                    {"name": "support.constant.port.gasem", "match": r"(?i)\ba20\b"},
                    {"name": "storage.modifier.gasem", "match": "(?i)" + words(SIZES)},
                    {"name": "meta.brackets.gasem", "begin": r"[\[(]", "end": r"[\])]",
                     "patterns": [{"include": "#comment"}, *expr]},
                    *expr,
                ],
            },
        },
    }


def main():
    path = os.path.join(HERE, "syntaxes", "gasem.tmLanguage.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(grammar(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("записано:", path)


if __name__ == "__main__":
    main()
