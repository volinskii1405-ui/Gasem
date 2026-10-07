package gasem

// macro, proc, struct, at — блоки, которые закрываются словом end.

import (
	"fmt"
	"sort"
	"strings"
)

var blockWords = map[string]bool{"macro": true, "proc": true, "struct": true, "at": true, "local": true, "return": true}

// слова, которые открывают блок, закрываемый end (until — для repeat)
var openers = map[string]bool{"if": true, "while": true, "for": true, "repeat": true,
	"macro": true, "proc": true, "struct": true, "at": true}

const (
	maxMacroDepth = 32
	maxMacroLines = 100_000 // всего строк, развёрнутых из макросов (защита от взрывного роста)
)

// statementWord — первое слово оператора после меток «имя:» (в нижнем регистре) или "".
func statementWord(toks []*Token) string {
	i := 0
	for i+1 < len(toks) && toks[i].Kind == ID && toks[i+1].IsOp(":") && !isDataUnit(toks[i].Value) {
		i += 2
	}
	if i < len(toks) && toks[i].Kind == ID {
		return lower(toks[i].Value)
	}
	return ""
}

func isDataUnit(word string) bool {
	_, ok := dataUnits[lower(word)]
	return ok
}

func isIDCharRune(r rune) bool { return isAlnum(r) || r == '_' || r == '.' }

// ---------------------------------------------------------------- macro

type bodyLine struct {
	loc  *SourceLoc
	text string
}

type macro struct {
	name     string
	params   []string
	loc      *SourceLoc
	body     []bodyLine
	depth    int             // вложенность блоков при сборе тела
	internal map[string]bool // метки и константы, объявленные внутри (у каждого вызова свои)
	broken   bool            // в заголовке была ошибка: тело пропускается
	def      *Def
}

func (m *macro) isParam(name string) bool {
	for _, p := range m.params {
		if p == name {
			return true
		}
	}
	return false
}

func (m *macro) finish() {
	m.internal = map[string]bool{}
	for _, b := range m.body {
		toks, err := SafeTokenize(b.text)
		if err != nil {
			continue
		}
		i := 0
		for i+1 < len(toks) && toks[i].Kind == ID && toks[i+1].IsOp(":") && !isDataUnit(toks[i].Value) {
			m.internal[toks[i].Value] = true
			i += 2
		}
		if i+1 < len(toks) && toks[i].Kind == ID {
			n := toks[i+1]
			if n.IsOp("=") || n.IsID("equ") ||
				(n.Kind == ID && isDataUnit(n.Value) && i+2 < len(toks) && toks[i+2].Kind == OP &&
					(toks[i+2].Value == ":" || toks[i+2].Value == "-" || toks[i+2].Value == "/") && !toks[i+2].Space) {
				m.internal[toks[i].Value] = true
			}
		}
	}
	for _, p := range m.params {
		delete(m.internal, p)
	}
}

type span struct {
	start, end int
	value      string
}

// substitute подставляет аргументы вместо параметров (и в выражения do/let в кавычках).
func (m *macro) substitute(text string, args map[string]string) string {
	if len(args) == 0 {
		return text
	}
	toks, err := SafeTokenize(text)
	if err != nil {
		return text
	}
	var spans []span
	isLet := statementWord(toks) == "let"
	for k, t := range toks {
		if t.Kind == ID {
			if v, ok := args[t.Value]; ok {
				spans = append(spans, span{t.Col, t.Col + runeLen(t.Text), v})
			}
		} else if t.Kind == STR && t.Quote == '"' && (isLet || (k > 0 && toks[k-1].IsID("do"))) {
			raw := []rune(t.Text)
			inner := raw[1 : len(raw)-1]
			for i := 0; i < len(inner); {
				c := inner[i]
				if (isAlpha(c) || c == '_' || c == '.') && (i == 0 || !isIDCharRune(inner[i-1])) {
					j := i + 1
					for j < len(inner) && isIDCharRune(inner[j]) {
						j++
					}
					if v, ok := args[string(inner[i:j])]; ok {
						spans = append(spans, span{t.Col + 1 + i, t.Col + 1 + j, v})
					}
					i = j
				} else if isAlnum(c) {
					for i < len(inner) && isIDCharRune(inner[i]) {
						i++
					}
				} else {
					i++
				}
			}
		}
	}
	sort.SliceStable(spans, func(i, j int) bool {
		a, b := spans[i], spans[j]
		if a.start != b.start {
			return a.start > b.start
		}
		if a.end != b.end {
			return a.end > b.end
		}
		return a.value > b.value
	})
	rs := []rune(text)
	for _, sp := range spans {
		out := append([]rune{}, rs[:sp.start]...)
		out = append(out, []rune(sp.value)...)
		rs = append(out, rs[sp.end:]...)
	}
	return string(rs)
}

