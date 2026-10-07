"""Ошибки компиляции Gasem и их форматирование."""


class SourceLoc:
    """Место в исходном тексте: файл, номер строки и сама строка."""

    __slots__ = ("file", "line", "text")

    def __init__(self, file, line, text):
        self.file = file
        self.line = line
        self.text = text

    def __str__(self):
        return f"{self.file}:{self.line}"


class GasemError(Exception):
    """Ошибка в программе на Gasem."""

    KIND = "ошибка"

    def __init__(self, message, loc=None, col=None):
        super().__init__(message)
        self.message = message
        self.loc = loc
        self.col = col

    def format(self):
        if self.loc is None:
            return f"{self.KIND}: {self.message}"
        out = f"{self.loc}: {self.KIND}: {self.message}"
        text = self.loc.text.rstrip("\r\n")
        if text.strip():
            out += "\n    " + text.expandtabs(4)
            if self.col is not None and 0 <= self.col <= len(text):
                pad = len(text[: self.col].expandtabs(4))
                out += "\n    " + " " * pad + "^"
        return out

    def __str__(self):
        return self.format()


class GasemWarning(GasemError):
    """Предупреждение: программа собирается, но что-то подозрительно."""

    KIND = "предупреждение"


class GasemErrors(Exception):
    """Набор ошибок, найденных за один запуск компилятора."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} ошибок")
        self.errors = list(errors)

    def format(self):
        return "\n".join(e.format() for e in self.errors)

    def __str__(self):
        return self.format()
