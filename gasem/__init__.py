"""Gasem — компилируемый язык для загрузчиков и системного ПО.

Синтаксис в стиле ассемблера x86, операнды разделяются дефисом: mov ax - 1.
Компилятор выдаёт плоский двоичный файл (машинный код x86, 16/32 бит).
"""

from .assembler import CompileResult, compile_file, compile_source
from .errors import GasemError, GasemErrors

__version__ = "0.2.0"

__all__ = ["compile_source", "compile_file", "CompileResult", "GasemError", "GasemErrors", "__version__"]