func (p *Parser) wordMacro(word *Token, toks []*Token, loc *SourceLoc) {
	// тело собирается даже при ошибке в заголовке — иначе оно разобралось бы как обычный код
	p.collecting = &macro{loc: loc, depth: 1, broken: true}
	if len(p.hl.blocks) > 0 || p.macroDepth > 0 {
		failCol("macro объявляется отдельно — не внутри if/while/for/proc и не внутри макроса", word.Col)
	}
	groups := splitSeps(toks)
	if len(groups[0]) != 1 || groups[0][0].Kind != ID || !allNonEmpty(groups) {
		failCol("формат: macro имя - параметр - параметр ...", word.Col)
	}
	nameTok := groups[0][0]
	name := nameTok.Value
	p.checkName(nameTok)
	if strings.HasPrefix(name, ".") {
		failCol("имя макроса не может начинаться с точки", nameTok.Col)
	}
	if Canonical(name) != "" || controlWords[lower(name)] {
		failCol(fmt.Sprintf("'%s' — команда Gasem, макрос так назвать нельзя", name), nameTok.Col)
	}
	if _, ok := p.macros[name]; ok {
		failCol(fmt.Sprintf("макрос '%s' уже объявлен", name), nameTok.Col)
	}
	var params []string
	for _, g := range groups[1:] {
		if len(g) != 1 || g[0].Kind != ID || strings.HasPrefix(g[0].Value, ".") {
			failCol("параметр макроса — одно имя (например: macro print - text - color)", g[0].Col)
		}
		p.checkName(g[0])
		for _, q := range params {
			if q == g[0].Value {
				failCol(fmt.Sprintf("параметр '%s' повторяется", g[0].Value), g[0].Col)
			}
		}
		params = append(params, g[0].Value)
	}
	p.collecting = &macro{name: name, params: params, loc: loc, depth: 1}
	detail := "macro " + name
	if len(params) > 0 {
		detail += " - " + strings.Join(params, " - ")
	}
	p.collecting.def = &Def{Name: name, Kind: "macro", Loc: loc, Col: nameTok.Col, Detail: detail}
	p.addDef(p.collecting.def)
	for i, g := range groups[1:] {
		p.addDef(&Def{Name: params[i], Kind: "param", Loc: loc, Col: g[0].Col, Scope: name,
			Detail: "параметр макроса " + name})
	}
}

func (p *Parser) collectMacroLine(line string, loc *SourceLoc) {
	m := p.collecting
	word := ""
	var toks []*Token
	if e := catch(func() { toks = Tokenize(line, loc, 0, true) }); e == nil {
		word = statementWord(toks)
	}
	if openers[word] {
		m.depth++
	} else if word == "end" || word == "until" {
		m.depth--
		if m.depth == 0 {
			if m.def != nil {
				m.def.EndLine = loc.Line
			}
			if !m.broken {
				m.finish()
				p.macros[m.name] = m
			}
			p.collecting = nil
			return
		}
	}
	m.body = append(m.body, bodyLine{loc, line})
}

