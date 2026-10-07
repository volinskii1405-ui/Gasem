package gasem

// Поведение строк и чисел как в Python: компилятор Gasem изначально написан
// на Python, и Go-версия должна давать те же байты, столбцы в сообщениях
// и то же разбиение на строки.

import (
	"errors"
	"io/fs"
	"math/big"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf8"
)

// isSpace — str.isspace() для одного символа.
func isSpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

func isAlpha(r rune) bool { return unicode.IsLetter(r) }

func isAlnum(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) }

// Символы, для которых str.isdigit() истинно, хотя это не десятичные цифры
// (надстрочные, подстрочные, цифры в кружках и т. п.).
var otherDigits = &unicode.RangeTable{
	R16: []unicode.Range16{
		{0x00b2, 0x00b3, 1}, {0x00b9, 0x00b9, 1}, {0x1369, 0x1371, 1}, {0x19da, 0x19da, 1},
		{0x2070, 0x2070, 1}, {0x2074, 0x2079, 1}, {0x2080, 0x2089, 1}, {0x2460, 0x2468, 1},
		{0x2474, 0x247c, 1}, {0x2488, 0x2490, 1}, {0x24ea, 0x24ea, 1}, {0x24f5, 0x24fd, 1},
		{0x24ff, 0x24ff, 1}, {0x2776, 0x277e, 1}, {0x2780, 0x2788, 1}, {0x278a, 0x2792, 1},
	},
	R32: []unicode.Range32{
		{0x10a40, 0x10a43, 1}, {0x10e60, 0x10e68, 1}, {0x11052, 0x1105a, 1}, {0x1f100, 0x1f10a, 1},
	},
}

// isDigit — str.isdigit().
func isDigit(r rune) bool { return unicode.IsDigit(r) || unicode.Is(otherDigits, r) }

// decimalValue — значение десятичной цифры любого алфавита (как unicodedata.decimal).
func decimalValue(r rune) (int, bool) {
	if r >= '0' && r <= '9' {
		return int(r - '0'), true
	}
	if !unicode.IsDigit(r) {
		return 0, false
	}
	start := r
	for k := 0; k < 100 && unicode.IsDigit(start-1); k++ {
		start--
	}
	return int(r-start) % 10, true
}

// lower — str.lower().
func lower(s string) string {
	if strings.ContainsRune(s, 'İ') {
		s = strings.ReplaceAll(s, "İ", "i̇")
	}
	return strings.ToLower(s)
}

func strip(s string) string  { return strings.TrimFunc(s, isSpace) }
func lstrip(s string) string { return strings.TrimLeftFunc(s, isSpace) }
func rstrip(s string) string { return strings.TrimRightFunc(s, isSpace) }

func runeLen(s string) int { return utf8.RuneCountInString(s) }

// expandTabs — str.expandtabs(size).
func expandTabs(s string, size int) string {
	if !strings.ContainsRune(s, '\t') {
		return s
	}
	var b strings.Builder
	col := 0
	for _, r := range s {
		switch r {
		case '\t':
			n := size - col%size
			b.WriteString(strings.Repeat(" ", n))
			col += n
		case '\n', '\r':
			b.WriteRune(r)
			col = 0
		default:
			b.WriteRune(r)
			col++
		}
	}
	return b.String()
}

func isLineBreak(r rune) bool {
	switch r {
	case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
		return true
	}
	return false
}

// splitLines — str.splitlines().
func splitLines(s string) []string {
	var out []string
	rs := []rune(s)
	start := 0
	for i := 0; i < len(rs); i++ {
		if isLineBreak(rs[i]) {
			out = append(out, string(rs[start:i]))
			if rs[i] == '\r' && i+1 < len(rs) && rs[i+1] == '\n' {
				i++
			}
			start = i + 1
		}
	}
	if start < len(rs) {
		out = append(out, string(rs[start:]))
	}
	return out
}

// splitLinesKeep — str.splitlines(True).
func splitLinesKeep(s string) []string {
	var out []string
	rs := []rune(s)
	start := 0
	for i := 0; i < len(rs); i++ {
		if isLineBreak(rs[i]) {
			if rs[i] == '\r' && i+1 < len(rs) && rs[i+1] == '\n' {
				i++
			}
			out = append(out, string(rs[start:i+1]))
			start = i + 1
		}
	}
	if start < len(rs) {
		out = append(out, string(rs[start:]))
	}
	return out
}

