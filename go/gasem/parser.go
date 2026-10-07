package gasem

// Парсер Gasem: строки исходника → список операторов.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

var dataUnits = map[string]int{"b": 1, "w": 2, "d": 4, "q": 8, "s": 1} // s: — строка с нулём в конце
var sizeWords = map[string]int{"byte": 8, "word": 16, "dword": 32, "qword": 64, "b": 8, "w": 16, "d": 32, "q": 64}
var jumpWords = map[string]bool{"short": true, "near": true, "far": true}
var directives = map[string]bool{"og": true, "align": true, "incbin": true, "include": true, "do": true,
	"equ": true, "pool": true, "args": true}

// Reserved — слова, которые нельзя использовать как имена.
var Reserved = func() map[string]bool {
	m := map[string]bool{"a20": true, "s": true, "rel": true, "abs": true}
	for k := range blockWords {
		m[k] = true
	}
	for k := range Registers {
		m[k] = true
	}
	for k := range sizeWords {
		m[k] = true
	}
	for k := range jumpWords {
		m[k] = true
	}
	for k := range directives {
		m[k] = true
	}
	return m
}()

// в какие регистры попадают аргументы call, если для подпрограммы нет args
var defaultArgs = map[int][]string{16: {"ax", "bx", "cx", "dx", "si", "di"},
	32: {"eax", "ebx", "ecx", "edx", "esi", "edi"},
	64: {"rax", "rbx", "rcx", "rdx", "rsi", "rdi"}}

// Приоритеты бинарных операторов (от низшего к высшему).
var binaryLevels = [][]string{{"|"}, {"^"}, {"&"}, {"<<", ">>"}, {"+", "-"}, {"*", "/", "%"}}

const sepHint = "разделитель операндов в Gasem — дефис с пробелами ' - '; " +
	"в выражениях пишите минус без пробелов (510-($-$$)) или в скобках"

const maxIncludeDepth = 32

// ---------------------------------------------------------------- поток токенов

type tokenStream struct {
	toks []*Token
	pos  int
	loc  *SourceLoc
}

func newStream(toks []*Token, loc *SourceLoc) *tokenStream {
	return &tokenStream{toks: toks, loc: loc}
}

func (ts *tokenStream) peek(k int) *Token {
	j := ts.pos + k
	if j >= 0 && j < len(ts.toks) {
		return ts.toks[j]
	}
	return nil
}

func (ts *tokenStream) next() *Token {
	t := ts.peek(0)
	if t == nil {
		ts.error("неожиданный конец строки")
	}
	ts.pos++
	return t
}

func (ts *tokenStream) atEnd() bool { return ts.pos >= len(ts.toks) }

func (ts *tokenStream) rest() []*Token {
	var r []*Token
	if ts.pos < len(ts.toks) {
		r = ts.toks[ts.pos:]
	}
	ts.pos = len(ts.toks)
	return r
}

// error — ошибка у текущего токена.
func (ts *tokenStream) error(msg string) {
	ts.errorAt(msg, ts.peek(0))
}

// errorAt — ошибка у токена tok (nil — в конце строки).
func (ts *tokenStream) errorAt(msg string, tok *Token) {
	col := NoCol
	if tok == nil && len(ts.toks) > 0 {
		last := ts.toks[len(ts.toks)-1]
		col = last.Col + runeLen(last.Text)
	} else if tok != nil {
		col = tok.Col
	}
	failAt(msg, ts.loc, col)
}

func (ts *tokenStream) expectEnd(what string) {
	t := ts.peek(0)
	if t != nil {
		if t.Kind == SEP {
			ts.errorAt("лишний разделитель ' - ' ("+sepHint+")", t)
		}
		if t.IsOp(",") {
			ts.errorAt("операнды разделяются ' - ' (дефис с пробелами), а не запятой", t)
		}
		ts.errorAt(fmt.Sprintf("неожиданное '%s' — ожидался конец %s", t.Text, what), t)
	}
}

// minusSeps — там, где нет операндов (константы, условия), ' - ' означает минус.
func minusSeps(toks []*Token) []*Token {
	out := make([]*Token, len(toks))
	for i, t := range toks {
		if t.Kind == SEP {
			out[i] = &Token{Kind: OP, Value: "-", Col: t.Col, Space: t.Space, Text: "-"}
		} else {
			out[i] = t
		}
	}
	return out
}

// stringData — метки и данные строк: (метка, байты) → @strN: s: "..."
func stringData(entries []poolEntry, loc *SourceLoc) []Stmt {
	var out []Stmt
	for _, e := range entries {
		out = append(out, &LabelStmt{Name: e.Label, Loc: loc, Col: NoCol})
		out = append(out, &DataStmt{Unit: 1, Kind: "list", Items: []Expr{&Str{e.Value, NoCol}, numNode(0)},
			Loc: loc, Name: "s"})
	}
	return out
}

// reads — какие регистры (семейства) читает операнд.
func reads(op Operand) map[int]bool {
	out := map[int]bool{}
	switch o := op.(type) {
	case *RegOperand:
		if o.Reg.Kind == "gpr" {
			out[family(o.Reg)] = true
		}
	case *MemOperand:
		for _, r := range []*Reg{o.Base, o.Index} {
			if r != nil {
				out[family(r)] = true
			}
		}
	}
	return out
}

func isByteOperand(o Operand) bool {
	switch x := o.(type) {
	case *RegOperand:
		return x.Reg.Size == 8
	case *MemOperand:
		return x.Size == 8
	}
	return false
}

func isStringOperand(o Operand) bool {
	x, ok := o.(*ImmOperand)
	return ok && x.FromString
}

// checkStringOperands: 'a' — символ, "a" — адрес строки. Ловим путаницу с байтовым операндом.
func checkStringOperands(ops []Operand, loc *SourceLoc, col int) {
	str, byteOp := false, false
	for _, o := range ops {
		str = str || isStringOperand(o)
		byteOp = byteOp || isByteOperand(o)
	}
	if str && byteOp {
		failAt("строка в двойных кавычках — это адрес строки; "+
			"символ записывается в одинарных кавычках: 'a'", loc, col)
	}
}

func splitSeps(toks []*Token) [][]*Token {
	groups := [][]*Token{nil}
	for _, t := range toks {
		if t.Kind == SEP {
			groups = append(groups, nil)
		} else {
			groups[len(groups)-1] = append(groups[len(groups)-1], t)
		}
	}
	return groups
}

func allNonEmpty(groups [][]*Token) bool {
	for _, g := range groups {
		if len(g) == 0 {
			return false
		}
	}
	return true
}