func (p *Parser) expandMacro(m *macro, nameTok *Token, toks []*Token, loc *SourceLoc, baseDir string) {
	if p.macroDepth >= maxMacroDepth {
		panic(&Error{Message: "слишком глубокая вложенность макросов (макрос вызывает сам себя?)",
			Col: NoCol, macroLimit: true})
	}
	p.macroLines += len(m.body)
	if p.macroLines > maxMacroLines {
		panic(&Error{Message: fmt.Sprintf("из макросов получается слишком много строк (больше %d) — "+
			"макрос вызывает сам себя?", maxMacroLines), Col: NoCol, macroLimit: true})
	}
	var groups [][]*Token
	if len(toks) > 0 {
		groups = splitSeps(toks)
	}
	if len(groups) != len(m.params) || !allNonEmpty(groups) {
		names := strings.Join(m.params, " - ")
		if names == "" {
			names = "без параметров"
		}
		failCol(fmt.Sprintf("макрос %s принимает %d параметр(а) (%s), а передано %d", m.name, len(m.params),
			names, len(groups)), nameTok.Col)
	}
	text := []rune(loc.Text)
	args := map[string]string{}
	for i, name := range m.params {
		g := groups[i]
		last := g[len(g)-1]
		args[name] = string(text[g[0].Col : last.Col+runeLen(last.Text)])
	}
	p.macroCounter++
	prefix := fmt.Sprintf("@m%d.", p.macroCounter)
	via := fmt.Sprintf("в макросе '%s', вызванном в %s:%d", m.name, loc.File, loc.Line)
	if loc.Via != "" {
		via += "; " + loc.Via
	}
	depth := len(p.hl.blocks)
	saved := p.currentLoc
	p.macroDepth++
	defer func() {
		p.macroDepth--
		p.currentLoc = saved
		if r := recover(); r != nil {
			if e, ok := r.(*Error); ok && e.macroLimit {
				p.hl.blocks = p.hl.blocks[:depth]
			}
			panic(r)
		}
	}()
	for _, b := range m.body {
		nloc := &SourceLoc{File: b.loc.File, Line: b.loc.Line, Text: m.substitute(b.text, args), Origin: loc.Top(), Via: via}
		p.currentLoc = nloc
		p.stringsWaiting = nil
		if e := catch(func() {
			toks := Tokenize(nloc.Text, nloc, 0, true)
			for _, t := range toks {
				if t.Kind == ID && m.internal[t.Value] {
					t.Value = prefix + t.Value
				}
			}
			if len(toks) > 0 {
				p.parseLine(toks, nloc, baseDir)
			}
		}); e != nil {
			if e.macroLimit {
				panic(e)
			}
			if e.Loc == nil {
				e.Loc = nloc
			}
			p.errors = append(p.errors, e)
		}
	}
	for len(p.hl.blocks) > depth {
		blk := p.hl.pop()
		p.errors = append(p.errors, &Error{Message: fmt.Sprintf("%s без end в макросе '%s'", blk.kind, m.name),
			Loc: blk.loc, Col: NoCol})
	}
}

// ---------------------------------------------------------------- proc

type localVar struct {
	offset  int
	memSize int // размер [имя] по умолчанию (0 — не задан)
}

func (p *Parser) wordProc(word *Token, toks []*Token, loc *SourceLoc) {
	if len(p.hl.blocks) > 0 {
		failCol("proc объявляется отдельно — не внутри if/while/for/proc", word.Col)
	}
	idx := -1
	for i, t := range toks {
		if t.IsID("uses") {
			idx = i
			break
		}
	}
	head := toks
	var usesToks []*Token
	if idx >= 0 {
		head, usesToks = toks[:idx], toks[idx+1:]
	}
	groups := splitSeps(head)
	if len(groups[0]) != 1 || groups[0][0].Kind != ID || !allNonEmpty(groups) {
		failCol("формат: proc имя [- регистры аргументов] [uses регистры]", word.Col)
	}
	nameTok := groups[0][0]
	if strings.HasPrefix(nameTok.Value, ".") {
		failCol("имя процедуры не может начинаться с точки", nameTok.Col)
	}
	var params []*Reg
	for _, g := range groups[1:] {
		params = append(params, regWord(g, "аргументы proc — регистры общего назначения", 8, 16, 32))
	}
	var uses []*Reg
	if idx >= 0 {
		ugroups := splitSeps(usesToks)
		if !allNonEmpty(ugroups) {
			failCol("после uses перечисляются регистры: uses eax - ebx", toks[idx].Col)
		}
		for _, g := range ugroups {
			uses = append(uses, regWord(g, "в uses перечисляются 16- или 32-битные регистры", 16, 32))
		}
	}
	p.defineLabel(nameTok, loc)
	name := p.fullName(nameTok)
	if len(params) > 0 {
		if _, ok := p.callArgs[name]; ok {
			failCol(fmt.Sprintf("аргументы '%s' уже объявлены", name), nameTok.Col)
		}
		p.callArgs[name] = params
	}
	blk := p.hl.push("proc", loc)
	if def := p.lastDef[name]; def != nil && def.Loc == loc {
		def.Kind = "proc"
		def.Detail = "proc " + strings.TrimSpace(tokensText(loc, toks))
		blk.def = def
	}
	blk.name, blk.uses, blk.bits = name, uses, p.bits
	blk.locals, blk.frameSize, blk.header = map[string]localVar{}, 0, true
	blk.exit = p.hl.newLabel("ret")
}

