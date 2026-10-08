"""Подсказки «может быть, вы имели в виду…» для опечаток."""

from . import x86

DIRECTIVE_WORDS = {"og", "align", "include", "incbin", "incprog", "do", "pool", "args", "equ",
                   "if", "elif", "else", "end", "while", "for", "repeat", "until",
                   "break", "continue", "let", "macro", "proc", "struct", "at", "local", "return"}
COMMAND_WORDS = sorted(x86.MNEMONICS | set(x86.ALIASES) | {"jf" + cc for cc in x86.CC}
                       | set(x86.PREFIXES) | DIRECTIVE_WORDS)


def distance(a, b):
    """Число правок (вставка, удаление, замена, перестановка соседних букв)."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
_POS = {ch: (r, c) for r, row in enumerate(_ROWS) for c, ch in enumerate(row)}


def _neighbours(a, b):
    """Сколько замен — соседние клавиши (ebz → ebx правдоподобнее, чем ebp)."""
    if len(a) != len(b):
        return 0
    n = 0
    for x, y in zip(a, b):
        if x != y and x in _POS and y in _POS:
            (r1, c1), (r2, c2) = _POS[x], _POS[y]
            n += abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1
    return n


def closest(word, candidates):
    """Самое похожее слово из candidates (или None)."""
    low = word.lower()
    limit = 1 if len(low) <= 4 else 2
    best = None
    for c in candidates:
        if c == word:
            continue
        d = distance(low, c.lower())
        if d > limit:
            continue
        # ближе; той же длины; опечатка на соседней клавише
        key = (d, abs(len(c) - len(word)), -_neighbours(low, c.lower()), c)
        if best is None or key < best[0]:
            best = (key, c)
    return best[1] if best else None


def suggest_name(name, symbols):
    """Подсказка для неизвестного имени: похожая метка или регистр."""
    found = closest(name, [s for s in symbols if not s.startswith("@")])
    if found:
        return f" — может быть, '{found}'?"
    reg = closest(name, x86.REGISTERS)
    if reg:
        return f" — может быть, регистр '{reg}'?"
    return ""


def suggest_command(word):
    """Похожая команда: перестановка, лишняя или пропущенная буква (mvo → mov)."""
    found = closest(word, [c for c in COMMAND_WORDS if abs(len(c) - len(word)) <= 1])
    return f" — может быть, '{found}'?" if found else ""