// ---------------------------------------------------------------- парсер

type definedLabel struct {
	name string
	loc  *SourceLoc
	col  int
}

// Parser превращает исходный текст в операторы.
type Parser struct {
	statements     []Stmt
	lines          []*SourceLoc         // все строки исходника (для листинга)
	varDefs        map[string]*DataStmt // имя переменной → данные (для [имя] в do)
	errors         []*Error
	lastGlobal     string
	hasGlobal      bool
	pendingLabels  []string // метки, за которыми ещё не было оператора
	includeStack   []string
	bits           int // текущий режим (b 16 / b 32) — нужен для let
	currentLoc     *SourceLoc
	hl             *highLevel
	callArgs       map[string][]*Reg // подпрограмма → регистры аргументов (args)
	pendingStrings map[string]string // строки из команд после последнего pool: байты → метка
	pendingOrder   []string
	stringFirstUse map[string]Stmt // метка строки → первый оператор, где она нужна
	stringsWaiting []string        // строки текущей строки, ещё не привязанные к оператору
	stringCounter  int
	referenced     map[string]bool // имена, на которые есть ссылки
	defined        []definedLabel  // метки программы
	firstLabel     string          // точка входа — о ней не предупреждаем
	hasFirstLabel  bool
	warnings       []*Error
	finished       bool
	macros         map[string]*macro
	collecting     *macro // макрос, тело которого сейчас собирается
	macroDepth     int
	macroCounter   int
	macroLines     int

	// для языкового сервера
	ReadFile func(path string) ([]byte, error) // чтение файлов (nil — с диска)
	Defs     []*Def                            // все объявления имён
	lastDef  map[string]*Def
	structs  map[string]*structInfo
}

func NewParser() *Parser {
	p := &Parser{
		varDefs:        map[string]*DataStmt{},
		bits:           16,
		callArgs:       map[string][]*Reg{},
		pendingStrings: map[string]string{},
		stringFirstUse: map[string]Stmt{},
		referenced:     map[string]bool{},
		macros:         map[string]*macro{},
		structs:        map[string]*structInfo{},
	}
	p.hl = &highLevel{p: p}
	return p
}

// ------------------------------------------------ файлы

func (p *Parser) readSource(path string) (string, *Error) {
	data, err := p.readFile(path)
	if err != nil {
		return "", &Error{Message: fmt.Sprintf("не удалось открыть файл '%s': %s", path, Strerror(err)), Col: NoCol}
	}
	if !utf8.Valid(data) {
		return "", &Error{Message: fmt.Sprintf("файл '%s' не в кодировке UTF-8", path), Col: NoCol}
	}
	text := strings.TrimPrefix(string(data), "\ufeff")
	return universalNewlines(text), nil
}

func absPath(path string) string {
	a, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return a
}

// ParseFile разбирает файл; ошибка открытия файла возвращается сразу.
func (p *Parser) ParseFile(path string, loc *SourceLoc) {
	apath := absPath(path)
	for _, s := range p.includeStack {
		if s == apath {
			failAt(fmt.Sprintf("циклическое подключение файла '%s'", path), loc, NoCol)
		}
	}
	if len(p.includeStack) >= maxIncludeDepth {
		failAt("слишком глубокая вложенность include", loc, NoCol)
	}
	text, e := p.readSource(path)
	if e != nil {
		e.Loc = loc
		panic(e)
	}
	p.includeStack = append(p.includeStack, apath)
	defer func() { p.includeStack = p.includeStack[:len(p.includeStack)-1] }()
	p.ParseText(text, path, filepath.Dir(apath))
}

// ParseText разбирает текст программы. baseDir — папка для include ("" — текущая).
func (p *Parser) ParseText(text, filename, baseDir string) {
	if baseDir == "" {
		baseDir, _ = os.Getwd()
	}
	depth := len(p.hl.blocks)
	for i, line := range splitLines(text) {
		loc := &SourceLoc{File: filename, Line: i + 1, Text: line}
		p.lines = append(p.lines, loc)
		p.currentLoc = loc
		p.stringsWaiting = nil
		if p.collecting != nil {
			p.collectMacroLine(line, loc)
			continue
		}
		if e := catch(func() {
			toks := Tokenize(line, loc, 0, true)
			if len(toks) > 0 {
				p.parseLine(toks, loc, baseDir)
			}
		}); e != nil {
			if e.Loc == nil {
				e.Loc = loc
			}
			p.errors = append(p.errors, e)
		}
	}
	if p.collecting != nil {
		p.errors = append(p.errors, &Error{Message: "macro без end", Loc: p.collecting.loc, Col: NoCol})
		p.collecting = nil
	}
	p.hl.closeFile(depth, &p.errors)
}

// ------------------------------------------------ имена

func (p *Parser) checkName(tok *Token) {
	if Reserved[lower(tok.Value)] {
		failCol(fmt.Sprintf("'%s' — зарезервированное слово, его нельзя использовать как имя", tok.Value), tok.Col)
	}
}

func (p *Parser) fullName(tok *Token) string {
	name := tok.Value
	if strings.HasPrefix(name, ".") {
		if !p.hasGlobal {
			failCol(fmt.Sprintf("локальная метка '%s' без предшествующей глобальной метки", name), tok.Col)
		}
		return p.lastGlobal + name
	}
	return name
}

func (p *Parser) readFile(path string) ([]byte, error) {
	if p.ReadFile != nil {
		return p.ReadFile(path)
	}
	return os.ReadFile(path)
}

func (p *Parser) defineLabel(tok *Token, loc *SourceLoc) *LabelStmt {
	p.checkName(tok)
	name := p.fullName(tok)
	if !strings.HasPrefix(tok.Value, ".") && !strings.HasPrefix(tok.Value, "@") { // @… — метки внутри макросов
		p.lastGlobal, p.hasGlobal = name, true
	}
	st := &LabelStmt{Name: name, Loc: loc, Col: tok.Col}
	p.addDef(&Def{Name: name, Kind: "label", Loc: loc, Col: tok.Col})
	p.statements = append(p.statements, st)
	p.pendingLabels = append(p.pendingLabels, name)
	p.defined = append(p.defined, definedLabel{name, loc, tok.Col})
	if !p.hasFirstLabel && !strings.HasPrefix(name, "@") {
		p.firstLabel, p.hasFirstLabel = name, true
	}
	return st
}