func regWord(g []*Token, msg string, sizes ...int) *Reg {
	var reg *Reg
	if len(g) == 1 && g[0].Kind == ID {
		reg = Registers[lower(g[0].Value)]
	}
	ok := false
	if reg != nil && reg.Kind == "gpr" {
		for _, s := range sizes {
			ok = ok || reg.Size == s
		}
	}
	if !ok {
		failCol(msg, g[0].Col)
	}
	return reg
}

func (p *Parser) wordLocal(word *Token, toks []*Token, loc *SourceLoc) {
	var blk *block
	if len(p.hl.blocks) > 0 {
		blk = p.hl.blocks[len(p.hl.blocks)-1]
	}
	if blk == nil || blk.kind != "proc" || !blk.header {
		failCol("local пишется в начале proc, до первой команды", word.Col)
	}
	groups := splitSeps(toks)
	if len(groups) != 2 || !allNonEmpty(groups) || len(groups[0]) != 1 || groups[0][0].Kind != ID {
		failCol("формат: local имя - тип (byte, word, dword, qword, число байт или структура)", word.Col)
	}
	nameTok := groups[0][0]
	name := nameTok.Value
	p.checkName(nameTok)
	if strings.HasPrefix(name, ".") {
		failCol("имя локальной переменной не может начинаться с точки", nameTok.Col)
	}
	if _, ok := blk.locals[name]; ok {
		failCol(fmt.Sprintf("локальная переменная '%s' уже объявлена", name), nameTok.Col)
	}
	tt := groups[1]
	wordSize := blk.bits / 8
	elem, memSize, rest := 1, 0, tt
	if tt[0].Kind == ID {
		if sz, ok := sizeWords[lower(tt[0].Value)]; ok {
			memSize = sz
			elem, rest = sz/8, tt[1:]
		} else if st, ok := p.structs[tt[0].Value]; ok {
			elem, rest = st.size, tt[1:]
		}
	}
	count := 1
	if len(rest) > 0 {
		v := foldConst(p.parseExprTokens(rest, loc, ""))
		if v == nil || v.Sign() <= 0 {
			failCol("размер локальной переменной — положительное число", rest[0].Col)
		}
		count = small(v)
	}
	size := elem * count
	align := wordSize
	if elem == 1 || elem == 2 || elem == 4 || elem == 8 {
		align = elem
	}
	align = min(align, wordSize)
	cur := blk.frameSize + size
	cur = (cur + align - 1) / align * align
	blk.frameSize = cur
	blk.locals[name] = localVar{cur, memSize}
	where := fmt.Sprintf("[%s-%d]", frameRegName(blk.bits), cur)
	p.addDef(&Def{Name: name, Kind: "local", Loc: loc, Col: nameTok.Col, Scope: blk.name,
		Detail: "local " + strings.TrimSpace(tokensText(loc, toks)) + "    ; " + where})
}

func frameRegName(bits int) string {
	if bits == 32 {
		return "ebp"
	}
	return "bp"
}

// localVar — локальная переменная текущей proc (ok=false — такой нет).
func (p *Parser) localVar(name string) (localVar, bool) {
	for _, blk := range p.hl.blocks {
		if blk.kind == "proc" && !blk.header {
			v, ok := blk.locals[name]
			return v, ok
		}
	}
	return localVar{}, false
}

