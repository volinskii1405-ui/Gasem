// Command gasem — компилятор языка Gasem.
//
//	gasem программа.gsm [-o файл.bin] [-l листинг] [-m карта]   собрать
//	gasem build программа.gsm ...                                то же самое
//	gasem run программа.gsm                                      собрать и запустить в QEMU
//	gasem debug программа.gsm -b метка                           запустить под отладчиком GDB
//	gasem fmt файлы.gsm [--check] [--diff]                       выровнять оформление
package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"runtime/debug"
	"strings"
	"time"

	"github.com/volinskii1405-ui/Gasem/go/gasem"
	"github.com/volinskii1405-ui/Gasem/go/lsp"
)

func main() {
	debug.SetGCPercent(400) // компилятор работает доли секунды — сборка мусора почти не нужна
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func plural(n int, one, few, many string) string {
	switch {
	case n%10 == 1 && n%100 != 11:
		return one
	case n%10 >= 2 && n%10 <= 4 && !(n%100 >= 12 && n%100 <= 14):
		return few
	}
	return many
}

// textFile — текст для записи в файл (в Windows — с переводами строк \r\n, как у Python).
func textFile(s string) []byte {
	if runtime.GOOS == "windows" {
		s = strings.ReplaceAll(s, "\n", "\r\n")
	}
	return []byte(s)
}

// ---------------------------------------------------------------- сборка

func buildActions(extra ...*action) []*action {
	acts := []*action{
		{opts: []string{"-h", "--help"}, dest: "help", nargs: "0", kind: "help"},
		{dest: "input", nargs: "1", kind: "store"},
		{opts: []string{"-o", "--output"}, dest: "output", nargs: "1", kind: "store"},
		{opts: []string{"-w", "--no-warnings"}, dest: "no_warnings", nargs: "0", kind: "true"},
		{opts: []string{"-q", "--quiet"}, dest: "quiet", nargs: "0", kind: "true"},
	}
	return append(acts, extra...)
}

type cli struct {
	stdout, stderr io.Writer
}

// compile собирает программу и записывает файлы. Возвращает результат и путь к образу (nil — ошибка).
func (c *cli) compile(args *parsedArgs) (*gasem.Result, string) {
	input := args.str("input")
	res, errs := gasem.CompileFile(input)
	if errs != nil {
		for _, e := range errs.List {
			fmt.Fprintln(c.stderr, e.Format())
		}
		n := len(errs.List)
		fmt.Fprintf(c.stderr, "gasem: компиляция не удалась (%d %s)\n", n, plural(n, "ошибка", "ошибки", "ошибок"))
		return nil, ""
	}
	if !args.flags["no_warnings"] {
		for _, w := range res.Warnings {
			fmt.Fprintln(c.stderr, w.Format())
		}
	}
	output := args.str("output")
	if output == "" {
		output = gasem.SplitExtRoot(input) + ".bin"
	}
	writes := []struct {
		path string
		data []byte
	}{{output, res.Code}}
	if path := args.str("listing"); path != "" {
		writes = append(writes, struct {
			path string
			data []byte
		}{path, textFile(res.Listing())})
	}
	if path := args.str("map"); path != "" {
		var b strings.Builder
		b.WriteString("; адрес     имя\n")
		for _, s := range res.SortedSymbols() {
			fmt.Fprintf(&b, "%s  %s\n", gasem.FormatSymbol(s.Value), s.Name)
		}
		writes = append(writes, struct {
			path string
			data []byte
		}{path, textFile(b.String())})
	}
	for _, w := range writes {
		if err := os.WriteFile(w.path, w.data, 0o666); err != nil {
			fmt.Fprintf(c.stderr, "gasem: не удалось записать '%s': %s\n", w.path, gasem.Strerror(err))
			return nil, ""
		}
	}
	if !args.flags["quiet"] {
		n := len(res.Code)
		fmt.Fprintf(c.stdout, "gasem: %s -> %s (%d %s)\n", input, output, n, plural(n, "байт", "байта", "байт"))
	}
	return res, output
}

func (c *cli) build(argv []string) int {
	p := &argParser{prog: "gasem build", help: helpBuild, actions: buildActions(
		&action{opts: []string{"-l", "--listing"}, dest: "listing", nargs: "1", kind: "store"},
		&action{opts: []string{"-m", "--map"}, dest: "map", nargs: "1", kind: "store"},
		&action{opts: []string{"--run"}, dest: "run", nargs: "0", kind: "true"},
	)}
	args, code, done := p.parse(argv, c.stdout, c.stderr)
	if done {
		return code
	}
	res, output := c.compile(args)
	if res == nil {
		return 1
	}
	if args.flags["run"] {
		return c.runQemu(output, nil)
	}
	return 0
}

// checkBootable предупреждает, если BIOS не станет загружать такой образ.
func (c *cli) checkBootable(res *gasem.Result) {
	code := res.Code
	if res.Origin.Cmp(bigInt(0x7C00)) == 0 && (len(code) < 512 || code[510] != 0x55 || code[511] != 0xAA) {
		fmt.Fprintln(c.stderr, "gasem: предупреждение: образ не загрузочный — в конце первого сектора нет "+
			"сигнатуры 0xAA55 (добавьте: && 510-($-$$) b: 0 / w: 0xAA55)")
	}
}

func (c *cli) runQemu(image string, extra []string) int {
	qemu := gasem.FindQemu()
	if qemu == "" {
		fmt.Fprintln(c.stderr, "gasem: QEMU не найден (нужен qemu-system-i386 или qemu-system-x86_64)")
		return 1
	}
	cmd := exec.Command(qemu, append([]string{"-drive", "format=raw,file=" + image}, extra...)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		fmt.Fprintf(c.stderr, "gasem: %s\n", err)
		return 1
	}
	return 0
}

func (c *cli) run(argv []string) int {
	p := &argParser{prog: "gasem run", help: helpRun, actions: buildActions(
		&action{dest: "qemu_args", nargs: "*", kind: "store"},
	)}
	args, code, done := p.parse(argv, c.stdout, c.stderr)
	if done {
		return code
	}
	res, output := c.compile(args)
	if res == nil {
		return 1
	}
	c.checkBootable(res)
	return c.runQemu(output, args.list("qemu_args"))
}

func (c *cli) debug(argv []string) int {
	p := &argParser{prog: "gasem debug", help: helpDebug, actions: buildActions(
		&action{opts: []string{"-b", "--break"}, dest: "breaks", nargs: "1", kind: "append"},
		&action{opts: []string{"--batch"}, dest: "batch", nargs: "0", kind: "true"},
	)}
	args, code, done := p.parse(argv, c.stdout, c.stderr)
	if done {
		return code
	}
	res, output := c.compile(args)
	if res == nil {
		return 1
	}
	c.checkBootable(res)
	rc, err := gasem.Debug(res, output, args.list("breaks"), args.flags["batch"], nil, 30*time.Second)
	if err != nil {
		fmt.Fprintf(c.stderr, "gasem: %s\n", err)
		return 1
	}
	return rc
}

// ---------------------------------------------------------------- fmt

func (c *cli) fmt(argv []string) int {
	p := &argParser{prog: "gasem fmt", help: helpFmt, actions: []*action{
		{opts: []string{"-h", "--help"}, dest: "help", nargs: "0", kind: "help"},
		{dest: "files", nargs: "+", kind: "store"},
		{opts: []string{"--check"}, dest: "check", nargs: "0", kind: "true"},
		{opts: []string{"--diff"}, dest: "diff", nargs: "0", kind: "true"},
	}}
	args, code, done := p.parse(argv, c.stdout, c.stderr)
	if done {
		return code
	}
	check, diff := args.flags["check"], args.flags["diff"]
	unformatted := 0
	for _, path := range args.list("files") {
		data, err := os.ReadFile(path)
		if err != nil {
			fmt.Fprintf(c.stderr, "gasem: не удалось открыть '%s': %s\n", path, gasem.Strerror(err))
			return 1
		}
		if !utf8Valid(data) {
			fmt.Fprintf(c.stderr, "gasem: файл '%s' не в кодировке UTF-8\n", path)
			return 1
		}
		text := gasem.UniversalNewlines(strings.TrimPrefix(string(data), "\ufeff"))
		formatted := gasem.FormatText(text)
		if formatted == text {
			continue
		}
		unformatted++
		switch {
		case diff:
			for _, line := range gasem.UnifiedDiff(gasem.SplitLinesKeep(text), gasem.SplitLinesKeep(formatted),
				path, path+" (gasem fmt)") {
				fmt.Fprint(c.stdout, line)
			}
		case check:
			fmt.Fprintf(c.stdout, "нужно выровнять: %s\n", path)
		default:
			if err := os.WriteFile(path, textFile(formatted), 0o666); err != nil {
				fmt.Fprintf(c.stderr, "gasem: не удалось записать '%s': %s\n", path, gasem.Strerror(err))
				return 1
			}
			fmt.Fprintf(c.stdout, "выровнено: %s\n", path)
		}
	}
	if (check || diff) && unformatted > 0 {
		return 1
	}
	return 0
}

// ---------------------------------------------------------------- main

var usage = "gasem " + gasem.Version + ` — компилятор языка Gasem

  gasem программа.gsm [-o файл.bin] [-l листинг] [-m карта]   собрать
  gasem run программа.gsm                                      собрать и запустить в QEMU
  gasem debug программа.gsm -b метка                           запустить под отладчиком GDB
  gasem fmt файлы.gsm [--check] [--diff]                       выровнять оформление

Подробнее: gasem <команда> --help
`

func run(argv []string, stdout, stderr io.Writer) int {
	c := &cli{stdout, stderr}
	if len(argv) == 0 || argv[0] == "-h" || argv[0] == "--help" {
		fmt.Fprintln(stdout, usage)
		if len(argv) == 0 {
			return 2
		}
		return 0
	}
	if argv[0] == "-V" || argv[0] == "--version" {
		fmt.Fprintln(stdout, "gasem "+gasem.Version)
		return 0
	}
	switch argv[0] {
	case "build":
		return c.build(argv[1:])
	case "run":
		return c.run(argv[1:])
	case "debug":
		return c.debug(argv[1:])
	case "fmt":
		return c.fmt(argv[1:])
	case "lsp": // языковой сервер для редактора (VS Code запускает его сам)
		if err := lsp.Serve(os.Stdin, os.Stdout); err != nil {
			fmt.Fprintf(stderr, "gasem lsp: %s\n", err)
			return 1
		}
		return 0
	}
	return c.build(argv)
}
