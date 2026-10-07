package gasem

// Узлы синтаксического дерева Gasem: выражения, операнды и операторы.

import "math/big"

// ---------------------------------------------------------------- выражения

// Expr — выражение. Col — столбец в строке или NoCol.
type Expr interface {
	Col() int
}

type Num struct {
	Value *big.Int
	C     int
}

type Str struct {
	Value []byte
	C     int
}

// Sym — имя метки или константы (значение — адрес/число).
type Sym struct {
	Name string
	C    int
}

// Here — $, адрес текущей строки.
type Here struct{ C int }

// Start — $$, адрес начала программы (значение og).
type Start struct{ C int }

// RegNode — регистр внутри выражения (допустим только в адресе [..]).
// Local — имя локальной переменной proc, если регистр взялся из неё ([ebp-4]).
type RegNode struct {
	Reg   *Reg
	C     int
	Local string
}

// Var — [имя] внутри выражения do: значение переменной целиком.
type Var struct {
	Name string
	C    int
}

// MemNode — чтение памяти [..] внутри выражения let (во время выполнения).
type MemNode struct {
	Mem *MemOperand
	C   int
}

type Unary struct {
	Op string
	X  Expr
	C  int
}

type Binary struct {
	Op   string
	A, B Expr
	C    int
}

func (n *Num) Col() int     { return n.C }
func (n *Str) Col() int     { return n.C }
func (n *Sym) Col() int     { return n.C }
func (n *Here) Col() int    { return n.C }
func (n *Start) Col() int   { return n.C }
func (n *RegNode) Col() int { return n.C }
func (n *Var) Col() int     { return n.C }
func (n *MemNode) Col() int { return n.C }
func (n *Unary) Col() int   { return n.C }
func (n *Binary) Col() int  { return n.C }

func numNode(v int64) *Num { return &Num{Value: big.NewInt(v), C: NoCol} }

func hasReg(node Expr) bool { return firstReg(node) != nil }

// isConst — можно ли вычислить выражение при компиляции (нет регистров и памяти).
func isConst(node Expr) bool {
	switch n := node.(type) {
	case *RegNode, *MemNode, *Var:
		return false
	case *Unary:
		return isConst(n.X)
	case *Binary:
		return isConst(n.A) && isConst(n.B)
	}
	return true
}

func firstReg(node Expr) *RegNode {
	switch n := node.(type) {
	case *RegNode:
		return n
	case *Unary:
		return firstReg(n.X)
	case *Binary:
		if r := firstReg(n.A); r != nil {
			return r
		}
		return firstReg(n.B)
	}
	return nil
}

// foldConst — значение выражения из одних чисел (без меток); nil — если нельзя.
func foldConst(node Expr) *big.Int {
	switch n := node.(type) {
	case *Num:
		return n.Value
	case *Unary:
		v := foldConst(n.X)
		if v == nil {
			return nil
		}
		switch n.Op {
		case "-":
			return neg(v)
		case "~":
			return bnot(v)
		}
		return v
	case *Binary:
		a, b := foldConst(n.A), foldConst(n.B)
		if a == nil || b == nil {
			return nil
		}
		switch n.Op {
		case "+":
			return add(a, b)
		case "-":
			return sub(a, b)
		case "*":
			return mul(a, b)
		}
	}
	return nil
}

// ---------------------------------------------------------------- операнды

// Operand — операнд команды в дереве разбора.
type Operand interface{ isOperand() }

type RegOperand struct{ Reg *Reg }

type MemOperand struct {
	Size  int // 8/16/32/64 или 0 (не указан)
	Seg   *Reg
	Base  *Reg
	Index *Reg
	Scale int
	Disp  Expr   // nil — нет смещения
	Jump  string // "far" для jmp far [..]
	Mode  string // "rel" / "abs" — способ адресации в режиме b 64
}

type ImmOperand struct {
	Expr       Expr
	Size       int    // явный размер (byte/word/dword)
	Jump       string // short / near / far
	FromString bool   // "строка" в команде — это её адрес
}

// FarOperand — сегмент:смещение для дальних jmp/call.
type FarOperand struct {
	Seg, Off Expr
	Size     int
}

// A20Operand — абстракция порта: линия A20.
type A20Operand struct{}

