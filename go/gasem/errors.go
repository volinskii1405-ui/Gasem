package gasem

import (
	"fmt"
	"strings"
)

// SourceLoc — место в исходном тексте: файл, номер строки и сама строка.
type SourceLoc struct {
	File string
	Line int
	Text string
}

func (l *SourceLoc) String() string { return fmt.Sprintf("%s:%d", l.File, l.Line) }

// NoCol — у сообщения нет столбца (не рисуется указатель ^).
const NoCol = -1

// Error — ошибка или предупреждение в программе на Gasem.
type Error struct {
	Message string
	Loc     *SourceLoc
	Col     int
	Warning bool
}

func (e *Error) Kind() string {
	if e.Warning {
		return "предупреждение"
	}
	return "ошибка"
}

// Format — текст сообщения: место, текст, строка исходника и указатель на столбец.
func (e *Error) Format() string {
	if e.Loc == nil {
		return e.Kind() + ": " + e.Message
	}
	out := fmt.Sprintf("%s: %s: %s", e.Loc, e.Kind(), e.Message)
	text := strings.TrimRight(e.Loc.Text, "\r\n")
	if strip(text) != "" {
		out += "\n    " + expandTabs(text, 4)
		if e.Col != NoCol && e.Col >= 0 && e.Col <= runeLen(text) {
			pad := runeLen(expandTabs(string([]rune(text)[:e.Col]), 4))
			out += "\n    " + strings.Repeat(" ", pad) + "^"
		}
	}
	return out
}

func (e *Error) Error() string { return e.Format() }

// Errors — все ошибки, найденные за один запуск компилятора.
type Errors struct {
	List []*Error
}

func (e *Errors) Format() string {
	parts := make([]string, len(e.List))
	for i, x := range e.List {
		parts[i] = x.Format()
	}
	return strings.Join(parts, "\n")
}

func (e *Errors) Error() string { return e.Format() }

// fail прерывает разбор или сборку текущего оператора ошибкой.
func fail(msg string) {
	panic(&Error{Message: msg, Col: NoCol})
}

func failCol(msg string, col int) {
	panic(&Error{Message: msg, Col: col})
}

func failAt(msg string, loc *SourceLoc, col int) {
	panic(&Error{Message: msg, Loc: loc, Col: col})
}

// catch выполняет f и возвращает ошибку Gasem, если она случилась.
func catch(f func()) (err *Error) {
	defer func() {
		if r := recover(); r != nil {
			e, ok := r.(*Error)
			if !ok {
				panic(r)
			}
			err = e
		}
	}()
	f()
	return nil
}