// universalNewlines — то, что делает open() в текстовом режиме: \r\n и \r → \n.
func universalNewlines(s string) string {
	if !strings.ContainsRune(s, '\r') {
		return s
	}
	s = strings.ReplaceAll(s, "\r\n", "\n")
	return strings.ReplaceAll(s, "\r", "\n")
}

// decodeReplace — bytes.decode("utf-8", errors="replace"): каждая
// «максимальная часть» неверной последовательности заменяется одним U+FFFD.
func decodeReplace(b []byte) string {
	if utf8.Valid(b) {
		return string(b)
	}
	var out strings.Builder
	for i := 0; i < len(b); {
		r, n := utf8.DecodeRune(b[i:])
		if r != utf8.RuneError || n > 1 {
			out.WriteRune(r)
			i += n
			continue
		}
		out.WriteRune(utf8.RuneError)
		i += invalidPrefix(b[i:])
	}
	return out.String()
}

func invalidPrefix(b []byte) int {
	c := b[0]
	var need int
	lo, hi := byte(0x80), byte(0xBF)
	switch {
	case c >= 0xC2 && c <= 0xDF:
		need = 1
	case c == 0xE0:
		need, lo = 2, 0xA0
	case c >= 0xE1 && c <= 0xEC, c == 0xEE, c == 0xEF:
		need = 2
	case c == 0xED:
		need, hi = 2, 0x9F
	case c == 0xF0:
		need, lo = 3, 0x90
	case c >= 0xF1 && c <= 0xF3:
		need = 3
	case c == 0xF4:
		need, hi = 3, 0x8F
	default:
		return 1
	}
	n := 1
	for k := 0; k < need && n < len(b); k++ {
		x := b[n]
		if k == 0 {
			if x < lo || x > hi {
				break
			}
		} else if x < 0x80 || x > 0xBF {
			break
		}
		n++
	}
	return n
}

// parseInt — int(s, base) для строки без знака и подчёркиваний.
func parseInt(s string, base int) (*big.Int, bool) {
	prefix := map[int]string{16: "0x", 8: "0o", 2: "0b"}[base]
	if prefix != "" && len(s) >= 2 && strings.EqualFold(s[:2], prefix) {
		s = s[2:]
	}
	if s == "" {
		return nil, false
	}
	var digits strings.Builder
	for _, r := range s {
		var d int
		if v, ok := decimalValue(r); ok {
			d = v
		} else if r >= 'a' && r <= 'z' {
			d = int(r-'a') + 10
		} else if r >= 'A' && r <= 'Z' {
			d = int(r-'A') + 10
		} else {
			return nil, false
		}
		if d >= base {
			return nil, false
		}
		digits.WriteByte("0123456789abcdefghijklmnopqrstuvwxyz"[d])
	}
	v, ok := new(big.Int).SetString(digits.String(), base)
	return v, ok
}

// basename — os.path.basename.
func basename(path string) string {
	i := strings.LastIndexAny(path, pathSeparators)
	return path[i+1:]
}

// splitExt — os.path.splitext(path)[0].
func SplitExtRoot(path string) string {
	sep := strings.LastIndexAny(path, pathSeparators)
	dot := strings.LastIndex(path, ".")
	if dot > sep {
		// точки в начале имени файла не считаются расширением
		for i := sep + 1; i < dot; i++ {
			if path[i] != '.' {
				return path[:dot]
			}
		}
	}
	return path
}

// Strerror — текст ошибки ОС, как e.strerror в Python.
func Strerror(err error) string {
	var errno syscall.Errno
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return "No such file or directory"
	case errors.Is(err, fs.ErrPermission):
		return "Permission denied"
	case errors.As(err, &errno):
		return capitalize(errno.Error())
	}
	var pe *fs.PathError
	if errors.As(err, &pe) {
		return capitalize(pe.Err.Error())
	}
	return capitalize(err.Error())
}

func capitalize(s string) string {
	r, n := utf8.DecodeRuneInString(s)
	if n == 0 {
		return s
	}
	return string(unicode.ToUpper(r)) + s[n:]
}
