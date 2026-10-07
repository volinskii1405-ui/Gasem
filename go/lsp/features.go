package lsp

import (
	"fmt"
	"math/big"
	"sort"
	"strings"
	"unicode"

	"github.com/volinskii1405-ui/Gasem/go/gasem"
)

func isLetterOrDigit(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) }

// ---------------------------------------------------------------- поиск объявления

// resolve — объявление имени word, записанного в строке line (с 0) файла path.
func resolve(a *gasem.Analysis, path string, line int, word string) *gasem.Def {
	if word == "" || a == nil {
		return nil
	}
	ln := line + 1
	// локальные переменные proc и параметры макроса — внутри своего блока
	for _, d := range a.Defs {
		if (d.Kind == "proc" || d.Kind == "macro") && normPath(d.Loc.File) == path &&
			d.Loc.Line <= ln && ln <= d.EndLine {
			for _, v := range a.Defs {
				if v.Scope == d.Name && v.Name == word && normPath(v.Loc.File) == path {
					return v
				}
			}
		}
	}
	name := word
	if strings.HasPrefix(word, ".") { // .loop — локальная метка последней глобальной выше
		var global *gasem.Def
		for _, d := range a.Defs {
			if d.Scope == "" && normPath(d.Loc.File) == path && d.Loc.Line <= ln && isGlobalLabel(d) &&
				(global == nil || d.Loc.Line >= global.Loc.Line) {
				global = d
			}
		}
		if global == nil {
			return nil
		}
		name = global.Name + word
	}
	for _, d := range a.Defs {
		if d.Scope == "" && d.Name == name {
			return d
		}
	}
	return nil
}

func isGlobalLabel(d *gasem.Def) bool {
	return (d.Kind == "label" || d.Kind == "var" || d.Kind == "proc") && !strings.Contains(d.Name, ".")
}

// defRange — место имени в строке объявления.
func defRange(d *gasem.Def) Range {
	text := d.Loc.Text
	_, start, end := wordAt(text, d.Col)
	return Range{Position{d.Loc.Line - 1, utf16Col(text, start)}, Position{d.Loc.Line - 1, utf16Col(text, end)}}
}

func (s *Server) wordAtPosition(p posParams) (string, string, int) {
	path := uriToPath(p.TextDocument.URI)
	text := s.lineText(path, p.Position.Line)
	word, _, _ := wordAt(text, runeCol(text, p.Position.Character))
	return path, word, p.Position.Line
}

func (s *Server) definition(p posParams) any {
	path, word, line := s.wordAtPosition(p)
	a, _ := s.analysisFor(path)
	d := resolve(a, path, line, word)
	if d == nil {
		return nil
	}
	return Location{URI: pathToURI(normPath(d.Loc.File)), Range: defRange(d)}
}

// ---------------------------------------------------------------- подсказка при наведении

var kindNames = map[string]string{"label": "метка", "var": "переменная", "const": "константа",
	"proc": "процедура", "macro": "макрос", "struct": "структура", "field": "поле структуры",
	"local": "локальная переменная", "param": "параметр макроса"}

func hexValue(v *big.Int) string {
	if v.Sign() >= 0 {
		return fmt.Sprintf("%#X", v)
	}
	return v.String()
}

func (s *Server) hover(p posParams) any {
	path, word, line := s.wordAtPosition(p)
	if word == "" {
		return nil
	}
	a, root := s.analysisFor(path)
	var md string
	if d := resolve(a, path, line, word); d != nil {
		md = s.describe(d, root)
	} else if doc := describeWord(word); doc != "" {
		md = doc
	} else {
		return nil
	}
	return map[string]any{"contents": map[string]any{"kind": "markdown", "value": md}}
}

func (s *Server) describe(d *gasem.Def, root string) string {
	code := d.Detail
	if code == "" {
		code = strings.TrimSpace(d.Loc.Text)
	}
	out := "```gasem\n" + code + "\n```\n" + kindNames[d.Kind]
	s.mu.Lock()
	good := s.good[root]
	s.mu.Unlock()
	if good != nil && good.Result != nil {
		if v, ok := good.Result.SymbolMap()[d.Name]; ok {
			switch d.Kind {
			case "const", "field":
				out += fmt.Sprintf(" = %s (%s)", v, hexValue(v))
			case "struct":
			default:
				out += ", адрес " + hexValue(v)
			}
		}
		if d.Kind == "struct" {
			if v, ok := good.Result.SymbolMap()[d.Name+".size"]; ok {
				out += fmt.Sprintf(", размер %s байт", v)
			}
		}
	}
	return out
}

// ---------------------------------------------------------------- автодополнение

// Виды пунктов автодополнения LSP.
const (
	kindFunction = 3
	kindField    = 5
	kindVariable = 6
	kindKeyword  = 14
	kindSnippet  = 15
	kindConstant = 21
	kindStruct   = 22
)

var defKinds = map[string]int{"label": kindFunction, "var": kindVariable, "const": kindConstant,
	"proc": kindFunction, "macro": kindSnippet, "struct": kindStruct, "field": kindField,
	"local": kindVariable, "param": kindVariable}

type completionItem struct {
	Label  string `json:"label"`
	Kind   int    `json:"kind"`
	Detail string `json:"detail,omitempty"`
}

