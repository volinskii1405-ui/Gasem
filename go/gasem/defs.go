package gasem

// Объявления имён и разбор проекта для языкового сервера (gasem lsp).

import (
	"strings"
)

// Def — объявление имени в программе.
type Def struct {
	Name    string     // полное имя: start.loop, Point.x
	Kind    string     // label, var, const, proc, macro, struct, field, local, param
	Loc     *SourceLoc // строка объявления
	Col     int
	EndLine int    // для proc, macro, struct — строка с end
	Detail  string // macro print - text; proc f - esi uses ebx; local n - dword …
	Scope   string // для local и param — имя proc или макроса
}

func (p *Parser) addDef(d *Def) {
	if d.Loc == nil || d.Loc.Origin != nil || strings.HasPrefix(d.Name, "@") {
		return // метки, созданные компилятором и макросами, не показываются
	}
	if p.lastDef == nil {
		p.lastDef = map[string]*Def{}
	}
	p.Defs = append(p.Defs, d)
	if d.Scope == "" {
		p.lastDef[d.Name] = d
	}
}

// tokensText — исходный текст от первого до последнего токена.
func tokensText(loc *SourceLoc, toks []*Token) string {
	if len(toks) == 0 || loc == nil {
		return ""
	}
	rs := []rune(loc.Text)
	last := toks[len(toks)-1]
	end := min(last.Col+runeLen(last.Text), len(rs))
	if toks[0].Col > end {
		return ""
	}
	return string(rs[toks[0].Col:end])
}

// Analysis — всё, что языковому серверу нужно знать о программе.
type Analysis struct {
	Result   *Result  // nil, если есть ошибки
	Errors   []*Error // ошибки разбора или сборки
	Warnings []*Error
	Defs     []*Def
	Files    []string // файлы, из которых собрана программа
}

// Analyze разбирает и собирает программу, начиная с файла root.
// readFile подменяет чтение файлов (открытые в редакторе, но не сохранённые).
func Analyze(root string, readFile func(string) ([]byte, error)) *Analysis {
	p := NewParser()
	p.ReadFile = readFile
	a := &Analysis{}
	if e := catch(func() { p.ParseFile(root, nil) }); e != nil {
		a.Errors = []*Error{e}
	} else if res, errs := assembleParsed(p); errs != nil {
		a.Errors = errs.List
		a.Warnings = p.warnings
	} else {
		a.Result = res
		a.Warnings = res.Warnings
	}
	a.Defs = p.Defs
	seen := map[string]bool{}
	for _, l := range p.lines {
		if !seen[l.File] {
			seen[l.File] = true
			a.Files = append(a.Files, l.File)
		}
	}
	return a
}