func (p *Parser) frameReg() *Reg {
	for _, blk := range p.hl.blocks {
		if blk.kind == "proc" {
			if blk.bits == 32 {
				return Registers["ebp"]
			}
			return Registers["bp"]
		}
	}
	return Registers["bp"]
}

func accFor(bits int) *Reg {
	if bits == 32 {
		return Registers["eax"]
	}
	return Registers["ax"]
}

func (p *Parser) wordReturn(word *Token, toks []*Token, loc *SourceLoc) {
	blk := p.hl.procBlock(word)
	if len(toks) > 0 {
		op := p.parseOperand(toks, loc)
		acc := accFor(blk.bits)
		for _, r := range blk.uses {
			if family(r) == 0 {
				failCol(fmt.Sprintf("return значение: %s восстанавливается из uses и затрёт результат", acc.Name), word.Col)
			}
		}
		ro, isReg := op.(*RegOperand)
		switch {
		case isReg && ro.Reg == acc:
		case isReg && ro.Reg.Kind == "gpr" && ro.Reg.Size < acc.Size:
			p.hl.instr("movzx", []Operand{&RegOperand{acc}, op}, loc)
		default:
			p.hl.instr("mov", []Operand{&RegOperand{acc}, op}, loc)
		}
	}
	p.hl.jump("jmp", blk.exit, loc)
}

// ---------------------------------------------------------------- struct

type structField struct {
	name   string
	offset int
	elem   int
	count  int
	nested *structInfo
}

type structConst struct {
	suffix string
	offset int
}

type structInfo struct {
	name   string
	loc    *SourceLoc
	fields []structField
	size   int
	consts []structConst // x, pos.x … — с вложенными полями
}

func (p *Parser) wordStruct(word *Token, toks []*Token, loc *SourceLoc) {
	if len(p.hl.blocks) > 0 {
		failCol("struct объявляется отдельно — не внутри if/while/for/proc", word.Col)
	}
	if len(toks) != 1 || toks[0].Kind != ID {
		failCol("формат: struct Имя (дальше поля, в конце end)", word.Col)
	}
	p.checkName(toks[0])
	name := toks[0].Value
	if strings.HasPrefix(name, ".") {
		failCol("имя структуры не может начинаться с точки", toks[0].Col)
	}
	if _, ok := p.structs[name]; ok {
		failCol(fmt.Sprintf("структура '%s' уже объявлена", name), toks[0].Col)
	}
	blk := p.hl.push("struct", loc)
	blk.structInfo = &structInfo{name: name, loc: loc}
	blk.def = &Def{Name: name, Kind: "struct", Loc: loc, Col: toks[0].Col, Detail: "struct " + name}
	p.addDef(blk.def)
}

func (p *Parser) structField(blk *block, toks []*Token, loc *SourceLoc) {
	st := blk.structInfo
	const format = "поле структуры: имя: тип [количество] — тип b, w, d, q или другая структура (например: x: w)"
	if len(toks) < 3 || toks[0].Kind != ID || !toks[1].IsOp(":") || toks[2].Kind != ID {
		failAt(format, loc, toks[0].Col)
	}
	name := toks[0].Value
	if strings.HasPrefix(name, ".") || name == "size" {
		failAt(fmt.Sprintf("поле не может называться '%s'", name), loc, toks[0].Col)
	}
	for _, f := range st.fields {
		if f.name == name {
			failAt(fmt.Sprintf("поле '%s' уже есть в %s", name, st.name), loc, toks[0].Col)
		}
	}
	t := toks[2]
	low := lower(t.Value)
	var nested *structInfo
	var elem int
	if u, ok := dataUnits[low]; ok && low != "s" {
		elem = u
	} else if sz, ok := sizeWords[low]; ok {
		elem = sz / 8
	} else if ns, ok := p.structs[t.Value]; ok {
		nested, elem = ns, ns.size
	} else {
		failAt(format, loc, t.Col)
	}
	count := 1
	if len(toks) > 3 {
		v := foldConst(p.parseExprTokens(toks[3:], loc, ""))
		if v == nil || v.Sign() <= 0 {
			failAt("количество элементов поля — положительное число", loc, toks[3].Col)
		}
		count = small(v)
	}
	p.addDef(&Def{Name: st.name + "." + name, Kind: "field", Loc: loc, Col: toks[0].Col,
		Detail: fmt.Sprintf("%s.%s: %s    ; смещение %d", st.name, name, strings.TrimSpace(tokensText(loc, toks[2:])), st.size)})
	st.fields = append(st.fields, structField{name, st.size, elem, count, nested})
	st.consts = append(st.consts, structConst{name, st.size})
	if nested != nil {
		for _, c := range nested.consts {
			st.consts = append(st.consts, structConst{name + "." + c.suffix, st.size + c.offset})
		}
	}
	st.size += elem * count
}

