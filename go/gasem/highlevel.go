package gasem

// Высокоуровневые конструкции Gasem.
//
//	if / elif / else / end          ветвления
//	while / end                     цикл с условием
//	for / end                       цикл со счётчиком
//	repeat / until                  цикл с проверкой в конце
//	break, continue                 выход из цикла / следующий шаг
//	let                             выражение, вычисляемое во время работы
//
// Всё разворачивается при разборе в обычные команды x86 и служебные метки
// (их имена начинаются с '@', поэтому не пересекаются с метками программы).
// Никакого скрытого кода времени выполнения нет — результат виден в листинге.

import (
	"fmt"
	"math/big"
)

var controlWords = map[string]bool{"if": true, "elif": true, "else": true, "end": true, "while": true,
	"for": true, "repeat": true, "until": true, "break": true, "continue": true, "let": true}

var condFlags = map[string]string{"zero": "z", "carry": "c", "sign": "s", "overflow": "o", "parity": "p"}
var relations = map[string]string{"=": "eq", "==": "eq", "!=": "ne", "<": "lt", ">": "gt", "<=": "le", ">=": "ge"}
var mirror = map[string]string{"eq": "eq", "ne": "ne", "lt": "gt", "gt": "lt", "le": "ge", "ge": "le"}
var unsignedCC = map[string]string{"eq": "e", "ne": "ne", "lt": "b", "gt": "a", "le": "be", "ge": "ae"}
var signedCC = map[string]string{"eq": "e", "ne": "ne", "lt": "l", "gt": "g", "le": "le", "ge": "ge"}
var invert = map[string]string{"e": "ne", "ne": "e", "z": "nz", "nz": "z", "b": "ae", "ae": "b", "a": "be",
	"be": "a", "l": "ge", "ge": "l", "g": "le", "le": "g", "c": "nc", "nc": "c",
	"s": "ns", "ns": "s", "o": "no", "no": "o", "p": "np", "np": "p"}

func isLoop(kind string) bool { return kind == "while" || kind == "for" || kind == "repeat" }

func hlError(msg string, tok *Token) {
	col := NoCol
	if tok != nil {
		col = tok.Col
	}
	failCol(msg, col)
}

type block struct {
	kind     string
	loc      *SourceLoc
	top      string
	cont     string
	end      string
	next     string
	hasNext  bool
	hasElse  bool
	variable Operand
	step     *big.Int
}

type highLevel struct {
	p       *Parser
	blocks  []*block
	counter int
}

// ------------------------------------------------ вывод операторов

func (h *highLevel) newLabel(what string) string {
	h.counter++
	return fmt.Sprintf("@%s%d", what, h.counter)
}

func (h *highLevel) label(name string, loc *SourceLoc) {
	h.p.statements = append(h.p.statements, &LabelStmt{Name: name, Loc: loc, Col: NoCol})
}

func (h *highLevel) instr(mn string, ops []Operand, loc *SourceLoc) {
	h.p.emit(newInstr(nil, mn, ops, loc, ""))
}

func (h *highLevel) jump(mn, target string, loc *SourceLoc) {
	h.instr(mn, []Operand{imm(&Sym{target, NoCol})}, loc)
}

// ------------------------------------------------ операторы

