package lsp

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// client — тестовый клиент: говорит с сервером так же, как редактор.
type client struct {
	t     *testing.T
	conn  *conn
	id    int
	diags chan map[string]any
	resp  chan *message
}

func startServer(t *testing.T) *client {
	t.Helper()
	inR, inW := io.Pipe()   // клиент → сервер
	outR, outW := io.Pipe() // сервер → клиент
	go func() {
		_ = Serve(inR, outW)
		outW.Close()
	}()
	c := &client{t: t, conn: newConn(outR, inW), diags: make(chan map[string]any, 100), resp: make(chan *message, 10)}
	go func() {
		for {
			m, err := c.conn.read()
			if err != nil {
				return
			}
			if m.Method == "textDocument/publishDiagnostics" {
				var p map[string]any
				_ = json.Unmarshal(m.Params, &p)
				c.diags <- p
			} else if len(m.ID) > 0 {
				c.resp <- m
			}
		}
	}()
	t.Cleanup(func() {
		_ = c.conn.notify("exit", nil)
		inW.Close()
	})
	return c
}

func (c *client) request(method string, params any) json.RawMessage {
	c.t.Helper()
	c.id++
	data, _ := json.Marshal(params)
	if err := c.conn.write(&message{ID: json.RawMessage(fmt.Sprint(c.id)), Method: method, Params: data}); err != nil {
		c.t.Fatal(err)
	}
	select {
	case m := <-c.resp:
		if m.Error != nil {
			c.t.Fatalf("%s: ошибка %d: %s", method, m.Error.Code, m.Error.Message)
		}
		return m.Result
	case <-time.After(10 * time.Second):
		c.t.Fatalf("%s: нет ответа", method)
	}
	return nil
}

func (c *client) notify(method string, params any) {
	if err := c.conn.notify(method, params); err != nil {
		c.t.Fatal(err)
	}
}

// diagnostics ждёт диагностику для файла path.
func (c *client) diagnostics(path string) []map[string]any {
	c.t.Helper()
	uri := pathToURI(path)
	deadline := time.After(10 * time.Second)
	for {
		select {
		case p := <-c.diags:
			if p["uri"] == uri {
				var out []map[string]any
				for _, d := range p["diagnostics"].([]any) {
					out = append(out, d.(map[string]any))
				}
				return out
			}
		case <-deadline:
			c.t.Fatalf("нет диагностики для %s", path)
		}
	}
}

func (c *client) open(path, text string) {
	c.notify("textDocument/didOpen", map[string]any{"textDocument": map[string]any{
		"uri": pathToURI(path), "languageId": "gasem", "version": 1, "text": text}})
}

func pos(path string, line, ch int) map[string]any {
	return map[string]any{"textDocument": map[string]any{"uri": pathToURI(path)},
		"position": map[string]any{"line": line, "character": ch}}
}

func writeFiles(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, text := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	dir, _ = filepath.EvalSymlinks(dir)
	return dir
}

const mainGsm = `og 0x7C00
SPEED = 3*7
start:
    mov si - msg
    call print
    call area
.loop:
    jmp .loop
include "lib.gsm"
msg: s: "Hi"
`

const libGsm = `print:
    nxtb
    chk al - al
    jfz .done
    jmp print
.done:
    ret

struct Point
    x: w
    y: w
end

macro twice - reg
    shl reg - 1
end

proc area - esi uses ebx
    local wid - dword
    mov eax - [wid]
end
`

func setup(t *testing.T) (*client, string) {
	dir := writeFiles(t, map[string]string{"main.gsm": mainGsm, "lib.gsm": libGsm})
	c := startServer(t)
	res := c.request("initialize", map[string]any{"rootUri": pathToURI(dir), "capabilities": map[string]any{}})
	if !strings.Contains(string(res), "definitionProvider") {
		t.Fatalf("нет возможностей в ответе initialize: %s", res)
	}
	c.notify("initialized", map[string]any{})
	return c, dir
}