func (p *Parser) endStruct(blk *block, loc *SourceLoc) {
	st := blk.structInfo
	for _, c := range st.consts {
		p.emit(&ConstStmt{Name: st.name + "." + c.suffix, Expr: numNode(int64(c.offset)), Loc: loc, Col: NoCol})
	}
	p.emit(&ConstStmt{Name: st.name + ".size", Expr: numNode(int64(st.size)), Loc: loc, Col: NoCol})
	p.structs[st.name] = st
	if blk.def != nil {
		blk.def.EndLine = loc.Line
		blk.def.Detail = fmt.Sprintf("struct %s    ; размер %d", st.name, st.size)
	}
}

var unitNames = map[int]string{1: "b", 2: "w", 4: "d", 8: "q"}

// structInstance: Point 1 - 2 — данные по образцу структуры (неуказанные поля — нули).
func (p *Parser) structInstance(st *structInfo, nameTok *Token, toks []*Token, loc *SourceLoc, emit bool) Stmt {
	if len(toks) == 0 {
		return p.finishStmt(&DataStmt{Unit: 1, Kind: "fill", Count: numNode(int64(st.size)), Items: []Expr{},
			Loc: loc, Name: "b"}, emit)
	}
	if !emit {
		failCol("&& повторяет одну команду — структуру со значениями здесь задать нельзя", nameTok.Col)
	}
	groups := splitSeps(toks)
	if !allNonEmpty(groups) {
		failCol("пустое значение (лишний разделитель ' - ')", toks[0].Col)
	}
	if len(groups) > len(st.fields) {
		failCol(fmt.Sprintf("в структуре %s %d пол(я/ей), а значений %d", st.name, len(st.fields), len(groups)),
			groups[len(st.fields)][0].Col)
	}
	for i, f := range st.fields {
		size := f.elem * f.count
		if i >= len(groups) {
			p.emit(&DataStmt{Unit: 1, Kind: "fill", Count: numNode(int64(size)), Items: []Expr{}, Loc: loc, Name: "b"})
			continue
		}
		unit, ok := unitNames[f.elem]
		if f.nested != nil || !ok {
			failCol(fmt.Sprintf("поле %s — структура, его нельзя задать одним значением", f.name), groups[i][0].Col)
		}
		v := p.parseExprTokens(groups[i], loc, "")
		if f.count == 1 {
			p.emit(&DataStmt{Unit: f.elem, Kind: "list", Items: []Expr{v}, Loc: loc, Name: unit})
		} else {
			p.emit(&DataStmt{Unit: f.elem, Kind: "fixed", Count: numNode(int64(f.count)), Items: []Expr{v}, Loc: loc, Name: unit})
		}
	}
	return nil
}

// ---------------------------------------------------------------- at

func (p *Parser) wordAt(word *Token, toks []*Token, loc *SourceLoc) {
	for _, b := range p.hl.blocks {
		if b.kind != "at" {
			failCol("at объявляется отдельно — не внутри if/while/for/proc", word.Col)
		}
	}
	if len(toks) == 0 {
		failCol("формат: at адрес (дальше код, в конце end)", word.Col)
	}
	expr := p.parseExprTokens(minusSeps(toks), loc, "")
	p.emit(&AtStmt{Expr: expr, Loc: loc})
	p.hl.push("at", loc)
}