func (p *Parser) emit(st Stmt) {
	if d, ok := st.(*DataStmt); ok {
		for _, name := range p.pendingLabels {
			p.varDefs[name] = d
			if def := p.lastDef[name]; def != nil && def.Kind == "label" {
				def.Kind = "var"
			}
		}
	}
	p.pendingLabels = nil
	for _, label := range p.stringsWaiting {
		if _, ok := p.stringFirstUse[label]; !ok {
			p.stringFirstUse[label] = st
		}
	}
	p.stringsWaiting = nil
	p.statements = append(p.statements, st)
}

// ------------------------------------------------ строки

func (p *Parser) parseLine(toks []*Token, loc *SourceLoc, baseDir string) {
	if n := len(p.hl.blocks); n > 0 {
		blk := p.hl.blocks[n-1]
		if blk.kind == "struct" && statementWord(toks) != "end" {
			p.structField(blk, toks, loc)
			return
		}
		if blk.kind == "proc" && blk.header && !toks[0].IsID("local") {
			p.hl.prologue(blk, loc)
		}
	}
	ts := newStream(toks, loc)
	// метки "имя:" в начале строки
	for {
		t0, t1 := ts.peek(0), ts.peek(1)
		if t0 != nil && t0.Kind == ID && t1 != nil && t1.IsOp(":") {
			if _, isUnit := dataUnits[lower(t0.Value)]; !isUnit {
				p.defineLabel(t0, loc)
				ts.pos += 2
				continue
			}
		}
		break
	}
	if ts.atEnd() {
		return
	}
	t0, t1 := ts.peek(0), ts.peek(1)
	// константа: ИМЯ = выражение  /  ИМЯ equ выражение
	if t0.Kind == ID && t1 != nil && (t1.IsOp("=") || t1.IsID("equ")) {
		p.checkName(t0)
		name := p.fullName(t0)
		ts.pos += 2
		ts.toks = minusSeps(ts.toks) // у константы нет операндов: ' - ' — это минус
		expr := p.parseExpr(ts, "")
		ts.expectEnd("строки")
		p.emit(&ConstStmt{Name: name, Expr: expr, Loc: loc, Col: t0.Col})
		p.addDef(&Def{Name: name, Kind: "const", Loc: loc, Col: t0.Col,
			Detail: name + " = " + strings.TrimSpace(tokensText(loc, ts.toks[2:]))})
		return
	}
	p.parseStatement(ts, loc, baseDir, true)
}

func (p *Parser) isData(ts *tokenStream, k int) bool {
	t, s := ts.peek(k), ts.peek(k+1)
	if t == nil || t.Kind != ID || s == nil || s.Kind != OP || s.Space {
		return false
	}
	if _, ok := dataUnits[lower(t.Value)]; !ok {
		return false
	}
	return s.Value == ":" || s.Value == "-" || s.Value == "/"
}

func (p *Parser) finishStmt(st Stmt, emit bool) Stmt {
	if emit {
		p.emit(st)
	}
	return st
}