func TestDiagnosticsUseWholeProgram(t *testing.T) {
	c, dir := setup(t)
	lib := filepath.Join(dir, "lib.gsm")
	c.open(lib, libGsm) // lib.gsm один бы не собрался (msg и т. п.), но он подключён из main.gsm
	if d := c.diagnostics(lib); len(d) != 0 {
		t.Fatalf("лишние ошибки: %v", d)
	}
	broken := strings.Replace(libGsm, "    chk al - al", "    chk al - [bx + cl]", 1)
	c.notify("textDocument/didChange", map[string]any{
		"textDocument":   map[string]any{"uri": pathToURI(lib), "version": 2},
		"contentChanges": []map[string]any{{"text": broken}}})
	d := c.diagnostics(lib)
	if len(d) != 1 || !strings.Contains(d[0]["message"].(string), "регистр cl нельзя использовать в адресе") {
		t.Fatalf("диагностика: %v", d)
	}
	rng := d[0]["range"].(map[string]any)
	start := rng["start"].(map[string]any)
	end := rng["end"].(map[string]any)
	if start["line"].(float64) != 2 || start["character"].(float64) != 19 || end["character"].(float64) != 21 {
		t.Fatalf("место ошибки: %v", rng)
	}
	c.notify("textDocument/didChange", map[string]any{
		"textDocument":   map[string]any{"uri": pathToURI(lib), "version": 3},
		"contentChanges": []map[string]any{{"text": libGsm}}})
	if d := c.diagnostics(lib); len(d) != 0 {
		t.Fatalf("ошибка не пропала: %v", d)
	}
}

func TestWarningsAndMacroErrors(t *testing.T) {
	dir := writeFiles(t, map[string]string{"p.gsm": "start:\nmacro bad - x\n    mov x - [bx + cl]\nend\nbad ax\nunused: nop\n"})
	c := startServer(t)
	c.request("initialize", map[string]any{"rootUri": pathToURI(dir)})
	p := filepath.Join(dir, "p.gsm")
	text, _ := os.ReadFile(p)
	c.open(p, string(text))
	d := c.diagnostics(p)
	var lines []string
	for _, x := range d {
		start := x["range"].(map[string]any)["start"].(map[string]any)
		lines = append(lines, fmt.Sprintf("%v:%v:%s", start["line"], x["severity"], x["message"]))
	}
	all := strings.Join(lines, "\n")
	for _, want := range []string{"2:1:регистр cl нельзя использовать в адресе (в макросе 'bad'",
		"4:1:регистр cl нельзя использовать в адресе — в макросе (p.gsm:3)",
		"5:2:метка 'unused' нигде не используется"} {
		if !strings.Contains(all, want) {
			t.Errorf("нет «%s» в\n%s", want, all)
		}
	}
}

func TestDefinitionHoverReferences(t *testing.T) {
	c, dir := setup(t)
	main := filepath.Join(dir, "main.gsm")
	lib := filepath.Join(dir, "lib.gsm")
	c.open(main, mainGsm)
	c.diagnostics(main)

	var loc Location
	_ = json.Unmarshal(c.request("textDocument/definition", pos(main, 4, 11)), &loc) // call print
	if loc.URI != pathToURI(lib) || loc.Range.Start.Line != 0 || loc.Range.End.Character != 5 {
		t.Fatalf("определение print: %+v", loc)
	}
	_ = json.Unmarshal(c.request("textDocument/definition", pos(main, 7, 10)), &loc) // jmp .loop
	if loc.URI != pathToURI(main) || loc.Range.Start.Line != 6 {
		t.Fatalf("определение .loop: %+v", loc)
	}
	_ = json.Unmarshal(c.request("textDocument/definition", pos(lib, 19, 15)), &loc) // [wid] в proc
	if loc.URI != pathToURI(lib) || loc.Range.Start.Line != 18 {
		t.Fatalf("определение wid: %+v", loc)
	}

	hover := func(path string, line, ch int) string {
		var h struct {
			Contents struct{ Value string } `json:"contents"`
		}
		_ = json.Unmarshal(c.request("textDocument/hover", pos(path, line, ch)), &h)
		return h.Contents.Value
	}
	if h := hover(main, 1, 2); !strings.Contains(h, "SPEED = 3*7") || !strings.Contains(h, "= 21 (0X15)") {
		t.Errorf("подсказка SPEED: %q", h)
	}
	if h := hover(main, 4, 11); !strings.Contains(h, "метка, адрес 0X7C") {
		t.Errorf("подсказка print: %q", h)
	}
	if h := hover(lib, 1, 5); !strings.Contains(h, "lodsb") {
		t.Errorf("подсказка nxtb: %q", h)
	}
	if h := hover(lib, 8, 9); !strings.Contains(h, "размер 4 байт") {
		t.Errorf("подсказка Point: %q", h)
	}
	if h := hover(main, 3, 9); !strings.Contains(h, "регистр общего назначения, 16 бит") {
		t.Errorf("подсказка si: %q", h)
	}

	var refs []Location
	_ = json.Unmarshal(c.request("textDocument/references", pos(lib, 0, 2)), &refs)
	if len(refs) != 3 { // print: в lib.gsm, jmp print, call print в main.gsm
		t.Errorf("использования print: %+v", refs)
	}
}

