package gasem

// gasem debug — запуск программы в QEMU под отладчиком GDB.
//
// QEMU стартует остановленным и ждёт GDB. GDB получает от Gasem таблицу меток
// и строк исходника, поэтому точки останова ставятся по именам меток, а при
// каждой остановке печатается строка .gsm, на которой стоит процессор.
//
// Команды, которые добавляются в GDB:
//
//	gbreak метка     точка останова на метке
//	gstep            выполнить одну строку исходника (даже если в ней несколько команд)
//	gwhere           показать текущую строку исходника

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode"
)

// pyRepr — запись строки в виде литерала Python (как repr()).
func pyRepr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch {
		case r == quote || r == '\\':
			b.WriteRune('\\')
			b.WriteRune(r)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r < ' ' || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x7f || unicode.IsPrint(r):
			b.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}

type gdbLine struct {
	addr       *big.Int
	size, bits int
	file       string
	line       int
	text       string
}

// GdbScript — сценарий для GDB: подключение к QEMU, таблицы программы, точки останова.
func GdbScript(res *Result, port int, breaks []string, batch bool) string {
	var syms []string
	for _, s := range res.Symbols {
		if !strings.HasPrefix(s.Name, "@") {
			syms = append(syms, pyRepr(s.Name)+": "+s.Value.String())
		}
	}
	lines := make([]gdbLine, len(res.Lines))
	for i := range res.Lines {
		l := res.Lines[i]
		lines[i] = gdbLine{l.Addr, l.Size, l.Bits, basename(l.Loc.File), l.Loc.Line, l.Loc.Text}
	}
	sort.SliceStable(lines, func(i, j int) bool {
		a, b := lines[i], lines[j]
		if c := a.addr.Cmp(b.addr); c != 0 {
			return c < 0
		}
		if a.size != b.size {
			return a.size < b.size
		}
		if a.bits != b.bits {
			return a.bits < b.bits
		}
		if a.file != b.file {
			return a.file < b.file
		}
		if a.line != b.line {
			return a.line < b.line
		}
		return a.text < b.text
	})
	var rows []string
	for _, l := range lines {
		rows = append(rows, fmt.Sprintf("(%s, %d, %d, %s, %d, %s)", l.addr, l.size, l.bits,
			pyRepr(l.file), l.line, pyRepr(l.text)))
	}
	helper := strings.ReplaceAll(gdbHelper, "__SYMBOLS__", "{"+strings.Join(syms, ", ")+"}")
	helper = strings.ReplaceAll(helper, "__LINES__", "["+strings.Join(rows, ", ")+"]")
	firstBits := 16
	if len(lines) > 0 {
		firstBits = lines[0].bits
	}
	arch := "i386"
	if firstBits == 16 {
		arch = "i8086"
	}
	out := []string{
		"set pagination off",
		"set confirm off",
		"set disassembly-flavor intel",
		"set architecture " + arch,
		fmt.Sprintf("target remote localhost:%d", port),
		"python",
		helper,
		"end",
	}
	for _, name := range breaks {
		out = append(out, "gbreak "+name)
	}
	if batch {
		out = append(out, "continue", "x/3i $pc", "kill")
	} else {
		out = append(out, `echo \nGasem: gbreak <метка>, gstep, gwhere; continue — продолжить, q — выход\n`)
	}
	return strings.Join(out, "\n") + "\n"
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

// FindQemu — путь к QEMU для x86 ("" — если не установлен).
func FindQemu() string {
	for _, name := range []string{"qemu-system-i386", "qemu-system-x86_64"} {
		if path, err := exec.LookPath(name); err == nil {
			return path
		}
	}
	return ""
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return ee.ExitCode()
	}
	return 1
}

// Debug запускает образ image в QEMU под GDB и возвращает код выхода GDB.
// В режиме batch GDB ждёт точку останова не дольше timeout.
func Debug(res *Result, image string, breaks []string, batch bool, qemuArgs []string, timeout time.Duration) (int, error) {
	qemu := FindQemu()
	if qemu == "" {
		return 1, errors.New("QEMU не найден (нужен qemu-system-i386)")
	}
	gdbPath, err := exec.LookPath("gdb")
	if err != nil {
		return 1, errors.New("GDB не найден (установите gdb)")
	}
	symbols := res.SymbolMap()
	for _, name := range breaks {
		if _, ok := symbols[name]; !ok {
			return 1, fmt.Errorf("неизвестная метка '%s'", name)
		}
	}
	port, err := freePort()
	if err != nil {
		return 1, err
	}
	args := []string{"-drive", "format=raw,file=" + image, "-S", "-gdb", fmt.Sprintf("tcp:127.0.0.1:%d", port), "-no-reboot"}
	args = append(args, qemuArgs...)
	if batch {
		args = append(args, "-display", "none")
	}
	q := exec.Command(qemu, args...)
	detach(q)
	if err := q.Start(); err != nil {
		return 1, err
	}
	defer func() {
		_ = q.Process.Kill()
		_ = q.Wait()
	}()

	tmp, err := os.MkdirTemp("", "gasem")
	if err != nil {
		return 1, err
	}
	defer os.RemoveAll(tmp)
	script := filepath.Join(tmp, "gasem.gdb")
	if err := os.WriteFile(script, []byte(GdbScript(res, port, breaks, batch)), 0o644); err != nil {
		return 1, err
	}

	if !batch {
		// Ctrl+C нужен GDB (остановить программу), а не нам: перехватываем его
		// и ничего не делаем — у GDB, в отличие от игнорирования, сигнал не пропадёт
		interrupts := make(chan os.Signal, 1)
		signal.Notify(interrupts, os.Interrupt)
		defer signal.Stop(interrupts)
		g := exec.Command(gdbPath, "-q", "-x", script)
		g.Stdin, g.Stdout, g.Stderr = os.Stdin, os.Stdout, os.Stderr
		return exitCode(g.Run()), nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	g := exec.CommandContext(ctx, gdbPath, "-q", "-batch", "-x", script)
	g.Stdin, g.Stdout, g.Stderr = os.Stdin, os.Stdout, os.Stderr
	err = g.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return 1, fmt.Errorf("за %d с программа не дошла до точки останова", int(timeout.Seconds()))
	}
	return exitCode(err), nil
}
