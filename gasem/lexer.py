"""Лексер Gasem: разбивает строку исходника на токены.

Особенность языка: разделитель операндов — дефис, окружённый пробелами
(``mov ax - 1``). Дефис без пробелов — это минус (``510-($-$$)``).
Внутри скобок ``( )`` и ``[ ]`` дефис всегда минус.
"""

from .errors import GasemError

NUM = "num"   # число
STR = "str"   # строка в кавычках
ID = "id"     # имя: команда, регистр, метка...
OP = "op"     # оператор или знак препинания
SEP = "sep"   # разделитель операндов ' - '

WHITESPACE = " \t\r\f\v"

_ESCAPES = {
    "n": 10, "r": 13, "t": 9, "0": 0, "a": 7, "b": 8, "e": 27, "f": 12,
    "v": 11, "\\": 92, '"': 34, "'": 39,
}
_OPS2 = ("&&", "<<", ">>", "$$")
_OPS1 = "+-*/%()[]:~&|^=,$"


class Token:
    __slots__ = ("kind", "value", "col", "space", "text", "quote")

    def __init__(self, kind, value, col, space, text, quote=None):
        self.kind = kind      # NUM / STR / ID / OP / SEP
        self.value = value    # int, bytes или str
        self.col = col        # позиция в строке (для сообщений об ошибках)
        self.space = space    # был ли пробел перед токеном
        self.text = text      # исходный текст токена
        self.quote = quote    # для строк: какой кавычкой открыта

    def is_op(self, *ops):
        return self.kind == OP and self.value in ops

    def is_id(self, *names):
        return self.kind == ID and self.value.lower() in names

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r})"


def _is_id_start(c):
    return c.isalpha() or c in "_."


def _is_id_char(c):
    return c.isalnum() or c in "_."


def parse_number(word):
    """Число: 1234, 0x7C00, 0b1010, 0o17, 0FFh. Подчёркивания игнорируются."""
    w = word.replace("_", "").lower()
    if not w:
        return None
    try:
        if w.endswith("h") and len(w) > 1 and all(c in "0123456789abcdef" for c in w[:-1]):
            return int(w[:-1], 16)
        if w.startswith("0x"):
            return int(w[2:], 16)
        if w.startswith("0b"):
            return int(w[2:], 2)
        if w.startswith("0o"):
            return int(w[2:], 8)
        return int(w, 10)
    except ValueError:
        return None


def _read_string(text, i, loc, col_base):
    quote = text[i]
    j = i + 1
    out = bytearray()
    while True:
        if j >= len(text):
            raise GasemError("незакрытая строка (нет закрывающей кавычки)", loc, col_base + i)
        c = text[j]
        if c == quote:
            return bytes(out), j + 1
        if c == "\\":
            if j + 1 >= len(text):
                raise GasemError("незакрытая строка (нет закрывающей кавычки)", loc, col_base + i)
            e = text[j + 1]
            if e == "x":
                h = text[j + 2:j + 4]
                if len(h) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
                    raise GasemError("после \\x нужны две шестнадцатеричные цифры", loc, col_base + j)
                out.append(int(h, 16))
                j += 4
                continue
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                j += 2
                continue
            raise GasemError(f"неизвестная escape-последовательность \\{e}", loc, col_base + j)
        out += c.encode("utf-8")
        j += 1


def tokenize(text, loc, col_base=0, seps=True):
    """Разбить строку на токены. Комментарий начинается с ';'.

    seps=False отключает распознавание разделителя ' - ' (используется
    внутри выражений do, где дефис всегда означает минус).
    """
    toks = []
    i, n = 0, len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in WHITESPACE:
            i += 1
            continue
        if c == ";":
            break
        space = i == 0 or text[i - 1] in WHITESPACE
        col = col_base + i

        if c in "\"'":
            value, j = _read_string(text, i, loc, col_base)
            toks.append(Token(STR, value, col, space, text[i:j], quote=c))
            i = j
            continue

        if c.isdigit():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            value = parse_number(word)
            if value is None:
                raise GasemError(f"неверное число '{word}'", loc, col)
            toks.append(Token(NUM, value, col, space, word))
            i = j
            continue

        if _is_id_start(c):
            j = i + 1
            while j < n and _is_id_char(text[j]):
                j += 1
            word = text[i:j]
            toks.append(Token(ID, word, col, space, word))
            i = j
            continue

        two = text[i:i + 2]
        if two in _OPS2:
            toks.append(Token(OP, two, col, space, two))
            i += 2
            continue

        if c in _OPS1:
            if (c == "-" and seps and depth == 0 and space
                    and (i + 1 >= n or text[i + 1] in WHITESPACE or text[i + 1] == ";")):
                toks.append(Token(SEP, "-", col, space, "-"))
                i += 1
                continue
            if c in "([":
                depth += 1
            elif c in ")]":
                depth = max(0, depth - 1)
            toks.append(Token(OP, c, col, space, c))
            i += 1
            continue

        raise GasemError(f"недопустимый символ '{c}'", loc, col)
    return toks