func TestCompletionSymbolsFormatting(t *testing.T) {
	c, dir := setup(t)
	lib := filepath.Join(dir, "lib.gsm")
	c.open(lib, libGsm)
	c.diagnostics(lib)

	var comp struct {
		Items []completionItem `json:"items"`
	}
	_ = json.Unmarshal(c.request("textDocument/completion", pos(lib, 19, 4)), &comp)
	labels := map[string]bool{}
	for _, it := range comp.Items {
		labels[it.Label] = true
	}
	for _, want := range []string{"print", "Point.y", "twice", "area", "wid", "msg", "rax", "mov", "nxtb", "proc"} {
		if !labels[want] {
			t.Errorf("в автодополнении нет %s", want)
		}
	}
	_ = json.Unmarshal(c.request("textDocument/completion", pos(lib, 2, 4)), &comp)
	for _, it := range comp.Items {
		if it.Label == "wid" {
			t.Error("локальная переменная видна вне proc")
		}
	}

	var syms []documentSymbol
	_ = json.Unmarshal(c.request("textDocument/documentSymbol", map[string]any{
		"textDocument": map[string]any{"uri": pathToURI(lib)}}), &syms)
	var names []string
	for _, s := range syms {
		n := s.Name
		for _, ch := range s.Children {
			n += "/" + ch.Name
		}
		names = append(names, n)
	}
	got := strings.Join(names, " ")
	if got != "print/print.done Point/Point.x/Point.y twice/reg area/wid" {
		t.Errorf("структура файла: %s", got)
	}

	messy := "start:\n  if al = 1 ; да\ninc bx\n   end\n"
	c.notify("textDocument/didChange", map[string]any{
		"textDocument":   map[string]any{"uri": pathToURI(lib), "version": 2},
		"contentChanges": []map[string]any{{"text": messy}}})
	var edits []struct {
		NewText string `json:"newText"`
	}
	_ = json.Unmarshal(c.request("textDocument/formatting", map[string]any{
		"textDocument": map[string]any{"uri": pathToURI(lib)}, "options": map[string]any{}}), &edits)
	if len(edits) != 1 || !strings.Contains(edits[0].NewText, "    inc bx") {
		t.Errorf("выравнивание: %+v", edits)
	}
}

func TestWindowsURI(t *testing.T) {
	if got := pathToURI(`C:\Users\Мой проект\a.gsm`); got != "file:///c%3A/Users/%D0%9C%D0%BE%D0%B9%20%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82/a.gsm" {
		t.Errorf("URI: %s", got)
	}
}

func TestURIs(t *testing.T) {
	p := filepath.Join(t.TempDir(), "Папка с пробелом", "файл.gsm")
	if got := uriToPath(pathToURI(p)); got != normPath(p) {
		t.Errorf("%s → %s → %s", p, pathToURI(p), got)
	}
	if !strings.HasPrefix(pathToURI(p), "file:///") || strings.Contains(pathToURI(p), " ") {
		t.Errorf("URI: %s", pathToURI(p))
	}
}
