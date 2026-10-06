"""Запуск загрузочного сектора в эмуляторе x86 (Unicorn) для тестов.

Эмулируется минимум «BIOS»: int 0x10 с ah=0x0E (вывод символа) и порт 0x92
(быстрое включение A20). Эмуляция останавливается на 'jmp $' или 'hlt'.
"""

try:
    import unicorn
    from unicorn import x86_const as X
except ImportError:  # pragma: no cover
    unicorn = None


class BootResult:
    def __init__(self, output, memory, port92, regs):
        self.output = output    # что напечатано через int 0x10
        self.memory = memory    # первый мегабайт памяти после работы
        self.port92 = port92    # последнее значение, записанное в порт 0x92
        self.regs = regs

    def screen(self, chars=80):
        """Текст из видеопамяти 0xB8000 (первая строка)."""
        raw = self.memory[0xB8000:0xB8000 + chars * 2]
        return bytes(raw[0::2]).rstrip(b"\0").decode("latin-1")


def run_boot(code, max_steps=1_000_000):
    mu = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_16)
    mu.mem_map(0, 0x100000)
    mu.mem_write(0x7C00, bytes(code))
    mu.reg_write(X.UC_X86_REG_DL, 0x80)   # номер загрузочного диска, как у BIOS
    output = bytearray()
    port92 = [0]

    def on_intr(uc, intno, _):
        if intno == 0x10:
            ax = uc.reg_read(X.UC_X86_REG_AX)
            if ax >> 8 == 0x0E:
                output.append(ax & 0xFF)
            return
        uc.emu_stop()

    def on_code(uc, address, size, _):
        head = bytes(uc.mem_read(address, 2))
        if head == b"\xeb\xfe" or head[0] == 0xF4:
            uc.emu_stop()

    def on_in(uc, port, size, _):
        return port92[0] if port == 0x92 else 0

    def on_out(uc, port, size, value, _):
        if port == 0x92:
            port92[0] = value

    mu.hook_add(unicorn.UC_HOOK_INTR, on_intr)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_code)
    mu.hook_add(unicorn.UC_HOOK_INSN, on_in, None, 1, 0, X.UC_X86_INS_IN)
    mu.hook_add(unicorn.UC_HOOK_INSN, on_out, None, 1, 0, X.UC_X86_INS_OUT)
    mu.emu_start(0x7C00, 0x100000, count=max_steps)
    regs = {name: mu.reg_read(getattr(X, "UC_X86_REG_" + name.upper()))
            for name in ("eax", "ebx", "ecx", "edx", "esi", "edi", "esp", "cr0")}
    return BootResult(bytes(output), bytes(mu.mem_read(0, 0x100000)), port92[0], regs)
