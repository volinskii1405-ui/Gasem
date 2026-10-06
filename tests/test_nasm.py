"""Сверка машинного кода Gasem с NASM (эталонный ассемблер).

Каждая строка компилируется в двух вариантах — на Gasem и (после замены
' - ' на ', ') на NASM — и байты должны совпасть. Если NASM не установлен,
тесты пропускаются.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from gasem import compile_source

NASM = shutil.which("nasm")

# Строка Gasem → строка NASM (чисто синтаксическая замена).
_ALIASES = {"nxtb": "lodsb", "nxtw": "lodsw", "nxtd": "lodsd", "chk": "test"}


def to_nasm(line):
    out, depth = [], 0
    for i, c in enumerate(line):
        depth += c in "(["
        depth -= c in ")]"
        if c == "-" and depth == 0 and line[i - 1:i] == " " and line[i + 1:i + 2] == " ":
            out[-1] = ","
            out.append("")
        else:
            out.append(c)
    line = "".join(out).replace(",  ", ", ")
    head, _, rest = line.partition(" ")
    low = head.lower()
    if low in _ALIASES:
        head = _ALIASES[low]
    elif re.fullmatch(r"jf[a-z]+", low):
        head = "j" + low[2:]
    return (head + " " + rest).strip()


LINES_16 = """
mov ax - bx
mov al - bl
mov ah - 0x12
mov ax - 0x1234
mov eax - 0x12345678
mov ax - [bx]
mov ax - [bx+si]
mov ax - [si+bx]
mov ax - [bp]
mov ax - [bp+di+4]
mov ax - [si-2]
mov ax - [bx+0x1234]
mov ax - [bx - 2]
mov al - [lvar]
mov ax - [lvar]
mov [lvar] - ax
mov [lvar] - al
mov bx - [lvar]
mov [lvar] - bx
mov eax - [lvar]
mov byte [bx] - 5
mov word [bx+2] - 0x1234
mov dword [bx] - 1
mov [bx] - word 7
mov ds - ax
mov es - [bx]
mov ss - ax
mov ax - ds
mov eax - ds
mov [bx] - es
mov eax - cr0
mov cr0 - eax
mov cr3 - ebx
mov eax - [ebx]
mov eax - [ebx+ecx*4+8]
mov eax - [ebx+ecx]
mov eax - [esp]
mov eax - [esp+4]
mov eax - [ebp]
mov eax - [ebp-4]
mov eax - [eax*4]
mov eax - [eax*2]
mov eax - [ebx+0x12345678]
mov ax - [es:bx]
mov ax - [es:di+4]
mov ax - [cs:lvar]
mov al - [fs:esi]
mov si - lvar
mov si - lvar+2
mov ax - 'A'
mov ax - 'AB'
mov ax - -1
mov cl - 255
add ax - 5
add ax - 0x1234
add bx - 5
add bx - 0x1234
add al - 5
add bl - 5
add ax - bx
add [bx] - ax
add ax - [bx]
add al - [si]
add byte [bx] - 5
add word [bx] - 5
add word [bx] - 500
add eax - 5
add eax - 0x12345678
add ax - -1
add ax - 0xFFFF
add word [bx] - byte 5
or ax - 1
or al - 0x80
adc dx - 0
sbb cx - bx
and al - 0xDF
and ax - 0xFF00
sub sp - 8
sub ax - [bp+4]
xor ax - ax
xor eax - eax
xor dl - dl
cmp al - 'a'
cmp byte [si] - 0
cmp ax - 1000
cmp [lvar] - word 3
test ax - bx
test al - 1
test ax - 0x8000
test bl - 1
test word [bx] - 1
test [bx] - ax
test ax - [bx]
inc ax
inc al
inc eax
inc word [bx]
inc byte [bx]
dec cx
dec cl
dec dword [bx]
not ax
neg al
mul bx
div cl
idiv word [bx]
imul cx
imul ax - bx
imul ax - [bx]
imul ax - bx - 10
imul ax - bx - 1000
imul ax - 5
imul eax - ecx - 3
shl ax - 1
shl ax - 4
shr al - cl
sar word [bx] - 2
rol eax - 8
ror bx - 1
rcl al - 1
rcr cx - cl
sal ax - 3
shr byte [bx] - 1
push ax
push eax
push es
push cs
push ds
push ss
push fs
push gs
push 5
push 0x1234
push word [bx]
push dword [bx]
push lvar
push -1
push dword 5
pop ax
pop eax
pop es
pop ds
pop ss
pop fs
pop gs
pop word [bx]
xchg ax - bx
xchg bx - ax
xchg bx - cx
xchg al - bl
xchg [bx] - ax
xchg ax - [bx]
xchg eax - ecx
lea ax - [bx+si+4]
lea eax - [eax+ebx*2]
lea si - [lvar]
lds si - [bx]
les di - [lvar]
lss sp - [bx]
lfs ax - [bx]
lgs eax - [bx]
movzx ax - al
movzx eax - byte [bx]
movzx eax - word [bx]
movzx eax - bx
movsx ax - bl
movsx eax - cx
jmp lback
jmp lfwd
jmp lfar
jmp short lfwd
jmp near lfwd
jmp $
jmp ax
jmp word [bx]
jmp far [bx]
jmp 0x08:lfwd
jmp 0x1000:0
jmp dword 0x08:lfwd
call lfar
call lback
call ax
call word [bx]
call far [bx]
call 0x1000:0x0
jz lback
jnz lfwd
jc lfar
jne lfar
jg lback
jl lfwd
ja lfwd
jb lfwd
jae lfwd
jbe lfwd
jge lfar
jle lfar
js lfwd
jns lfwd
jo lfwd
jno lfwd
jp lfwd
jnp lfwd
je lback
jna lfwd
jcxz lback
jecxz lback
loop lback
loope lback
loopne lback
loopz lback
ret
ret 4
retf
retf 2
iret
iretd
int 0x10
int 3
int3
into
in al - 0x60
in ax - dx
in eax - 0x60
in al - dx
out 0x64 - al
out dx - al
out dx - ax
out 0x80 - eax
enter 16 - 0
leave
lgdt [lvar]
lidt [bx]
sgdt [lvar]
sidt [bx]
lmsw ax
smsw ax
lldt ax
ltr ax
sldt ax
str ax
invlpg [bx]
sete al
setnz byte [bx]
setg bl
cmovz ax - bx
cmovne eax - [bx]
bt ax - 3
bt ax - bx
bts word [bx] - 1
btr eax - ecx
btc ax - 15
bsf ax - bx
bsr eax - [bx]
bswap eax
nop
hlt
cli
sti
cld
std
clc
stc
cmc
lahf
sahf
xlatb
wait
cpuid
rdtsc
rdmsr
wrmsr
wbinvd
invd
clts
ud2
pause
daa
das
aaa
aas
pusha
popa
pushad
popad
pushf
popf
pushfd
popfd
cbw
cwde
cwd
cdq
movsb
movsw
movsd
cmpsb
cmpsw
cmpsd
stosb
stosw
stosd
lodsb
lodsw
lodsd
scasb
scasw
scasd
insb
insw
insd
outsb
outsw
outsd
rep movsb
rep stosw
repe cmpsb
repne scasb
rep movsd
lock add [bx] - ax
nxtb
nxtw
nxtd
chk al - al
chk ax - 1
jfnz lback
jfz lfwd
jfc lfar
jfge lfwd
mov ax - [bp+0x80]
mov ax - [bp-0x80]
mov ax - [bx+127]
mov ax - [bx+128]
mov ax - [bx-129]
mov ax - [si+lfar-lback]
mov ax - [bx+lvar]
mov eax - [ebx+0x80]
mov eax - [ebx-128]
mov eax - [esp+ebp*2]
mov eax - [ebp+eax*8+0x1000]
add word [lvar] - 1000
cmp byte [lvar] - 7
mov [es:lvar] - ax
lea ax - [bx+lfar]
push word [bp+6]
""".strip().splitlines()

LINES_32 = """
mov eax - ebx
mov ax - bx
mov al - 5
mov eax - 0x12345678
mov ax - 0x1234
mov eax - [ebx]
mov eax - [lvar]
mov [lvar] - eax
mov ax - [lvar]
mov eax - [ebx+esi*8-4]
mov eax - [esp+8]
mov eax - [ebp]
mov eax - [bx]
mov ax - [bx+si]
mov dword [eax] - 1
mov word [eax] - 1
mov byte [eax] - 1
mov ds - ax
mov ds - eax
mov ax - ds
mov eax - ds
mov eax - cr0
mov cr0 - eax
mov eax - [es:edi]
add eax - 5
add eax - 1000
add ax - 5
add ax - 1000
add esp - 16
sub dword [ebx] - 2
cmp eax - [esi]
xor eax - eax
test eax - eax
test ax - ax
inc eax
inc ax
dec ecx
dec cx
push eax
push ax
push 5
push 0x12345678
push word 5
push dword [ebx]
push word [ebx]
push es
push fs
pop eax
pop ax
pop ds
pop gs
xchg eax - ebx
xchg ax - bx
lea eax - [ebx+ecx*4]
lea ax - [ebx+4]
movzx eax - al
movzx eax - word [ebx]
movsx eax - byte [ebx]
jmp lback
jmp lfwd
jmp lfar
jmp eax
jmp dword [ebx]
jmp 0x08:lfwd
jmp word 0x08:lfwd
call lfar
call eax
call dword [ebx]
jz lback
jnz lfar
jcxz lback
jecxz lback
loop lback
ret
ret 8
iret
iretd
pusha
pushad
popa
popad
pushf
pushfd
cbw
cwde
cwd
cdq
lodsb
lodsw
lodsd
stosw
stosd
movsw
rep movsd
rep stosb
in al - dx
in ax - dx
in eax - dx
out dx - al
out dx - ax
shl eax - 2
shr ax - 1
imul eax - ebx - 100
imul eax - ebx - 10
bswap eax
lgdt [lvar]
lidt [ebx]
lldt ax
ltr ax
sete al
cmovz eax - ebx
bt eax - 31
mov eax - [ebp+lvar]
mov ax - [fs:lvar]
mov eax - [ecx*8]
mov eax - [ecx*2+5]
lea eax - [eax+eax*4]
cmp dword [ebx+lfar-lback] - -2
""".strip().splitlines()


def gasem_program(line, bits):
    return (f"og 0x7C00\nb {bits}\nlback:\n{line}\nlfwd:\n&& 300 nop\n"
            f"lfar:\nlvar w: 0\n")


def nasm_program(line, bits):
    return (f"org 0x7C00\nbits {bits}\nlback:\n{to_nasm(line)}\nlfwd:\ntimes 300 nop\n"
            f"lfar:\nlvar dw 0\n")


def nasm_bytes(src):
    with tempfile.TemporaryDirectory() as d:
        asm = os.path.join(d, "t.asm")
        out = os.path.join(d, "t.bin")
        with open(asm, "w") as f:
            f.write(src)
        r = subprocess.run([NASM, "-f", "bin", "-w-all", asm, "-o", out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"NASM не принял программу:\n{src}\n{r.stderr}")
        with open(out, "rb") as f:
            return f.read()


@unittest.skipUnless(NASM, "nasm не установлен")
class NasmDifferentialTest(unittest.TestCase):
    def check(self, lines, bits):
        for line in lines:
            with self.subTest(bits=bits, line=line):
                ours = compile_source(gasem_program(line, bits)).code
                ref = nasm_bytes(nasm_program(line, bits))
                self.assertEqual(ours.hex(" "), ref.hex(" "), f"b {bits}: {line}")

    def test_16bit(self):
        self.check(LINES_16, 16)

    def test_32bit(self):
        self.check(LINES_32, 32)

    def test_hello_bootloader(self):
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "examples", "hello.gsm"), encoding="utf-8") as f:
            ours = compile_source(f.read()).code
        ref = nasm_bytes(
            "org 0x7C00\nbits 16\nxor ax, ax\nmov ds, ax\nmov si, msg\nprint:\nmov ah, 0x0E\n"
            "lodsb\nint 0x10\ntest al, al\njnz print\njmp $\nmsg db 'Hello', 0\n"
            "times 510-($-$$) db 0\ndw 0xAA55\n")
        self.assertEqual(ours, ref)
        self.assertEqual(len(ours), 512)


if __name__ == "__main__":
    unittest.main()