func (*RegOperand) isOperand() {}
func (*MemOperand) isOperand() {}
func (*ImmOperand) isOperand() {}
func (*FarOperand) isOperand() {}
func (*A20Operand) isOperand() {}

func imm(e Expr) *ImmOperand { return &ImmOperand{Expr: e} }

// ---------------------------------------------------------------- операторы

type Stmt interface{ Location() *SourceLoc }

type LabelStmt struct {
	Name string
	Loc  *SourceLoc
	Col  int
}

type ConstStmt struct {
	Name string
	Expr Expr
	Loc  *SourceLoc
	Col  int
}

type OrgStmt struct {
	Expr Expr
	Loc  *SourceLoc
}

type BitsStmt struct {
	Expr Expr
	Loc  *SourceLoc
}

type AlignStmt struct {
	Expr Expr
	Fill Expr // nil — заполнять nop
	Loc  *SourceLoc
}

// DataStmt — b: / b-N / b/ N (и то же для w, d, q, s).
type DataStmt struct {
	Unit  int    // размер элемента в байтах: 1, 2, 4, 8
	Kind  string // "list" (b:), "fill" (b-N), "fixed" (b/ N)
	Count Expr   // для fill/fixed
	Items []Expr
	Loc   *SourceLoc
	Name  string // b / w / d / q / s — для сообщений
}

// TimesStmt — && N <команда>: повторение.
type TimesStmt struct {
	Count Expr
	Body  Stmt
	Loc   *SourceLoc
}

type InstrStmt struct {
	Prefixes []byte
	Mnemonic string
	Operands []Operand
	Loc      *SourceLoc
	Written  string         // как команда написана в исходнике
	Flags    map[string]int // выбранные размеры кодировок (растут монотонно между проходами)
}

// DoStmt — do "выражение" - приёмник.
type DoStmt struct {
	Expr  Expr
	Dest  Operand
	Loc   *SourceLoc
	Flags map[string]int
}

type IncbinStmt struct {
	Data []byte
	Loc  *SourceLoc
}

// CallStmt — call/jmp с аргументами: call puts - "Hello". Разворачивается после
// разбора, когда известны все объявления args.
type CallStmt struct {
	Mnemonic string
	Prefixes []byte
	Target   Operand
	Args     []Operand
	Bits     int
	Loc      *SourceLoc
}

// AtStmt — at АДРЕС: код ниже работает по другому адресу, чем лежит в файле.
type AtStmt struct {
	Expr Expr
	Loc  *SourceLoc
}

// AtEndStmt — end блока at.
type AtEndStmt struct {
	Loc *SourceLoc
}

type poolEntry struct {
	Label string
	Value []byte
}

// PoolStmt — pool: место для строк из команд, встреченных выше.
type PoolStmt struct {
	Loc     *SourceLoc
	Entries []poolEntry
}

func (s *LabelStmt) Location() *SourceLoc  { return s.Loc }
func (s *ConstStmt) Location() *SourceLoc  { return s.Loc }
func (s *OrgStmt) Location() *SourceLoc    { return s.Loc }
func (s *BitsStmt) Location() *SourceLoc   { return s.Loc }
func (s *AlignStmt) Location() *SourceLoc  { return s.Loc }
func (s *DataStmt) Location() *SourceLoc   { return s.Loc }
func (s *TimesStmt) Location() *SourceLoc  { return s.Loc }
func (s *InstrStmt) Location() *SourceLoc  { return s.Loc }
func (s *DoStmt) Location() *SourceLoc     { return s.Loc }
func (s *IncbinStmt) Location() *SourceLoc { return s.Loc }
func (s *CallStmt) Location() *SourceLoc   { return s.Loc }
func (s *PoolStmt) Location() *SourceLoc   { return s.Loc }
func (s *AtStmt) Location() *SourceLoc     { return s.Loc }
func (s *AtEndStmt) Location() *SourceLoc  { return s.Loc }

func newInstr(prefixes []byte, mn string, ops []Operand, loc *SourceLoc, written string) *InstrStmt {
	if written == "" {
		written = mn
	}
	return &InstrStmt{Prefixes: prefixes, Mnemonic: mn, Operands: ops, Loc: loc, Written: written,
		Flags: map[string]int{}}
}