// parseStatement разбирает один оператор. emit=false — вернуть его (для &&).
func (p *Parser) parseStatement(ts *tokenStream, loc *SourceLoc, baseDir string, emit bool) Stmt {
	t := ts.peek(0)

	if emit && !t.IsOp("&&") { // условие в конце строки: ret if carry
		for j := ts.pos + 1; j < len(ts.toks); j++ {
			if ts.toks[j].IsID("if") {
				p.parsePostfixIf(ts, j, loc, baseDir)
				return nil
			}
		}
	}

	if t.IsOp("&&") {
		ts.next()
		if ts.atEnd() {
			ts.error("после && ожидается число повторений")
		}
		count := p.parseExpr(ts, "")
		if ts.atEnd() {
			ts.error("после числа повторений ожидается команда или данные: && N b: 0")
		}
		if ts.peek(0).Kind == SEP {
			ts.error(sepHint)
		}
		if !ts.peek(0).IsID("if") {
			for _, x := range ts.toks[ts.pos:] {
				if x.IsID("if") {
					ts.error("условие в конце строки нельзя сочетать с &&")
				}
			}
		}
		body := p.parseStatement(ts, loc, baseDir, false)
		if body == nil {
			ts.error("&& повторяет одну команду или директиву данных")
		}
		st := &TimesStmt{Count: count, Body: body, Loc: loc}
		if emit {
			p.emit(st)
		}
		return st
	}

	if p.isData(ts, 0) {
		st := p.parseData(ts, loc)
		if emit {
			p.emit(st)
		}
		return st
	}

	// переменная без двоеточия: msg b: "Hello" - 0
	if t.Kind == ID && p.isData(ts, 1) {
		if !emit {
			ts.error("внутри && нельзя объявлять метку")
		}
		p.defineLabel(ts.next(), loc)
		st := p.parseData(ts, loc)
		p.emit(st)
		return st
	}

	if t.Kind != ID {
		ts.error(fmt.Sprintf("ожидалась команда, а встретилось '%s'", t.Text))
	}
	word := lower(t.Value)

	if m, ok := p.macros[t.Value]; ok {
		if !emit {
			ts.error("макрос нельзя повторять через && — повторите команды внутри макроса")
		}
		ts.next()
		p.expandMacro(m, t, ts.rest(), loc, baseDir)
		return nil
	}

	if st, ok := p.structs[t.Value]; ok && !(ts.peek(1) != nil && ts.peek(1).IsOp(":")) {
		ts.next()
		return p.structInstance(st, t, ts.rest(), loc, emit)
	}

	if blockWords[word] {
		if !emit {
			ts.error(word + " нельзя повторять через &&")
		}
		ts.next()
		rest := ts.rest()
		switch word {
		case "macro":
			p.wordMacro(t, rest, loc)
		case "proc":
			p.wordProc(t, rest, loc)
		case "struct":
			p.wordStruct(t, rest, loc)
		case "at":
			p.wordAt(t, rest, loc)
		case "local":
			p.wordLocal(t, rest, loc)
		case "return":
			p.wordReturn(t, rest, loc)
		}
		return nil
	}

	if controlWords[word] {
		if !emit {
			ts.error(word + " нельзя повторять через &&")
		}
		ts.next()
		p.hl.statement(word, t, ts.rest(), loc)
		return nil
	}

	switch word {
	case "og":
		ts.next()
		expr := p.parseExpr(ts, "")
		ts.expectEnd("строки")
		return p.finishStmt(&OrgStmt{Expr: expr, Loc: loc}, emit)

	case "b": // b 16 / b 32 — режим
		ts.next()
		if ts.atEnd() {
			ts.error("после b ожидается режим (b 16 или b 32), либо данные (b: ...)")
		}
		expr := p.parseExpr(ts, "")
		ts.expectEnd("строки")
		if v := foldConst(expr); v != nil && (eqInt(v, 16) || eqInt(v, 32) || eqInt(v, 64)) {
			p.bits = small(v)
		}
		return p.finishStmt(&BitsStmt{Expr: expr, Loc: loc}, emit)

	case "align":
		ts.next()
		groups := splitSeps(ts.rest())
		if (len(groups) != 1 && len(groups) != 2) || !allNonEmpty(groups) {
			ts.error("формат: align N  или  align N - байт_заполнения")
		}
		var exprs []Expr
		for _, g := range groups {
			exprs = append(exprs, p.parseExprTokens(g, loc, ""))
		}
		st := &AlignStmt{Expr: exprs[0], Loc: loc}
		if len(exprs) > 1 {
			st.Fill = exprs[1]
		}
		return p.finishStmt(st, emit)

	case "include":
		if !emit {
			ts.error("include нельзя повторять через &&")
		}
		ts.next()
		path := p.parsePath(ts, "include")
		ts.expectEnd("строки")
		p.ParseFile(resolvePath(path, baseDir), loc)
		return nil

	case "incbin":
		ts.next()
		path := p.parsePath(ts, "incbin")
		var extra []Expr
		if !ts.atEnd() {
			if ts.peek(0).Kind != SEP {
				ts.error("формат: incbin \"файл\" [- смещение [- длина]]")
			}
			ts.next()
			for _, g := range splitSeps(ts.rest()) {
				extra = append(extra, p.parseExprTokens(g, loc, ""))
			}
		}
		full := resolvePath(path, baseDir)
		data, err := p.readFile(full)
		if err != nil {
			failAt(fmt.Sprintf("не удалось открыть файл '%s': %s", path, Strerror(err)), loc, NoCol)
		}
		var vals []int
		for _, e := range extra {
			v := foldConst(e)
			if v == nil || v.Sign() < 0 {
				failAt("смещение и длина в incbin должны быть неотрицательными числами", loc, NoCol)
			}
			vals = append(vals, small(v))
		}
		if len(vals) > 2 {
			failAt("формат: incbin \"файл\" [- смещение [- длина]]", loc, NoCol)
		}
		if len(vals) > 0 {
			data = data[min(vals[0], len(data)):]
		}
		if len(vals) > 1 {
			data = data[:min(vals[1], len(data))]
		}
		return p.finishStmt(&IncbinStmt{Data: data, Loc: loc}, emit)

	case "do":
		return p.finishStmt(p.parseDo(ts, loc), emit)

	case "pool": // сюда кладутся строки из команд выше
		if !emit {
			ts.error("pool нельзя повторять через &&")
		}
		ts.next()
		ts.expectEnd("строки")
		st := &PoolStmt{Loc: loc}
		for _, value := range p.pendingOrder {
			st.Entries = append(st.Entries, poolEntry{p.pendingStrings[value], []byte(value)})
		}
		p.pendingStrings = map[string]string{}
		p.pendingOrder = nil
		p.emit(st)
		return nil

	case "args": // args puts - esi
		if !emit {
			ts.error("args нельзя повторять через &&")
		}
		ts.next()
		p.parseArgs(ts, loc)
		return nil
	}

	// префиксы: rep, lock, сегментные
	var prefixes []byte
	for t != nil && t.Kind == ID {
		low := lower(t.Value)
		nxt := ts.peek(1)
		if b, ok := prefixBytes[low]; ok && nxt != nil {
			prefixes = append(prefixes, b)
		} else if b, ok := segPrefix[low]; ok && nxt != nil && nxt.Kind == ID {
			prefixes = append(prefixes, b)
		} else {
			break
		}
		ts.next()
		t = ts.peek(0)
	}
	if t == nil || t.Kind != ID {
		ts.error("после префикса ожидается команда")
	}

	mn := Canonical(t.Value)
	if mn == "" {
		nxt := ts.peek(1)
		hint := suggestCommand(t.Value)
		if hint == "" {
			if nxt == nil && !Reserved[lower(t.Value)] {
				hint = fmt.Sprintf(" (если это метка — добавьте двоеточие: %s:)", t.Value)
			} else if nxt != nil && nxt.IsOp("-") && !nxt.Space {
				hint = " (разделитель операндов — ' - ' с пробелами)"
			}
		}
		ts.errorAt(fmt.Sprintf("неизвестная команда '%s'%s", t.Value, hint), t)
	}
	ts.next()
	operands := p.parseOperands(ts.rest(), loc)
	checkStringOperands(operands, loc, t.Col)
	if (mn == "push" || mn == "pop") && len(operands) > 1 {
		// push eax - ebx - ecx; pop снимает в обратном порядке: pop eax - ebx - ecx
		if !emit {
			ts.errorAt("&& повторяет одну команду — список в push/pop здесь нельзя", t)
		}
		for i := range operands {
			op := operands[i]
			if mn == "pop" {
				op = operands[len(operands)-1-i]
			}
			p.emit(newInstr(prefixes, mn, []Operand{op}, loc, t.Value))
		}
		return nil
	}
	if (mn == "call" || mn == "jmp") && len(operands) > 1 {
		if !emit {
			ts.errorAt("вызов с аргументами нельзя повторять через &&", t)
		}
		st := &CallStmt{Mnemonic: mn, Prefixes: prefixes, Target: operands[0], Args: operands[1:], Bits: p.bits, Loc: loc}
		p.emit(st)
		return st
	}
	return p.finishStmt(newInstr(prefixes, mn, operands, loc, t.Value), emit)
}

