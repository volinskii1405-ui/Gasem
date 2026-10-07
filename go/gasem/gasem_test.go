package gasem

// Основная проверка Go-версии — весь набор тестов Python-версии (tests/,
// запуск с GASEM_IMPL=go) и сверка обеих версий (tests/test_go.py).
// Здесь — быстрые проверки, которые работают и без Python.

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func compile(t *testing.T, src string) *Result {
	t.Helper()
	res, errs := CompileSource(src, "", "")
	if errs != nil {
		t.Fatalf("ошибки компиляции:\n%s", errs.Format())
	}
	return res
}

func compileErr(t *testing.T, src string) string {
	t.Helper()
	_, errs := CompileSource(src, "", "")
	if errs == nil {
		t.Fatalf("ожидалась ошибка:\n%s", src)
	}
	return errs.Format()
}

func TestHelloFromDocs(t *testing.T) {
	res, errs := CompileFile("../../examples/hello.gsm")
	if errs != nil {
		t.Fatal(errs.Format())
	}
	if len(res.Code) != 512 || !bytes.Equal(res.Code[510:], []byte{0x55, 0xAA}) {
		t.Fatalf("загрузочный сектор: %d байт, конец % X", len(res.Code), res.Code[len(res.Code)-2:])
	}
	if res.Origin.Int64() != 0x7C00 {
		t.Fatalf("og = %s", res.Origin)
	}
}

func TestEncoding(t *testing.T) {
	cases := map[string]string{
		"mov ax - 1":                    "B8 01 00",
		"b 32\nmov eax - [ebx+esi*4+8]": "8B 44 B3 08",
		"mov byte [bx] - 'A'":           "C6 07 41",
		"add ax - 0x7F":                 "83 C0 7F",
		"push dword 1":                  "66 6A 01",
		"x: jmp x":                      "EB FE",
		"mov a20 - 1":                   "50 E4 92 0C 02 24 FE E6 92 58",
		"nxtb\nchk al - 1":              "AC A8 01",
		"jfnz $":                        "75 FE",
		"b: \"Hi\" - 0\nw: 0xAA55":      "48 69 00 55 AA",
		"&& 3 b: 7":                     "07 07 07",
		"do \"6*7\" - cx":               "B9 2A 00",
	}
	for src, want := range cases {
		got := fmt.Sprintf("% X", compile(t, src).Code)
		if got != want {
			t.Errorf("%q: %s, ожидалось %s", src, got, want)
		}
	}
}

func TestHighLevel(t *testing.T) {
	res := compile(t, "start:\nfor cx - 0 - 10\n    if cx = 5\n        break\n    end\nend\nlet ax - \"bx * 3 + 1\"\n")
	if len(res.Code) == 0 {
		t.Fatal("пустой код")
	}
}

func TestErrorMessages(t *testing.T) {
	cases := map[string]string{
		"mvo ax - 1":                    "неизвестная команда 'mvo' — может быть, 'mov'?",
		"mov ax, 1":                     "операнды разделяются ' - ' (дефис с пробелами), а не запятой",
		"start:\njmp prnit\nprint: ret": "неизвестное имя 'prnit' — может быть, 'print'?",
		"if al = 1":                     "if без end",
		"mov al - \"a\"":                "строка в двойных кавычках — это адрес строки",
		"b: 300":                        "значение 300 не помещается в b: (8 бит)",
	}
	for src, want := range cases {
		if got := compileErr(t, src); !strings.Contains(got, want) {
			t.Errorf("%q:\n%s\nожидалось: %s", src, got, want)
		}
	}
	got := compileErr(t, "mov ax -\t[bx + cl]")
	want := "<источник>:1: ошибка: регистр cl нельзя использовать в адресе\n    mov ax -    [bx + cl]\n                      ^"
	if got != want {
		t.Errorf("формат сообщения:\n%s\nожидалось:\n%s", got, want)
	}
}

func TestWarnings(t *testing.T) {
	res := compile(t, "start:\nunused: ret\n")
	if len(res.Warnings) != 1 || !strings.Contains(res.Warnings[0].Format(), "метка 'unused' нигде не используется") {
		t.Fatalf("предупреждения: %v", res.Warnings)
	}
}

func TestGasemOS(t *testing.T) {
	res, errs := CompileFile("../../os/gasemos.gsm")
	if errs != nil {
		t.Fatal(errs.Format())
	}
	if len(res.Code) != 166912 {
		t.Fatalf("размер образа %d", len(res.Code))
	}
	if !bytes.Equal(res.Code[64*512:64*512+8], []byte("GASEMFS1")) {
		t.Fatal("нет суперблока GasemFS в секторе 64")
	}
}

func TestFormatterKeepsSources(t *testing.T) {
	files, _ := filepath.Glob("../../os/*.gsm")
	more, _ := filepath.Glob("../../examples/*.gsm")
	for _, path := range append(files, more...) {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		text := string(data)
		if got := FormatText(text); got != text {
			t.Errorf("%s: gasem fmt меняет файл из репозитория", path)
		}
	}
}

func TestPythonCompat(t *testing.T) {
	if got := splitLines("a\r\nb\rc\x0bd\u2028e\n"); strings.Join(got, "|") != "a|b|c|d|e" {
		t.Errorf("splitLines: %q", got)
	}
	if got := expandTabs("a\tbc\td", 4); got != "a   bc  d" {
		t.Errorf("expandTabs: %q", got)
	}
	if got := decodeReplace([]byte("a\xe2\x82b\xffc")); got != "a\ufffdb\ufffdc" {
		t.Errorf("decodeReplace: %q", got)
	}
	for word, want := range map[string]int64{"0FFh": 255, "0x7C00": 0x7C00, "0b101": 5, "1_000": 1000, "0x0x10": 16} {
		if v, ok := ParseNumber(word); !ok || v.Int64() != want {
			t.Errorf("ParseNumber(%q) = %v", word, v)
		}
	}
}

func BenchmarkGasemOS(b *testing.B) {
	for i := 0; i < b.N; i++ {
		if _, errs := CompileFile("../../os/gasemos.gsm"); errs != nil {
			b.Fatal(errs.Format())
		}
	}
}
