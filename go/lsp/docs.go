package lsp

import (
	"fmt"
	"strings"

	"github.com/volinskii1405-ui/Gasem/go/gasem"
)

// Краткие описания слов Gasem и частых команд x86 (для подсказки при наведении).
var wordDocs = map[string]string{
	// Gasem
	"og":       "og адрес — адрес, с которого программа работает в памяти (как org в NASM)",
	"b":        "b 16 / b 32 / b 64 — режим процессора; b: — данные по байту",
	"nxtb":     "nxtb — следующий байт строки: al = [esi], esi + 1 (lodsb)",
	"nxtw":     "nxtw — следующее слово: ax = [esi], esi + 2 (lodsw)",
	"nxtd":     "nxtd — следующее двойное слово: eax = [esi], esi + 4 (lodsd)",
	"nxtq":     "nxtq — следующее 64-битное значение: rax = [rsi], rsi + 8 (lodsq)",
	"chk":      "chk a - b — проверить биты: флаги по a & b, сами операнды не меняются (test)",
	"do":       "do \"выражение\" - приёмник — вычислить при компиляции (числа и строки)",
	"let":      "let приёмник - \"выражение\" — вычислить во время работы; меняется только приёмник",
	"if":       "if условие … elif … else … end — ветвление",
	"elif":     "elif условие — ещё одна ветка if",
	"else":     "else — ветка if, если условия не выполнились",
	"end":      "end — конец блока if, while, for, macro, proc, struct, at",
	"while":    "while условие … end — цикл, пока условие истинно",
	"for":      "for счётчик - начало - конец [- шаг] … end — цикл со счётчиком (конец не включается)",
	"repeat":   "repeat … until условие — цикл с проверкой в конце",
	"until":    "until условие — конец цикла repeat",
	"break":    "break — выйти из цикла (break if условие)",
	"continue": "continue — к следующему шагу цикла",
	"macro":    "macro имя - параметры … end — макрос: текст подставляется в место вызова",
	"proc":     "proc имя - регистры аргументов uses сохраняемые регистры … end — процедура",
	"local":    "local имя - тип — локальная переменная процедуры (в стеке)",
	"return":   "return [значение] — выйти из процедуры (значение — в eax/rax)",
	"struct":   "struct Имя … end — структура: поля Имя.поле и размер Имя.size",
	"at":       "at адрес … end — код работает по этому адресу, хотя лежит в файле в другом месте",
	"pool":     "pool — сюда кладутся строки из команд вида call puts - \"текст\"",
	"args":     "args имя - регистры — в какие регистры call передаёт аргументы",
	"include":  "include \"файл\" — подключить другой файл .gsm",
	"incbin":   "incbin \"файл\" [- смещение [- длина]] — вставить двоичный файл",
	"align":    "align N [- байт] — выровнять адрес на N",
	"equ":      "ИМЯ equ выражение — константа (то же, что ИМЯ = выражение)",
	"uses":     "uses регистры — proc сохраняет их в стеке и восстанавливает перед выходом",
	"signed":   "signed — сравнивать или считать числа со знаком",
	"a20":      "a20 — линия адреса A20: mov a20 - 1 включает, mov a20 - 0 выключает",
	"rel":      "[rel метка] — адрес относительно rip (в режиме b 64 так кодируются метки)",
	"abs":      "[abs адрес] — абсолютный адрес в режиме b 64",
	// x86
	"mov":     "mov приёмник - источник — скопировать значение",
	"add":     "add a - b — a = a + b",
	"sub":     "sub a - b — a = a - b",
	"cmp":     "cmp a - b — сравнить (флаги как у a - b)",
	"inc":     "inc a — a = a + 1",
	"dec":     "dec a — a = a - 1",
	"and":     "and a - b — побитовое И",
	"or":      "or a - b — побитовое ИЛИ",
	"xor":     "xor a - b — исключающее ИЛИ (xor eax - eax обнуляет eax)",
	"not":     "not a — инвертировать биты",
	"neg":     "neg a — сменить знак",
	"test":    "test a - b — флаги по a & b (в Gasem то же — chk)",
	"mul":     "mul b — беззнаковое умножение: edx:eax = eax * b",
	"imul":    "imul — умножение со знаком: imul r - a [- число]",
	"div":     "div b — беззнаковое деление edx:eax на b: eax — частное, edx — остаток",
	"idiv":    "idiv b — деление со знаком",
	"shl":     "shl a - n — сдвиг влево",
	"shr":     "shr a - n — сдвиг вправо (без знака)",
	"sar":     "sar a - n — сдвиг вправо со знаком",
	"lea":     "lea r - [адрес] — вычислить адрес (без чтения памяти)",
	"push":    "push a — положить в стек (можно списком: push eax - ebx)",
	"pop":     "pop a — снять со стека (списком снимает в обратном порядке)",
	"call":    "call метка [- аргументы] — вызвать подпрограмму",
	"ret":     "ret — вернуться из подпрограммы",
	"jmp":     "jmp метка — перейти",
	"int":     "int n — программное прерывание (int 0x10 — видео BIOS)",
	"iret":    "iret — вернуться из обработчика прерывания",
	"in":      "in al - порт — прочитать из порта ввода-вывода",
	"out":     "out порт - al — записать в порт ввода-вывода",
	"hlt":     "hlt — остановить процессор до прерывания",
	"cli":     "cli — запретить прерывания",
	"sti":     "sti — разрешить прерывания",
	"nop":     "nop — ничего не делать",
	"lodsb":   "lodsb — al = [esi], esi + 1 (в Gasem — nxtb)",
	"stosb":   "stosb — [edi] = al, edi + 1",
	"stosw":   "stosw — [edi] = ax, edi + 2",
	"movsb":   "movsb — [edi] = [esi], оба + 1",
	"rep":     "rep — повторить строковую команду ecx раз",
	"loop":    "loop метка — ecx = ecx - 1, перейти, если ecx ≠ 0",
	"lgdt":    "lgdt [адрес] — загрузить таблицу дескрипторов (GDT)",
	"lidt":    "lidt [адрес] — загрузить таблицу прерываний (IDT)",
	"cpuid":   "cpuid — сведения о процессоре",
	"syscall": "syscall — системный вызов (режим b 64)",
	"movzx":   "movzx r - a — скопировать с расширением нулями",
	"movsx":   "movsx r - a — скопировать с расширением знаком",
	"movsxd":  "movsxd r64 - r32 — расширить 32 бита до 64 со знаком",
	"xchg":    "xchg a - b — обменять значения",
}

// describeWord — описание команды, ключевого слова или регистра (Markdown) или "".
func describeWord(word string) string {
	low := strings.ToLower(word)
	if r, ok := gasem.Registers[low]; ok {
		kinds := map[string]string{"gpr": "регистр общего назначения", "seg": "сегментный регистр",
			"cr": "управляющий регистр", "dr": "отладочный регистр"}
		out := fmt.Sprintf("**%s** — %s, %d бит", r.Name, kinds[r.Kind], r.Size)
		if r.X64 {
			out += " (только в режиме b 64)"
		}
		return out
	}
	if strings.HasPrefix(low, "jf") {
		if c := gasem.Canonical(low); c != "" {
			return fmt.Sprintf("**%s** — то же, что %s: условный переход", low, c)
		}
	}
	if d, ok := wordDocs[low]; ok {
		parts := strings.SplitN(d, " — ", 2)
		return "```gasem\n" + parts[0] + "\n```\n" + parts[1]
	}
	if c := gasem.Canonical(low); c != "" {
		if strings.HasPrefix(c, "j") {
			return fmt.Sprintf("**%s** — условный переход", low)
		}
		return fmt.Sprintf("**%s** — команда x86", low)
	}
	return ""
}
