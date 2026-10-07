"""gasem fmt — приводит программу к единому виду.

Меняются только пробелы, поэтому смысл программы остаться прежним не может:
  * тело if / while / for / repeat / macro / proc / struct / at сдвигается
    на 4 пробела от начала блока, elif / else / end / until — на уровень
    начала блока;
  * комментарии в конце строк выравниваются в один столбец (в пределах
    группы строк без пустых строк между ними);
  * табуляция заменяется пробелами, пробелы в конце строк убираются.
"""

from .lexer import tokenize, ID, OP
from .errors import GasemError

OPENERS = {"if", "while", "for", "repeat", "macro", "proc", "struct", "at"}
MIDDLE = {"elif", "else"}
CLOSERS = {"end", "until"}
DATA_WORDS = {"b", "w", "d", "q", "s"}
COMMENT_COL = 33          # столбец комментариев по умолчанию
LONG_LINE = 70            # такие длинные строки в выравнивании не участвуют
INDENT = 4


def split_comment(line):
    """Строка → (код, комментарий). ';' внутри строк в кавычках — не комментарий."""
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ";":
            return line[:i].rstrip(), line[i:].rstrip()
        i += 1
    return line.rstrip(), ""


def statement_word(code):
    """Первое слово оператора (после меток вида «имя:»), в нижнем регистре."""
    try:
        toks = tokenize(code, None)
    except GasemError:
        return ""
    i = 0
    while (i + 1 < len(toks) and toks[i].kind == ID and toks[i + 1].kind == OP
           and toks[i + 1].value == ":" and toks[i].value.lower() not in DATA_WORDS):
        i += 2
    return toks[i].value.lower() if i < len(toks) and toks[i].kind == ID else ""


def format_text(text):
    lines = text.expandtabs(INDENT).split("\n")
    stack = []                     # отступы открытых блоков
    rows = []                      # (отступ, код, комментарий) или None для пустой строки
    for line in lines:
        code, comment = split_comment(line)
        body = code.strip()
        own_indent = len(code) - len(code.lstrip())
        if not body:
            if not comment:
                rows.append(None)
                continue
            indent = stack[-1] + INDENT if stack else len(line) - len(line.lstrip())
            rows.append((indent, "", comment))
            continue
        word = statement_word(body)
        if word in MIDDLE or word in CLOSERS:
            indent = stack[-1] if stack else own_indent
            if word in CLOSERS and stack:
                stack.pop()
        else:
            indent = stack[-1] + INDENT if stack else own_indent
            if word in OPENERS:
                stack.append(indent)
        rows.append((indent, body, comment))

    out = []
    group = []

    def flush():
        widths = [ind + len(code) for ind, code, com in group
                  if code and com and ind + len(code) <= LONG_LINE]
        col = max([COMMENT_COL] + [w + 2 for w in widths])
        for ind, code, com in group:
            if not code:
                out.append(" " * ind + com)
            elif not com:
                out.append(" " * ind + code)
            else:
                left = " " * ind + code
                pad = col - len(left) if len(left) + 2 <= col else 1
                out.append(left + " " * pad + com)
        group.clear()

    for row in rows:
        if row is None:
            flush()
            out.append("")
        else:
            group.append(row)
    flush()
    return "\n".join(out)
