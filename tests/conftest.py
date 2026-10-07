import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import impl  # noqa: E402,F401  — GASEM_IMPL=go: тесты проверяют Go-версию компилятора