func (h *highLevel) statement(word string, wordTok *Token, toks []*Token, loc *SourceLoc) {
	if word == "let" {
		h.let(wordTok, toks, loc)
		return
	}
	switch word {
	case "else", "end", "repeat", "break", "continue":
		if len(toks) > 0 {
			hlError(fmt.Sprintf("после %s ничего не пишется", word), toks[0])
		}
	}

	switch word {
	case "if":
		blk := h.push("if", loc)
		blk.end = h.newLabel("endif")
		blk.next, blk.hasNext = h.newLabel("else"), true
		signed, c := h.condition(wordTok, toks)
		h.jumpIf(c, false, blk.next, signed, loc)

	case "elif":
		blk := h.top("if", "elif")
		if blk.hasElse {
			hlError("elif после else", wordTok)
		}
		h.jump("jmp", blk.end, loc)
		h.label(blk.next, loc)
		blk.next = h.newLabel("else")
		signed, c := h.condition(wordTok, toks)
		h.jumpIf(c, false, blk.next, signed, loc)

	case "else":
		blk := h.top("if", "else")
		if blk.hasElse {
			hlError("второй else в одном if", wordTok)
		}
		h.jump("jmp", blk.end, loc)
		h.label(blk.next, loc)
		blk.hasNext = false
		blk.hasElse = true

	case "while":
		blk := h.push("while", loc)
		blk.top = h.newLabel("while")
		blk.cont = blk.top
		blk.end = h.newLabel("wend")
		h.label(blk.top, loc)
		signed, c := h.condition(wordTok, toks)
		h.jumpIf(c, false, blk.end, signed, loc)

	case "for":
		h.forLoop(wordTok, toks, loc)

	case "repeat":
		blk := h.push("repeat", loc)
		blk.top = h.newLabel("repeat")
		blk.cont = h.newLabel("until")
		blk.end = h.newLabel("rend")
		h.label(blk.top, loc)

	case "until":
		if len(h.blocks) == 0 || h.blocks[len(h.blocks)-1].kind != "repeat" {
			hlError("until без repeat", wordTok)
		}
		blk := h.pop()
		h.label(blk.cont, loc)
		signed, c := h.condition(wordTok, toks)
		h.jumpIf(c, false, blk.top, signed, loc)
		h.label(blk.end, loc)

	case "break", "continue":
		loop := h.innerLoop()
		if loop == nil {
			hlError(word+" вне цикла", wordTok)
		}
		if word == "break" {
			h.jump("jmp", loop.end, loc)
		} else {
			h.jump("jmp", loop.cont, loc)
		}

	case "end":
		if len(h.blocks) == 0 {
			hlError("end без if, while или for", wordTok)
		}
		blk := h.blocks[len(h.blocks)-1]
		if blk.kind == "repeat" {
			hlError("repeat закрывается словом until, а не end", wordTok)
		}
		h.pop()
		switch blk.kind {
		case "if":
			if blk.hasNext {
				h.label(blk.next, loc)
			}
			h.label(blk.end, loc)
		case "while":
			h.jump("jmp", blk.top, loc)
			h.label(blk.end, loc)
		case "for":
			h.label(blk.cont, loc)
			switch {
			case eqInt(blk.step, 1):
				h.instr("inc", []Operand{blk.variable}, loc)
			case eqInt(blk.step, -1):
				h.instr("dec", []Operand{blk.variable}, loc)
			case blk.step.Sign() > 0:
				h.instr("add", []Operand{blk.variable, imm(&Num{blk.step, NoCol})}, loc)
			default:
				h.instr("sub", []Operand{blk.variable, imm(&Num{neg(blk.step), NoCol})}, loc)
			}
			h.jump("jmp", blk.top, loc)
			h.label(blk.end, loc)
		}
	}
}

func (h *highLevel) innerLoop() *block {
	for i := len(h.blocks) - 1; i >= 0; i-- {
		if isLoop(h.blocks[i].kind) {
			return h.blocks[i]
		}
	}
	return nil
}

// condJump — перейти на target, если условие из toks равно value.
func (h *highLevel) condJump(target string, wordTok *Token, toks []*Token, loc *SourceLoc, value bool) {
	signed, c := h.condition(wordTok, toks)
	h.jumpIf(c, value, target, signed, loc)
}

// loopJump — break if ... / continue if ...: один условный переход.
func (h *highLevel) loopJump(word string, wordTok, ifTok *Token, toks []*Token, loc *SourceLoc) {
	loop := h.innerLoop()
	if loop == nil {
		hlError(word+" вне цикла", wordTok)
	}
	target := loop.cont
	if word == "break" {
		target = loop.end
	}
	h.condJump(target, ifTok, toks, loc, true)
}

func (h *highLevel) push(kind string, loc *SourceLoc) *block {
	blk := &block{kind: kind, loc: loc, step: bi(1)}
	h.blocks = append(h.blocks, blk)
	return blk
}

func (h *highLevel) pop() *block {
	blk := h.blocks[len(h.blocks)-1]
	h.blocks = h.blocks[:len(h.blocks)-1]
	return blk
}

func (h *highLevel) top(kind, word string) *block {
	if len(h.blocks) == 0 || h.blocks[len(h.blocks)-1].kind != kind {
		fail(fmt.Sprintf("%s без %s", word, kind))
	}
	return h.blocks[len(h.blocks)-1]
}

// closeFile — конец файла: все блоки, открытые в нём, должны быть закрыты.
func (h *highLevel) closeFile(depth int, errors *[]*Error) {
	for len(h.blocks) > depth {
		blk := h.pop()
		closer := "end"
		if blk.kind == "repeat" {
			closer = "until"
		}
		*errors = append(*errors, &Error{Message: fmt.Sprintf("%s без %s", blk.kind, closer), Loc: blk.loc, Col: NoCol})
	}
}

