package gasem

// gasem fmt — приводит программу к единому виду.
//
// Меняются только пробелы, поэтому смысл программы остаться прежним не может:
//   - тело if / while / for / repeat / macro / proc / struct / at сдвигается
//     на 4 пробела от начала блока, elif / else / end / until — на уровень
//     начала блока;
//   - комментарии в конце строк выравниваются в один столбец (в пределах
//     группы строк без пустых строк между ними);
//   - табуляция заменяется пробелами, пробелы в конце строк убираются.

import "strings"

var fmtOpeners = map[string]bool{"if": true, "while": true, "for": true, "repeat": true,
	"macro": true, "proc": true, "struct": true, "at": true}
var fmtMiddle = map[string]bool{"elif": true, "else": true}
var fmtClosers = map[string]bool{"end": true, "until": true}
var dataWords = map[string]bool{"b": true, "w": true, "d": true, "q": true, "s": true}

const (
	commentCol = 33 // столбец комментариев по умолчанию
	longLine   = 70 // такие длинные строки в выравнивании не участвуют
	indentStep = 4
)

// splitComment — строка → (код, комментарий). ';' внутри строк в кавычках — не комментарий.
func splitComment(line string) (string, string) {
	rs := []rune(line)
	var quote rune
	for i := 0; i < len(rs); i++ {
		c := rs[i]
		if quote != 0 {
			if c == '\\' {
				i++
				continue
			}
			if c == quote {
				quote = 0
			}
		} else if c == '"' || c == '\'' {
			quote = c
		} else if c == ';' {
			return rstrip(string(rs[:i])), rstrip(string(rs[i:]))
		}
	}
	return rstrip(line), ""
}

// codeStatementWord — первое слово оператора (после меток вида «имя:»), в нижнем регистре.
func codeStatementWord(code string) string {
	var toks []*Token
	if e := catch(func() { toks = Tokenize(code, nil, 0, true) }); e != nil {
		return ""
	}
	i := 0
	for i+1 < len(toks) && toks[i].Kind == ID && toks[i+1].Kind == OP && toks[i+1].Value == ":" &&
		!dataWords[lower(toks[i].Value)] {
		i += 2
	}
	if i < len(toks) && toks[i].Kind == ID {
		return lower(toks[i].Value)
	}
	return ""
}

type fmtRow struct {
	indent  int
	code    string
	comment string
}

// FormatText выравнивает оформление программы.
func FormatText(text string) string {
	lines := strings.Split(expandTabs(text, indentStep), "\n")
	var stack []int    // отступы открытых блоков
	var rows []*fmtRow // nil — пустая строка
	for _, line := range lines {
		code, comment := splitComment(line)
		body := strip(code)
		ownIndent := runeLen(code) - runeLen(lstrip(code))
		if body == "" {
			if comment == "" {
				rows = append(rows, nil)
				continue
			}
			indent := runeLen(line) - runeLen(lstrip(line))
			if len(stack) > 0 {
				indent = stack[len(stack)-1] + indentStep
			}
			rows = append(rows, &fmtRow{indent, "", comment})
			continue
		}
		word := codeStatementWord(body)
		var indent int
		if fmtMiddle[word] || fmtClosers[word] {
			indent = ownIndent
			if len(stack) > 0 {
				indent = stack[len(stack)-1]
			}
			if fmtClosers[word] && len(stack) > 0 {
				stack = stack[:len(stack)-1]
			}
		} else {
			indent = ownIndent
			if len(stack) > 0 {
				indent = stack[len(stack)-1] + indentStep
			}
			if fmtOpeners[word] {
				stack = append(stack, indent)
			}
		}
		rows = append(rows, &fmtRow{indent, body, comment})
	}

	var out []string
	var group []*fmtRow
	flush := func() {
		col := commentCol
		for _, r := range group {
			if w := r.indent + runeLen(r.code); r.code != "" && r.comment != "" && w <= longLine && w+2 > col {
				col = w + 2
			}
		}
		for _, r := range group {
			pad := strings.Repeat(" ", r.indent)
			switch {
			case r.code == "":
				out = append(out, pad+r.comment)
			case r.comment == "":
				out = append(out, pad+r.code)
			default:
				left := pad + r.code
				n := 1
				if runeLen(left)+2 <= col {
					n = col - runeLen(left)
				}
				out = append(out, left+strings.Repeat(" ", n)+r.comment)
			}
		}
		group = nil
	}
	for _, r := range rows {
		if r == nil {
			flush()
			out = append(out, "")
		} else {
			group = append(group, r)
		}
	}
	flush()
	return strings.Join(out, "\n")
}