// parsePostfixIf: команда if условие — выполнить команду, только если условие истинно.
func (p *Parser) parsePostfixIf(ts *tokenStream, idx int, loc *SourceLoc, baseDir string) {
	stmt, ifTok, cond := ts.toks[ts.pos:idx], ts.toks[idx], ts.toks[idx+1:]
	if len(cond) == 0 {
		failAt("после if ожидается условие: ret if carry", loc, ifTok.Col)
	}
	first := stmt[0]
	word := ""
	if first.Kind == ID {
		word = lower(first.Value)
	}
	if (word == "break" || word == "continue") && len(stmt) == 1 {
		p.hl.loopJump(word, first, ifTok, cond, loc)
		return
	}
	if word == "return" {
		blk := p.hl.procBlock(first)
		acc := FamilyNames[blk.bits][0]
		if len(stmt) == 1 || (len(stmt) == 2 && stmt[1].IsID(acc)) { // return [eax] if … — один переход
			p.hl.condJump(blk.exit, ifTok, cond, loc, true)
			return
		}
	}
	if (controlWords[word] && word != "let") || word == "macro" || word == "proc" || word == "struct" ||
		word == "at" || word == "local" || first.IsOp("&&") {
		failAt(fmt.Sprintf("'%s' нельзя дополнить условием в конце строки", first.Text), loc, first.Col)
	}
	hasSep := false
	for _, x := range stmt {
		hasSep = hasSep || x.Kind == SEP
	}
	if Canonical(word) == "jmp" && len(stmt) > 1 && !hasSep {
		op := p.parseOperand(stmt[1:], loc)
		if o, ok := op.(*ImmOperand); ok && o.Jump != "far" {
			if sym, ok := o.Expr.(*Sym); ok {
				p.hl.condJump(sym.Name, ifTok, cond, loc, true) // один условный переход
				return
			}
		}
	}
	skip := p.hl.newLabel("skip")
	p.hl.condJump(skip, ifTok, cond, loc, false)
	p.parseStatement(newStream(stmt, loc), loc, baseDir, true)
	p.hl.label(skip, loc)
}

func (p *Parser) parseArgs(ts *tokenStream, loc *SourceLoc) {
	groups := splitSeps(ts.rest())
	const format = "формат: args подпрограмма - регистр - регистр ... (например: args puts - esi)"
	if len(groups) < 2 || !allNonEmpty(groups) || len(groups[0]) != 1 || groups[0][0].Kind != ID {
		failAt(format, loc, NoCol)
	}
	name := p.fullName(groups[0][0])
	var regs []*Reg
	for _, g := range groups[1:] {
		var reg *Reg
		if len(g) == 1 && g[0].Kind == ID {
			reg = Registers[lower(g[0].Value)]
		}
		if reg == nil || reg.Kind != "gpr" {
			failAt("в args перечисляются регистры общего назначения", loc, g[0].Col)
		}
		regs = append(regs, reg)
	}
	if _, ok := p.callArgs[name]; ok {
		failAt(fmt.Sprintf("аргументы '%s' уже объявлены", name), loc, groups[0][0].Col)
	}
	p.callArgs[name] = regs
}

// internString — строка из команды → метка. Одинаковые строки хранятся один раз.
func (p *Parser) internString(value []byte) string {
	key := string(value)
	label, ok := p.pendingStrings[key]
	if !ok {
		p.stringCounter++
		label = fmt.Sprintf("@str%d", p.stringCounter)
		p.pendingStrings[key] = label
		p.pendingOrder = append(p.pendingOrder, key)
	}
	if _, ok := p.stringFirstUse[label]; !ok {
		p.stringsWaiting = append(p.stringsWaiting, label)
	}
	return label
}

// ------------------------------------------------ после разбора

// finish размещает строки, разворачивает вызовы с аргументами, собирает предупреждения.
func (p *Parser) finish() {
	if p.finished {
		return
	}
	p.finished = true
	inline := map[Stmt][]poolEntry{} // строки без pool — прямо в код с обходом
	for _, value := range p.pendingOrder {
		label := p.pendingStrings[value]
		if st, ok := p.stringFirstUse[label]; ok {
			inline[st] = append(inline[st], poolEntry{label, []byte(value)})
		}
	}
	var out []Stmt
	for _, st := range p.statements {
		if entries := inline[st]; len(entries) > 0 {
			over := entries[0].Label + ".over"
			out = append(out, newInstr(nil, "jmp", []Operand{imm(&Sym{over, NoCol})}, st.Location(), ""))
			out = append(out, stringData(entries, st.Location())...)
			out = append(out, &LabelStmt{Name: over, Loc: st.Location(), Col: NoCol})
		}
		switch s := st.(type) {
		case *PoolStmt:
			out = append(out, stringData(s.Entries, s.Loc)...)
		case *CallStmt:
			if e := catch(func() { out = append(out, p.expandCall(s)...) }); e != nil {
				if e.Loc == nil {
					e.Loc = s.Loc
				}
				p.errors = append(p.errors, e)
			}
		default:
			out = append(out, st)
		}
	}
	p.statements = out
	for _, d := range p.defined {
		if !strings.HasPrefix(d.name, "@") && d.name != p.firstLabel && !p.referenced[d.name] {
			p.warnings = append(p.warnings, &Error{Message: fmt.Sprintf("метка '%s' нигде не используется", d.name),
				Loc: d.loc, Col: d.col, Warning: true})
		}
	}
}

// expandCall: call f - a - b → mov рег1 - a / mov рег2 - b / call f
func (p *Parser) expandCall(st *CallStmt) []Stmt {
	target := st.Target
	name, hasName := "", false
	if o, ok := target.(*ImmOperand); ok {
		if sym, ok := o.Expr.(*Sym); ok {
			name, hasName = sym.Name, true
		}
	}
	regs, declared := p.callArgs[name]
	if !hasName || !declared {
		defaults := defaultArgs[st.Bits]
		if len(st.Args) > len(defaults) {
			fail(fmt.Sprintf("слишком много аргументов (больше %d)", len(defaults)))
		}
		regs = nil
		for _, r := range defaults[:len(st.Args)] {
			regs = append(regs, Registers[r])
		}
	}
	if len(regs) != len(st.Args) {
		names := make([]string, len(regs))
		for i, r := range regs {
			names[i] = r.Name
		}
		fail(fmt.Sprintf("%s принимает %d аргумент(а) (%s), а передано %d", pyStrName(name, hasName), len(regs),
			strings.Join(names, " - "), len(st.Args)))
	}
	type move struct {
		r *Reg
		a Operand
	}
	var moves []move
	for i, r := range regs {
		a := st.Args[i]
		if ro, ok := a.(*RegOperand); ok && ro.Reg == r {
			continue
		}
		moves = append(moves, move{r, a})
	}
	var ordered []move
	for len(moves) > 0 { // сначала те, чей регистр больше никому не нужен
		found := false
		for i, m := range moves {
			free := true
			for j, m2 := range moves {
				if j != i && reads(m2.a)[family(m.r)] {
					free = false
					break
				}
			}
			if free {
				ordered = append(ordered, m)
				moves = append(moves[:i:i], moves[i+1:]...)
				found = true
				break
			}
		}
		if !found {
			fail("аргументы зависят друг от друга по кругу (например, call f - ebx - eax " +
				"при args f - eax - ebx) — передайте их через другие регистры")
		}
	}
	targetRegs := reads(target)
	for _, m := range ordered {
		if targetRegs[family(m.r)] {
			fail("адрес вызова зависит от регистра, в который передаётся аргумент")
		}
	}
	var out []Stmt
	for _, m := range ordered {
		out = append(out, widenMove(m.r, m.a, st.Loc))
	}
	out = append(out, newInstr(st.Prefixes, st.Mnemonic, []Operand{target}, st.Loc, ""))
	return out
}