func (h *highLevel) forLoop(wordTok *Token, toks []*Token, loc *SourceLoc) {
	signed := false
	if len(toks) > 0 && toks[0].IsID("signed") {
		signed = true
		toks = toks[1:]
	}
	groups := splitSeps(toks)
	blk := h.push("for", loc)
	if (len(groups) != 3 && len(groups) != 4) || !allNonEmpty(groups) {
		hlError("формат: for счётчик - начало - конец [- шаг]", wordTok)
	}
	v := h.p.parseOperand(groups[0], loc)
	_, isMemOp := v.(*MemOperand)
	r, isReg := v.(*RegOperand)
	if !(isMemOp || (isReg && r.Reg.Kind == "gpr")) {
		hlError("счётчик for — регистр или переменная в памяти", groups[0][0])
	}
	start := h.p.parseOperand(groups[1], loc)
	end := h.p.parseOperand(groups[2], loc)
	step := bi(1)
	if len(groups) == 4 {
		step = foldConst(h.p.parseExprTokens(groups[3], loc, ""))
		if step == nil || step.Sign() == 0 {
			hlError("шаг for — ненулевое число (например, 2 или -1)", groups[3][0])
		}
	}
	blk.variable, blk.step = v, step
	blk.top = h.newLabel("for")
	blk.cont = h.newLabel("next")
	blk.end = h.newLabel("fend")
	h.instr("mov", []Operand{v, start}, loc)
	h.label(blk.top, loc)
	rel := "le"
	if step.Sign() > 0 {
		rel = "ge"
	}
	c := h.compare(v, end, rel, signed, loc)
	h.jump("j"+c, blk.end, loc)
}

// ------------------------------------------------ условия

// cond — дерево условия.
type cond struct {
	kind  string // and / or / flag / truth / cmp
	items []*cond
	flag  string
	neg   bool
	op    Operand // truth
	left  Operand // cmp
	right Operand
	rel   string
}

// condition разбирает условие → (signed, дерево).
func (h *highLevel) condition(wordTok *Token, toks []*Token) (bool, *cond) {
	toks = minusSeps(toks)
	signed := false
	if len(toks) > 0 && toks[0].IsID("signed") {
		signed = true
		toks = toks[1:]
	}
	if len(toks) == 0 {
		hlError(fmt.Sprintf("после %s ожидается условие, например: %s al = 10", wordTok.Value, wordTok.Value), wordTok)
	}
	var ors []*cond
	for _, part := range splitWord(toks, "or", wordTok) {
		var ands []*cond
		for _, a := range splitWord(part, "and", wordTok) {
			ands = append(ands, h.simple(a))
		}
		if len(ands) == 1 {
			ors = append(ors, ands[0])
		} else {
			ors = append(ors, &cond{kind: "and", items: ands})
		}
	}
	if len(ors) == 1 {
		return signed, ors[0]
	}
	return signed, &cond{kind: "or", items: ors}
}

func splitWord(toks []*Token, word string, near *Token) [][]*Token {
	parts := [][]*Token{nil}
	depth := 0
	for _, t := range toks {
		if t.IsOp("(", "[") {
			depth++
		} else if t.IsOp(")", "]") {
			depth--
		}
		if depth == 0 && t.IsID(word) {
			parts = append(parts, nil)
			continue
		}
		parts[len(parts)-1] = append(parts[len(parts)-1], t)
	}
	for _, p := range parts {
		if len(p) == 0 {
			hlError("пустое условие рядом с "+word, near)
		}
	}
	return parts
}

func (h *highLevel) simple(toks []*Token) *cond {
	neg := false
	if toks[0].IsID("not") {
		neg = true
		toks = toks[1:]
		if len(toks) == 0 {
			hlError("после not ожидается условие", nil)
		}
	}
	if len(toks) == 1 && toks[0].Kind == ID {
		if f, ok := condFlags[lower(toks[0].Value)]; ok {
			return &cond{kind: "flag", flag: f, neg: neg}
		}
	}
	depth, idx := 0, -1
	for i, t := range toks {
		if t.IsOp("(", "[") {
			depth++
		} else if t.IsOp(")", "]") {
			depth--
		} else if _, ok := relations[t.Value]; depth == 0 && t.Kind == OP && ok {
			idx = i
			break
		}
	}
	loc := h.p.currentLoc
	if idx < 0 {
		return &cond{kind: "truth", op: h.p.parseOperand(toks, loc), neg: neg}
	}
	if neg {
		hlError("not перед сравнением не нужен — используйте != или обратное сравнение", toks[0])
	}
	if idx == 0 || idx == len(toks)-1 {
		hlError("сравнению нужны две стороны: например al = 10", toks[idx])
	}
	left := h.p.parseOperand(toks[:idx], loc)
	right := h.p.parseOperand(toks[idx+1:], loc)
	return &cond{kind: "cmp", left: left, right: right, rel: relations[toks[idx].Value]}
}

