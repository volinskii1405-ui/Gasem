// Command gasemapi — Go-компилятор Gasem для тестов Python-версии.
//
// Читает запросы JSON (по одному в строке) из stdin и отвечает в stdout:
//
//	{"op": "compile_source", "text": "...", "filename": "...", "base_dir": "..."}
//	{"op": "compile_file", "path": "..."}
//	{"op": "format_text", "text": "..."}
//	{"op": "tokenize", "text": "..."}
//
// С его помощью весь набор тестов (tests/, GASEM_IMPL=go) проверяет Go-версию
// компилятора так же, как Python-версию.
package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"

	"github.com/volinskii1405-ui/Gasem/go/gasem"
)

type request struct {
	Op       string  `json:"op"`
	Text     string  `json:"text"`
	Filename *string `json:"filename"`
	BaseDir  *string `json:"base_dir"`
	Path     string  `json:"path"`
}

type jsonError struct {
	Message   string  `json:"message"`
	File      *string `json:"file"`
	Line      int     `json:"line"`
	Text      string  `json:"text"`
	Col       *int    `json:"col"`
	Warning   bool    `json:"warning"`
	Formatted string  `json:"formatted"`
}

func encodeError(e *gasem.Error) jsonError {
	out := jsonError{Message: e.Message, Warning: e.Warning, Formatted: e.Format()}
	if e.Loc != nil {
		f := e.Loc.File
		out.File, out.Line, out.Text = &f, e.Loc.Line, e.Loc.Text
	}
	if e.Col != gasem.NoCol {
		c := e.Col
		out.Col = &c
	}
	return out
}

func encodeErrors(list []*gasem.Error) []jsonError {
	out := make([]jsonError, len(list))
	for i, e := range list {
		out[i] = encodeError(e)
	}
	return out
}

func compileResponse(res *gasem.Result, errs *gasem.Errors) map[string]any {
	if errs != nil {
		return map[string]any{"ok": false, "errors": encodeErrors(errs.List)}
	}
	syms := make([][2]string, len(res.Symbols))
	for i, s := range res.Symbols {
		syms[i] = [2]string{s.Name, s.Value.String()}
	}
	lines := make([][]any, len(res.Lines))
	for i, l := range res.Lines {
		lines[i] = []any{l.Addr.String(), l.Size, l.Bits, l.Loc.File, l.Loc.Line, l.Loc.Text}
	}
	return map[string]any{
		"ok":       true,
		"code":     base64.StdEncoding.EncodeToString(res.Code),
		"origin":   res.Origin.String(),
		"symbols":  syms,
		"listing":  res.Listing(),
		"lines":    lines,
		"warnings": encodeErrors(res.Warnings),
	}
}

func handle(req request) (resp map[string]any) {
	defer func() {
		if r := recover(); r != nil {
			resp = map[string]any{"ok": false, "crash": fmt.Sprint(r)}
		}
	}()
	switch req.Op {
	case "compile_source":
		filename, baseDir := gasem.SourceName, ""
		if req.Filename != nil {
			filename = *req.Filename
		}
		if req.BaseDir != nil {
			baseDir = *req.BaseDir
		}
		return compileResponse(gasem.CompileSource(req.Text, filename, baseDir))
	case "compile_file":
		return compileResponse(gasem.CompileFile(req.Path))
	case "format_text":
		return map[string]any{"ok": true, "text": gasem.FormatText(req.Text)}
	case "tokenize":
		toks, err := gasem.SafeTokenize(req.Text)
		if err != nil {
			return map[string]any{"ok": false, "errors": encodeErrors([]*gasem.Error{err})}
		}
		out := make([][]any, len(toks))
		for i, t := range toks {
			var v any
			switch t.Kind {
			case gasem.NUM:
				v = map[string]string{"int": t.Num.String()}
			case gasem.STR:
				v = map[string]string{"bytes": base64.StdEncoding.EncodeToString(t.Bytes)}
			default:
				v = t.Value
			}
			out[i] = []any{t.Kind, v}
		}
		return map[string]any{"ok": true, "tokens": out}
	}
	return map[string]any{"ok": false, "crash": "неизвестная операция " + req.Op}
}

func main() {
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriter(os.Stdout)
	enc := json.NewEncoder(out)
	enc.SetEscapeHTML(false)
	for {
		line, err := in.ReadBytes('\n')
		if len(line) > 0 {
			var req request
			if e := json.Unmarshal(line, &req); e != nil {
				_ = enc.Encode(map[string]any{"ok": false, "crash": e.Error()})
			} else {
				_ = enc.Encode(handle(req))
			}
			_ = out.Flush()
		}
		if err != nil {
			return
		}
	}
}