// widenMove — mov r - a; меньший регистр расширяется нулями (из 32 в 64 бита — mov в 32-битную часть).
func widenMove(r *Reg, a Operand, loc *SourceLoc) *InstrStmt {
	if ro, ok := a.(*RegOperand); ok && ro.Reg.Kind == "gpr" && ro.Reg.Size < r.Size {
		if ro.Reg.Size == 32 {
			low := Registers[FamilyNames[32][r.Num]]
			return newInstr(nil, "mov", []Operand{&RegOperand{low}, a}, loc, "")
		}
		return newInstr(nil, "movzx", []Operand{&RegOperand{r}, a}, loc, "")
	}
	return newInstr(nil, "mov", []Operand{&RegOperand{r}, a}, loc, "")
}

// pyStrName — имя подпрограммы так, как его напечатал бы Python (None, если имени нет).
func pyStrName(name string, ok bool) string {
	if !ok {
		return "None"
	}
	return name
}

func (p *Parser) parsePath(ts *tokenStream, what string) string {
	t := ts.peek(0)
	if t == nil || t.Kind != STR {
		ts.error(fmt.Sprintf("после %s ожидается имя файла в кавычках", what))
	}
	ts.next()
	return decodeReplace(t.Bytes)
}

func resolvePath(path, baseDir string) string {
	if filepath.IsAbs(path) {
		return path
	}
	return filepath.Join(baseDir, path)
}

// ------------------------------------------------ данные

func (p *Parser) parseData(ts *tokenStream, loc *SourceLoc) *DataStmt {
	ut := ts.next()
	name := lower(ut.Value)
	unit := dataUnits[name]
	kindTok := ts.next()
	if name == "s" {
		if kindTok.Value != ":" {
			ts.errorAt("s: — строка с нулём в конце; для N нулевых байт используйте b-N", kindTok)
		}
		items := append(p.parseItems(ts.rest(), loc), numNode(0))
		return &DataStmt{Unit: 1, Kind: "list", Items: items, Loc: loc, Name: "s"}
	}
	switch kindTok.Value {
	case ":":
		items := p.parseItems(ts.rest(), loc)
		return &DataStmt{Unit: unit, Kind: "list", Items: items, Loc: loc, Name: name}
	case "-":
		if ts.atEnd() {
			ts.error(fmt.Sprintf("после %s- ожидается количество: %s-N", name, name))
		}
		count := p.parseExpr(ts, "")
		if !ts.atEnd() && ts.peek(0).Kind == SEP {
			ts.error(fmt.Sprintf("%s-N заполняет нулями и не принимает значений; "+
				"для значений используйте %s/ N: значения", name, name))
		}
		ts.expectEnd("строки")
		return &DataStmt{Unit: unit, Kind: "fill", Count: count, Items: []Expr{}, Loc: loc, Name: name}
	}
	// b/ N: значения
	if ts.atEnd() {
		ts.error(fmt.Sprintf("после %s/ ожидается количество: %s/ N: значения", name, name))
	}
	count := p.parseExpr(ts, "")
	if !ts.atEnd() && (ts.peek(0).IsOp(":") || ts.peek(0).Kind == SEP) {
		ts.next()
	}
	items := p.parseItems(ts.rest(), loc)
	if len(items) == 0 {
		failAt(fmt.Sprintf("%s/ N требует инициализации: %s/ N: значения", name, name), loc, kindTok.Col)
	}
	return &DataStmt{Unit: unit, Kind: "fixed", Count: count, Items: items, Loc: loc, Name: name}
}

func (p *Parser) parseItems(toks []*Token, loc *SourceLoc) []Expr {
	items := []Expr{}
	if len(toks) == 0 {
		return items
	}
	for _, g := range splitSeps(toks) {
		if len(g) == 0 {
			failAt("пустое значение (лишний разделитель ' - ')", loc, toks[0].Col)
		}
		items = append(items, p.parseExprTokens(g, loc, ""))
	}
	return items
}

// ------------------------------------------------ do

func (p *Parser) parseDo(ts *tokenStream, loc *SourceLoc) *DoStmt {
	doTok := ts.next()
	t := ts.peek(0)
	if t == nil || t.Kind != STR || t.Quote != '"' {
		at := t
		if at == nil {
			at = doTok
		}
		ts.errorAt("после do ожидается выражение в двойных кавычках: do \"4+4\" - ax", at)
	}
	ts.next()
	sep := ts.peek(0)
	if sep == nil || sep.Kind != SEP {
		ts.errorAt("после выражения do ожидается ' - ' и приёмник: do \"4+4\" - ax", sep)
	}
	ts.next()
	destToks := ts.rest()
	if len(destToks) == 0 {
		failAt("не указан приёмник результата do", loc, sep.Col)
	}
	for _, x := range destToks {
		if x.Kind == SEP {
			failAt("у do только один приёмник", loc, x.Col)
		}
	}
	dest := p.parseOperand(destToks, loc)
	inner := decodeReplace(t.Bytes)
	itoks := Tokenize(inner, loc, t.Col+1, false)
	if len(itoks) == 0 {
		failAt("пустое выражение do", loc, t.Col)
	}
	its := newStream(itoks, loc)
	expr := p.parseExpr(its, "do")
	its.expectEnd("выражения")
	return &DoStmt{Expr: expr, Dest: dest, Loc: loc, Flags: map[string]int{}}
}

// ------------------------------------------------ операнды

func (p *Parser) parseOperands(toks []*Token, loc *SourceLoc) []Operand {
	if len(toks) == 0 {
		return nil
	}
	for _, t := range toks {
		if t.IsOp(",") {
			failAt("операнды разделяются ' - ' (дефис с пробелами), а не запятой", loc, t.Col)
		}
	}
	var ops []Operand
	for _, g := range splitSeps(toks) {
		if len(g) == 0 {
			for _, t := range toks {
				if t.Kind == SEP {
					failAt("пустой операнд (лишний разделитель ' - ')", loc, t.Col)
				}
			}
		}
		ops = append(ops, p.parseOperand(g, loc))
	}
	return ops
}

