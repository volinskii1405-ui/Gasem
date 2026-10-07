package gasem

// Лексер Gasem: разбивает строку исходника на токены.
//
// Особенность языка: разделитель операндов — дефис, окружённый пробелами
// (mov ax - 1). Дефис без пробелов — это минус (510-($-$$)).
// Внутри скобок ( ) и [ ] дефис всегда минус.

import (
	"math/big"
	"strings"
)

// Виды токенов.
const (
	NUM = "num" // число
	STR = "str" // строка в кавычках
	ID  = "id"  // имя: команда, регистр, метка...
	OP  = "op"  // оператор или знак препинания
	SEP = "sep" // разделитель операндов ' - '
)

const whitespace = " \t\r\f\v"

var escapes = map[rune]byte{
	'n': 10, 'r': 13, 't': 9, '0': 0, 'a': 7, 'b': 8, 'e': 27, 'f': 12,
	'v': 11, '\\': 92, '"': 34, '\'': 39,
}

var ops2 = []string{"&&", "<<", ">>", "$$", "<=", ">=", "!=", "=="}

const ops1 = "+-*/%()[]:~&|^=,$<>"

// Token — один токен строки.
type Token struct {
	Kind  string
	Value string   // для ID, OP, SEP
	Num   *big.Int // для NUM
	Bytes []byte   // для STR
	Col   int      // позиция в строке (для сообщений об ошибках)
	Space bool     // был ли пробел перед токеном
	Text  string   // исходный текст токена
	Quote rune     // для строк: какой кавычкой открыта
}

func (t *Token) IsOp(ops ...string) bool {
	if t == nil || t.Kind != OP {
		return false
	}
	for _, o := range ops {
		if t.Value == o {
			return true
		}
	}
	return false
}

func (t *Token) IsID(names ...string) bool {
	if t == nil || t.Kind != ID {
		return false
	}
	low := lower(t.Value)
	for _, n := range names {
		if low == n {
			return true
		}
	}
	return false
}

func isWS(r rune) bool { return strings.ContainsRune(whitespace, r) }

func isIDStart(r rune) bool { return isAlpha(r) || r == '_' || r == '.' }
func isIDChar(r rune) bool  { return isAlnum(r) || r == '_' || r == '.' }

// ParseNumber — число: 1234, 0x7C00, 0b1010, 0o17, 0FFh. Подчёркивания игнорируются.
func ParseNumber(word string) (*big.Int, bool) {
	w := lower(strings.ReplaceAll(word, "_", ""))
	if w == "" {
		return nil, false
	}
	if strings.HasSuffix(w, "h") && runeLen(w) > 1 {
		hex := true
		for _, c := range w[:len(w)-1] {
			if !strings.ContainsRune("0123456789abcdef", c) {
				hex = false
				break
			}
		}
		if hex {
			return parseInt(w[:len(w)-1], 16)
		}
	}
	switch {
	case strings.HasPrefix(w, "0x"):
		return parseInt(w[2:], 16)
	case strings.HasPrefix(w, "0b"):
		return parseInt(w[2:], 2)
	case strings.HasPrefix(w, "0o"):
		return parseInt(w[2:], 8)
	}
	return parseInt(w, 10)
}

func isHex(r rune) bool { return strings.ContainsRune("0123456789abcdefABCDEF", r) }

func readString(text []rune, i int, loc *SourceLoc, colBase int) ([]byte, int) {
	quote := text[i]
	j := i + 1
	var out []byte
	for {
		if j >= len(text) {
			failAt("незакрытая строка (нет закрывающей кавычки)", loc, colBase+i)
		}
		c := text[j]
		if c == quote {
			if out == nil {
				out = []byte{}
			}
			return out, j + 1
		}
		if c == '\\' {
			if j+1 >= len(text) {
				failAt("незакрытая строка (нет закрывающей кавычки)", loc, colBase+i)
			}
			e := text[j+1]
			if e == 'x' {
				end := j + 4
				if end > len(text) {
					end = len(text)
				}
				h := text[j+2 : end]
				if len(h) != 2 || !isHex(h[0]) || !isHex(h[1]) {
					failAt("после \\x нужны две шестнадцатеричные цифры", loc, colBase+j)
				}
				v, _ := parseInt(string(h), 16)
				out = append(out, byte(v.Int64()))
				j += 4
				continue
			}
			if b, ok := escapes[e]; ok {
				out = append(out, b)
				j += 2
				continue
			}
			failAt("неизвестная escape-последовательность \\"+string(e), loc, colBase+j)
		}
		out = append(out, string(c)...)
		j++
	}
}

// Tokenize разбивает строку на токены. Комментарий начинается с ';'.
//
// seps=false отключает распознавание разделителя ' - ' (используется
// внутри выражений do и let, где дефис всегда означает минус).
func Tokenize(line string, loc *SourceLoc, colBase int, seps bool) []*Token {
	var toks []*Token
	text := []rune(line)
	n := len(text)
	depth := 0
	for i := 0; i < n; {
		c := text[i]
		if isWS(c) {
			i++
			continue
		}
		if c == ';' {
			break
		}
		space := i == 0 || isWS(text[i-1])
		col := colBase + i

		if c == '"' || c == '\'' {
			value, j := readString(text, i, loc, colBase)
			toks = append(toks, &Token{Kind: STR, Bytes: value, Col: col, Space: space,
				Text: string(text[i:j]), Quote: c})
			i = j
			continue
		}

		if isDigit(c) {
			j := i
			for j < n && (isAlnum(text[j]) || text[j] == '_') {
				j++
			}
			word := string(text[i:j])
			value, ok := ParseNumber(word)
			if !ok {
				failAt("неверное число '"+word+"'", loc, col)
			}
			toks = append(toks, &Token{Kind: NUM, Num: value, Col: col, Space: space, Text: word})
			i = j
			continue
		}

		if isIDStart(c) {
			j := i + 1
			for j < n && isIDChar(text[j]) {
				j++
			}
			word := string(text[i:j])
			toks = append(toks, &Token{Kind: ID, Value: word, Col: col, Space: space, Text: word})
			i = j
			continue
		}

		if i+1 < n {
			two := string(text[i : i+2])
			found := false
			for _, o := range ops2 {
				if two == o {
					found = true
					break
				}
			}
			if found {
				toks = append(toks, &Token{Kind: OP, Value: two, Col: col, Space: space, Text: two})
				i += 2
				continue
			}
		}

		if strings.ContainsRune(ops1, c) {
			if c == '-' && seps && depth == 0 && space &&
				(i+1 >= n || isWS(text[i+1]) || text[i+1] == ';') {
				toks = append(toks, &Token{Kind: SEP, Value: "-", Col: col, Space: space, Text: "-"})
				i++
				continue
			}
			if c == '(' || c == '[' {
				depth++
			} else if c == ')' || c == ']' {
				if depth > 0 {
					depth--
				}
			}
			s := string(c)
			toks = append(toks, &Token{Kind: OP, Value: s, Col: col, Space: space, Text: s})
			i++
			continue
		}

		failAt("недопустимый символ '"+string(c)+"'", loc, col)
	}
	return toks
}

// SafeTokenize разбивает строку на токены и возвращает ошибку вместо паники.
func SafeTokenize(line string) (toks []*Token, err *Error) {
	err = catch(func() { toks = Tokenize(line, nil, 0, true) })
	return
}