func (s *Server) completion(p posParams) any {
	path := uriToPath(p.TextDocument.URI)
	a, _ := s.analysisFor(path)
	line := p.Position.Line + 1
	seen := map[string]bool{}
	var items []completionItem
	add := func(label string, kind int, detail string) {
		if label != "" && !seen[label] {
			seen[label] = true
			items = append(items, completionItem{label, kind, detail})
		}
	}
	if a != nil {
		var scopes []string // proc и макросы, внутри которых курсор
		for _, d := range a.Defs {
			if (d.Kind == "proc" || d.Kind == "macro") && normPath(d.Loc.File) == path &&
				d.Loc.Line <= line && line <= d.EndLine {
				scopes = append(scopes, d.Name)
			}
		}
		for _, d := range a.Defs {
			if d.Scope != "" {
				for _, sc := range scopes {
					if d.Scope == sc {
						add(d.Name, defKinds[d.Kind], d.Detail)
					}
				}
				continue
			}
			add(d.Name, defKinds[d.Kind], d.Detail)
		}
	}
	for _, w := range keywordList() {
		add(w, kindKeyword, "")
	}
	regs := make([]string, 0, len(gasem.Registers))
	for r := range gasem.Registers {
		regs = append(regs, r)
	}
	sort.Strings(regs)
	for _, r := range regs {
		add(r, kindVariable, "регистр")
	}
	return map[string]any{"isIncomplete": false, "items": items}
}

func keywordList() []string {
	words := append([]string{}, gasem.CommandWords...)
	words = append(words, "b:", "w:", "d:", "q:", "s:", "byte", "word", "dword", "qword", "short", "near", "far",
		"signed", "and", "or", "not", "zero", "carry", "sign", "overflow", "parity", "uses", "a20")
	return words
}

// ---------------------------------------------------------------- структура файла

type documentSymbol struct {
	Name           string           `json:"name"`
	Detail         string           `json:"detail,omitempty"`
	Kind           int              `json:"kind"`
	Range          Range            `json:"range"`
	SelectionRange Range            `json:"selectionRange"`
	Children       []documentSymbol `json:"children,omitempty"`
}

// Виды символов LSP.
var symbolKinds = map[string]int{"label": 12, "var": 13, "const": 14, "proc": 12, "macro": 12,
	"struct": 23, "field": 8, "local": 13, "param": 13}

func (s *Server) documentSymbols(path string) any {
	a, _ := s.analysisFor(path)
	if a == nil {
		return []documentSymbol{}
	}
	var top []documentSymbol
	var containers []*gasem.Def
	var containerIdx []int
	for _, d := range a.Defs {
		if normPath(d.Loc.File) != path {
			continue
		}
		sel := defRange(d)
		sym := documentSymbol{Name: d.Name, Detail: d.Detail, Kind: symbolKinds[d.Kind], Range: sel, SelectionRange: sel}
		if d.EndLine > 0 {
			sym.Range = Range{Position{d.Loc.Line - 1, 0}, Position{d.EndLine - 1, 0}}
		}
		parent := -1
		switch {
		case d.Scope != "": // локальная переменная proc или параметр макроса
			for i := len(top) - 1; i >= 0; i-- {
				if top[i].Name == d.Scope {
					parent = i
					break
				}
			}
		case (d.Kind == "label" || d.Kind == "var") && strings.Contains(d.Name, "."): // start.loop
			owner := d.Name[:strings.Index(d.Name, ".")]
			for i := len(top) - 1; i >= 0; i-- {
				if top[i].Name == owner {
					parent = i
					break
				}
			}
		default:
			for i := len(containers) - 1; i >= 0; i-- {
				c := containers[i]
				if c.Loc.Line < d.Loc.Line && d.Loc.Line <= c.EndLine {
					parent = containerIdx[i]
					break
				}
			}
		}
		if parent >= 0 {
			top[parent].Children = append(top[parent].Children, sym)
			continue
		}
		top = append(top, sym)
		if d.EndLine > 0 {
			containers = append(containers, d)
			containerIdx = append(containerIdx, len(top)-1)
		}
	}
	if top == nil {
		top = []documentSymbol{}
	}
	return top
}

// ---------------------------------------------------------------- использования

func (s *Server) references(p posParams) any {
	path, word, line := s.wordAtPosition(p)
	a, _ := s.analysisFor(path)
	target := resolve(a, path, line, word)
	if target == nil {
		return []Location{}
	}
	var out []Location
	for _, f := range a.Files {
		file := normPath(f)
		text, ok := s.text(file)
		if !ok {
			continue
		}
		for i, ln := range strings.Split(text, "\n") {
			ln = strings.TrimRight(ln, "\r")
			toks, err := gasem.SafeTokenize(ln)
			if err != nil {
				continue
			}
			for _, t := range toks {
				if t.Kind != gasem.ID || (t.Value != word && !strings.HasSuffix(target.Name, t.Value)) {
					continue
				}
				if d := resolve(a, file, i, t.Value); d == target {
					out = append(out, Location{URI: pathToURI(file), Range: Range{
						Position{i, utf16Col(ln, t.Col)}, Position{i, utf16Col(ln, t.Col+len([]rune(t.Text)))}}})
				}
			}
		}
	}
	if out == nil {
		out = []Location{}
	}
	return out
}

// ---------------------------------------------------------------- выравнивание

func (s *Server) formatting(path string) any {
	text, ok := s.text(path)
	if !ok {
		return []any{}
	}
	norm := gasem.UniversalNewlines(text)
	formatted := gasem.FormatText(norm)
	if formatted == norm {
		return []any{}
	}
	lines := strings.Split(text, "\n")
	end := Position{len(lines) - 1, utf16Col(lines[len(lines)-1], len([]rune(lines[len(lines)-1])))}
	return []map[string]any{{"range": Range{Position{0, 0}, end}, "newText": formatted}}
}