func (p *Parser) parseOperand(toks []*Token, loc *SourceLoc) Operand {
	ts := newStream(toks, loc)
	size, jump := 0, ""
	for {
		t := ts.peek(0)
		if t != nil && t.Kind == ID && ts.peek(1) != nil {
			low := lower(t.Value)
			if s, ok := sizeWords[low]; ok {
				if size != 0 {
					ts.error("размер указан дважды")
				}
				size = s
				ts.next()
				continue
			}
			if jumpWords[low] {
				if jump != "" {
					ts.error("тип перехода указан дважды")
				}
				jump = low
				ts.next()
				continue
			}
		}
		break
	}
	t := ts.peek(0)
	if t == nil {
		ts.error("ожидался операнд")
	}

	if t.Kind == STR && t.Quote == '"' && ts.peek(1) == nil {
		// "строка" в команде — адрес строки с нулём в конце (её кладёт компилятор)
		label := p.internString(t.Bytes)
		return &ImmOperand{Expr: &Sym{label, t.Col}, Size: size, Jump: jump, FromString: true}
	}

	if t.IsID("a20") && ts.peek(1) == nil {
		return &A20Operand{}
	}

	if t.IsOp("[") {
		mem := p.parseMem(ts, size)
		mem.Jump = jump
		ts.expectEnd("операнда")
		return mem
	}

	if t.Kind == ID {
		if reg, ok := Registers[lower(t.Value)]; ok {
			nxt := ts.peek(1)
			if nxt == nil {
				if size != 0 && reg.Kind == "gpr" && size != reg.Size {
					ts.errorAt("размер не совпадает с регистром "+reg.Name, t)
				}
				return &RegOperand{reg}
			}
			// es:[di] — сегмент перед скобкой
			if reg.Kind == "seg" && nxt.IsOp(":") && ts.peek(2) != nil && ts.peek(2).IsOp("[") {
				ts.pos += 2
				mem := p.parseMem(ts, size)
				if mem.Seg != nil {
					ts.error("сегмент указан дважды")
				}
				mem.Seg = reg
				mem.Jump = jump
				ts.expectEnd("операнда")
				return mem
			}
		}
	}

	expr := p.parseExpr(ts, "")
	if ts.peek(0) != nil && ts.peek(0).IsOp(":") {
		ts.next()
		off := p.parseExpr(ts, "")
		ts.expectEnd("операнда")
		for _, part := range []Expr{expr, off} {
			if r := firstReg(part); r != nil {
				failAt("в дальнем адресе сегмент:смещение не может быть регистров", loc, r.C)
			}
		}
		return &FarOperand{Seg: expr, Off: off, Size: size}
	}
	ts.expectEnd("операнда")
	if r := firstReg(expr); r != nil {
		if r.Local != "" {
			acc := FamilyNames[r.Reg.Size][0]
			failAt(fmt.Sprintf("'%s' — локальная переменная (лежит в стеке): значение — [%s], адрес — lea %s - [%s]",
				r.Local, r.Local, acc, r.Local), loc, r.C)
		}
		msg := fmt.Sprintf("регистр %s нельзя использовать в выражении", r.Reg.Name)
		for _, x := range toks {
			if x.IsOp("-") {
				msg += " (" + sepHint + ")"
				break
			}
		}
		failAt(msg, loc, r.C)
	}
	return &ImmOperand{Expr: expr, Size: size, Jump: jump}
}

func (p *Parser) parseMem(ts *tokenStream, size int) *MemOperand {
	openTok := ts.next() // '['
	var seg *Reg
	t0, t1 := ts.peek(0), ts.peek(1)
	if t0 != nil && t0.Kind == ID && t1 != nil && t1.IsOp(":") {
		low := lower(t0.Value)
		for _, s := range segments {
			if s == low {
				seg = Registers[low]
				ts.pos += 2
				break
			}
		}
	}
	mode := ""
	if t0, t1 := ts.peek(0), ts.peek(1); t0 != nil && t0.IsID("rel", "abs") && t1 != nil && !t1.IsOp("]") {
		mode = lower(t0.Value) // [abs 0xB8000], [rel msg] — для режима b 64
		ts.pos++
	}
	if ts.peek(0) != nil && ts.peek(0).IsOp("]") {
		ts.error("пустой адрес []")
	}
	if t0, t1 := ts.peek(0), ts.peek(1); size == 0 && t0 != nil && t0.Kind == ID && t1 != nil && t1.IsOp("]") {
		if v, ok := p.localVar(t0.Value); ok {
			size = v.memSize // [count] — размер из local count - dword
		}
	}
	expr := p.parseExpr(ts, "mem")
	closeTok := ts.peek(0)
	if closeTok == nil || !closeTok.IsOp("]") {
		ts.errorAt("ожидалась ']'", closeTok)
	}
	ts.next()
	base, index, scale, disp := p.splitAddress(expr, ts.loc, openTok.Col)
	return &MemOperand{Size: size, Seg: seg, Base: base, Index: index, Scale: scale, Disp: disp, Mode: mode}
}

type addrReg struct {
	reg   *Reg
	scale int
	node  *RegNode
}