// compare сравнивает и возвращает код условия, при котором сравнение истинно.
func (h *highLevel) compare(left, right Operand, rel string, signed bool, loc *SourceLoc) string {
	_, leftImm := left.(*ImmOperand)
	_, rightImm := right.(*ImmOperand)
	if leftImm && !rightImm {
		left, right, rel = right, left, mirror[rel]
	}
	if _, ok := left.(*ImmOperand); ok {
		fail("сравнивать два числа бессмысленно — слева должен быть регистр или память")
	}
	if isStringOperand(right) && isByteOperand(left) {
		fail("строка в двойных кавычках — это адрес строки; " +
			"символ записывается в одинарных кавычках: 'a'")
	}
	_, lReg := left.(*RegOperand)
	_, lMem := left.(*MemOperand)
	_, rFar := right.(*FarOperand)
	_, rA20 := right.(*A20Operand)
	if !(lReg || lMem) || rFar || rA20 {
		fail("в условии можно сравнивать регистры, память и числа")
	}
	zero := false
	if ri, ok := right.(*ImmOperand); ok {
		if v := foldConst(ri.Expr); v != nil && v.Sign() == 0 {
			zero = true
		}
	}
	if r, ok := left.(*RegOperand); ok && r.Reg.Kind == "gpr" && zero {
		h.instr("test", []Operand{left, left}, loc) // короче, чем cmp r - 0
	} else {
		h.instr("cmp", []Operand{left, right}, loc)
	}
	if signed {
		return signedCC[rel]
	}
	return unsignedCC[rel]
}

func (h *highLevel) condCode(c *cond, signed bool, loc *SourceLoc) string {
	switch c.kind {
	case "flag":
		if c.neg {
			return invert[c.flag]
		}
		return c.flag
	case "truth":
		switch op := c.op.(type) {
		case *RegOperand:
			if op.Reg.Kind != "gpr" {
				fail("условием может быть сравнение, флаг (zero, carry, sign, overflow, " +
					"parity), регистр или память")
			}
			h.instr("test", []Operand{op, op}, loc)
		case *MemOperand:
			h.instr("cmp", []Operand{op, imm(numNode(0))}, loc)
		default:
			fail("условием может быть сравнение, флаг (zero, carry, sign, overflow, " +
				"parity), регистр или память")
		}
		if c.neg {
			return "z"
		}
		return "nz"
	}
	return h.compare(c.left, c.right, c.rel, signed, loc)
}

// jumpIf — перейти на target, если условие c равно value; иначе идти дальше.
func (h *highLevel) jumpIf(c *cond, value bool, target string, signed bool, loc *SourceLoc) {
	if c.kind == "and" || c.kind == "or" {
		short := (c.kind == "or") == value // одно подусловие решает всё
		if short {
			for _, sub := range c.items {
				h.jumpIf(sub, value, target, signed, loc)
			}
		} else {
			skip := h.newLabel("skip")
			for _, sub := range c.items[:len(c.items)-1] {
				h.jumpIf(sub, !value, skip, signed, loc)
			}
			h.jumpIf(c.items[len(c.items)-1], value, target, signed, loc)
			h.label(skip, loc)
		}
		return
	}
	if c.kind == "truth" {
		if o, ok := c.op.(*ImmOperand); ok {
			v := foldConst(o.Expr)
			if v == nil {
				fail("условие-число должно быть числом (например: while 1)")
			}
			if ((v.Sign() != 0) != c.neg) == value {
				h.jump("jmp", target, loc)
			}
			return
		}
	}
	code := h.condCode(c, signed, loc)
	if !value {
		code = invert[code]
	}
	h.jump("j"+code, target, loc)
}

// ------------------------------------------------ let

func (h *highLevel) let(wordTok *Token, toks []*Token, loc *SourceLoc) {
	signed := false
	if len(toks) > 0 && toks[0].IsID("signed") {
		signed = true
		toks = toks[1:]
	}
	groups := splitSeps(toks)
	const format = "формат: let приёмник - \"выражение\""
	if len(groups) != 2 || !allNonEmpty(groups) {
		hlError(format, wordTok)
	}
	a, b := groups[0], groups[1]
	var destToks []*Token
	var text *Token
	switch {
	case len(b) == 1 && b[0].Kind == STR:
		destToks, text = a, b[0]
	case len(a) == 1 && a[0].Kind == STR:
		destToks, text = b, a[0]
	default:
		hlError(format+" (выражение — в двойных кавычках)", wordTok)
	}
	if text.Quote != '"' {
		hlError("выражение let пишется в двойных кавычках", text)
	}
	dest := h.p.parseOperand(destToks, loc)
	inner := decodeReplace(text.Bytes)
	itoks := Tokenize(inner, loc, text.Col+1, false)
	if len(itoks) == 0 {
		hlError("пустое выражение let", text)
	}
	expr := h.p.parseExprTokens(itoks, loc, "let")
	lc := &letCompiler{signed: signed, bits: h.p.bits, busy: map[int]bool{}}
	for _, ins := range lc.compile(expr, dest, text) {
		h.instr(ins.mn, ins.ops, loc)
	}
}