func (p *Parser) splitAddress(node Expr, loc *SourceLoc, col int) (*Reg, *Reg, int, Expr) {
	var regs []addrReg
	type term struct {
		sign int
		n    Expr
	}
	var consts []term

	failNode := func(msg string, n Expr) {
		c := col
		if n != nil && n.Col() != NoCol {
			c = n.Col()
		}
		failAt(msg, loc, c)
	}

	var walk func(n Expr, sign int)
	walk = func(n Expr, sign int) {
		if r, ok := n.(*RegNode); ok {
			if sign < 0 {
				failNode("регистр в адресе нельзя вычитать", n)
			}
			regs = append(regs, addrReg{r.Reg, 1, r})
			return
		}
		if b, ok := n.(*Binary); ok && (b.Op == "+" || b.Op == "-") {
			walk(b.A, sign)
			if b.Op == "+" {
				walk(b.B, sign)
			} else {
				walk(b.B, -sign)
			}
			return
		}
		if b, ok := n.(*Binary); ok && b.Op == "*" && hasReg(n) {
			var r, k Expr = b.A, b.B
			if _, isReg := b.A.(*RegNode); !isReg {
				r, k = b.B, b.A
			}
			rn, isReg := r.(*RegNode)
			if !isReg || hasReg(k) {
				failNode("недопустимое выражение адреса", n)
			}
			if sign < 0 {
				failNode("регистр в адресе нельзя вычитать", r)
			}
			scale := foldConst(k)
			if scale == nil || !(eqInt(scale, 1) || eqInt(scale, 2) || eqInt(scale, 4) || eqInt(scale, 8)) {
				failNode("масштаб должен быть числом 1, 2, 4 или 8", k)
			}
			regs = append(regs, addrReg{rn.Reg, small(scale), rn})
			return
		}
		if hasReg(n) {
			failNode("недопустимое выражение адреса: регистры можно только складывать "+
				"и умножать на 1, 2, 4, 8", firstReg(n))
		}
		consts = append(consts, term{sign, n})
	}

	walk(node, 1)

	for _, r := range regs {
		if r.reg.Kind != "gpr" || r.reg.Size == 8 {
			failNode(fmt.Sprintf("регистр %s нельзя использовать в адресе", r.reg.Name), r.node)
		}
	}
	if len(regs) > 2 {
		failNode("в адресе может быть не больше двух регистров", regs[2].node)
	}
	if len(regs) == 2 && regs[0].reg.Size != regs[1].reg.Size {
		failNode("в адресе нельзя смешивать 16- и 32-битные регистры", regs[1].node)
	}

	var base, index *Reg
	scale := 1
	if len(regs) == 1 {
		r := regs[0]
		if r.scale == 1 {
			base = r.reg
		} else {
			index, scale = r.reg, r.scale
		}
	} else if len(regs) == 2 {
		r1, r2 := regs[0], regs[1]
		if r1.scale != 1 && r2.scale != 1 {
			failNode("масштаб может быть только у одного регистра", r2.node)
		}
		if r1.scale != 1 {
			base, index, scale = r2.reg, r1.reg, r1.scale
		} else {
			base, index, scale = r1.reg, r2.reg, r2.scale
		}
		if index.Name == "esp" || index.Name == "rsp" {
			if scale == 1 && base.Name != "esp" && base.Name != "rsp" {
				base, index = index, base
			} else {
				failNode(fmt.Sprintf("%s нельзя использовать как индексный регистр", index.Name), r2.node)
			}
		}
	}
	if index != nil && index.Size == 16 && scale != 1 {
		failNode("масштаб (*2, *4, *8) недоступен в 16-битной адресации", nil)
	}

	var disp Expr
	for _, c := range consts {
		t := c.n
		if c.sign < 0 {
			t = &Unary{"-", c.n, c.n.Col()}
		}
		if disp == nil {
			disp = t
		} else {
			disp = &Binary{"+", disp, t, c.n.Col()}
		}
	}
	return base, index, scale, disp
}

// ------------------------------------------------ выражения

func (p *Parser) parseExprTokens(toks []*Token, loc *SourceLoc, mode string) Expr {
	ts := newStream(toks, loc)
	e := p.parseExpr(ts, mode)
	ts.expectEnd("выражения")
	return e
}

// parseExpr — mode: "" — обычное выражение, "mem" — адрес, "do" — выражение do,
// "let" — выражение let (можно читать память: [x], byte [esi]).
func (p *Parser) parseExpr(ts *tokenStream, mode string) Expr {
	return p.binary(ts, 0, mode)
}

func (p *Parser) binary(ts *tokenStream, level int, mode string) Expr {
	if level == len(binaryLevels) {
		return p.unary(ts, mode)
	}
	left := p.binary(ts, level+1, mode)
	for {
		t := ts.peek(0)
		if t != nil && t.IsOp(binaryLevels[level]...) {
			ts.next()
			right := p.binary(ts, level+1, mode)
			left = &Binary{t.Value, left, right, t.Col}
		} else {
			return left
		}
	}
}

func (p *Parser) unary(ts *tokenStream, mode string) Expr {
	t := ts.peek(0)
	if t == nil {
		ts.error("неожиданный конец выражения")
	}
	if t.IsOp("-", "+", "~") {
		ts.next()
		return &Unary{t.Value, p.unary(ts, mode), t.Col}
	}
	return p.primary(ts, mode)
}

func (p *Parser) primary(ts *tokenStream, mode string) Expr {
	t := ts.peek(0)
	if t == nil {
		ts.error("неожиданный конец выражения")
	}
	if t.Kind == SEP {
		ts.error("ожидалось значение (" + sepHint + ")")
	}
	ts.next()
	switch t.Kind {
	case NUM:
		return &Num{t.Num, t.Col}
	case STR:
		return &Str{t.Bytes, t.Col}
	}
	if t.IsOp("(") {
		e := p.parseExpr(ts, mode)
		c := ts.peek(0)
		if c == nil || !c.IsOp(")") {
			ts.errorAt("ожидалась ')'", c)
		}
		ts.next()
		return e
	}
	if t.IsOp("$") {
		return &Here{t.Col}
	}
	if t.IsOp("$$") {
		return &Start{t.Col}
	}
	if t.IsOp("[") && mode == "do" {
		nt := ts.peek(0)
		if nt == nil || nt.Kind != ID {
			ts.errorAt("внутри [ ] в do ожидается имя переменной: [msg]", nt)
		}
		ts.next()
		c := ts.peek(0)
		if c == nil || !c.IsOp("]") {
			ts.errorAt("в do внутри [ ] пишется только имя переменной: [msg]", c)
		}
		ts.next()
		name := p.fullName(nt)
		p.referenced[name] = true
		return &Var{name, t.Col}
	}
	if mode == "let" {
		_, isSize := sizeWords[lower(t.Value)]
		if t.IsOp("[") || (t.Kind == ID && isSize && ts.peek(0) != nil && ts.peek(0).IsOp("[")) {
			size := 0
			if t.Kind == ID {
				size = sizeWords[lower(t.Value)]
			} else {
				ts.pos--
			}
			return &MemNode{p.parseMem(ts, size), t.Col}
		}
	}
	if t.Kind == ID {
		low := lower(t.Value)
		if reg, ok := Registers[low]; ok {
			return &RegNode{reg, t.Col, ""}
		}
		if Reserved[low] {
			ts.errorAt(fmt.Sprintf("'%s' нельзя использовать в выражении", t.Value), t)
		}
		if v, ok := p.localVar(t.Value); ok { // локальная переменная proc → [ebp-смещение]
			return &Binary{"+", &RegNode{p.frameReg(), t.Col, t.Value}, &Num{bi(int64(-v.offset)), t.Col}, t.Col}
		}
		name := p.fullName(t)
		p.referenced[name] = true
		return &Sym{name, t.Col}
	}
	if t.IsOp(",") {
		ts.errorAt("операнды разделяются ' - ' (дефис с пробелами), а не запятой", t)
	}
	ts.errorAt(fmt.Sprintf("неожиданное '%s' в выражении", t.Text), t)
	return nil
}
