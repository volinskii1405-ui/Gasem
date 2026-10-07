package gasem

// let: выражение → команды. Меняется только приёмник (и флаги):
// временные регистры сохраняются в стеке.

import "fmt"

var regNames = FamilyNames // имя регистра по размеру и семейству

var tempOrder = []int{3, 6, 7, 1, 2, 5, 0} // ebx esi edi ecx edx ebp eax
var low8Order = []int{3, 1, 2, 0}          // регистры, у которых есть младший байт

const (
	famEAX = 0
	famECX = 1
	famEDX = 2
	famESP = 4
)

// family — семейство регистра: al/ah/ax/eax/rax → 0, cl/ch/cx/ecx/rcx → 1, …, r15 → 15.
func family(r *Reg) int {
	if r.High8 {
		return r.Num & 3
	}
	return r.Num
}

// walk обходит выражение: сначала узел, потом его части.
func walk(node Expr, f func(Expr)) {
	f(node)
	switch n := node.(type) {
	case *Unary:
		walk(n.X, f)
	case *Binary:
		walk(n.A, f)
		walk(n.B, f)
	}
}

func memRegs(m *MemOperand) []*Reg {
	var out []*Reg
	for _, r := range []*Reg{m.Base, m.Index} {
		if r != nil {
			out = append(out, r)
		}
	}
	return out
}

// refs — семейства регистров (0=eax ... 7=edi), которые читает выражение.
func refs(node Expr) map[int]bool {
	out := map[int]bool{}
	walk(node, func(n Expr) {
		switch x := n.(type) {
		case *RegNode:
			out[family(x.Reg)] = true
		case *MemNode:
			for _, r := range memRegs(x.Mem) {
				out[family(r)] = true
			}
		}
	})
	return out
}

// log2 — показатель степени двойки или -1.
func log2(node Expr) int {
	v := foldConst(node)
	if v != nil && v.Sign() > 0 && band(v, sub(v, bi(1))).Sign() == 0 {
		return v.BitLen() - 1
	}
	return -1
}

type letInstr struct {
	mn  string
	ops []Operand
}

type letCompiler struct {
	signed   bool
	bits     int
	width    int
	out      []letInstr
	busy     map[int]bool
	tok      *Token
	varShift bool
}

func (c *letCompiler) emit(mn string, ops ...Operand) {
	c.out = append(c.out, letInstr{mn, ops})
}

func (c *letCompiler) reg(fam int) *RegOperand { return c.regSized(fam, c.width) }

func (c *letCompiler) regSized(fam, size int) *RegOperand {
	return &RegOperand{Registers[regNames[size][fam]]}
}

func (c *letCompiler) error(msg string) { hlError(msg, c.tok) }

func (c *letCompiler) compile(expr Expr, dest Operand, tok *Token) []letInstr {
	c.tok = tok
	// приёмник и ширина вычислений
	destSize := 0
	switch d := dest.(type) {
	case *RegOperand:
		if d.Reg.Kind != "gpr" {
			c.error("приёмник let — регистр общего назначения или память")
		}
		if family(d.Reg) == famESP {
			c.error("sp/esp нельзя использовать в let")
		}
		c.width = c.bits
		if d.Reg.Size == 16 || d.Reg.Size == 32 || d.Reg.Size == 64 {
			c.width = d.Reg.Size
		}
		destSize = d.Reg.Size
	case *MemOperand:
		if d.Size != 8 && d.Size != 16 && d.Size != 32 && d.Size != 64 {
			c.error("укажите размер приёмника: let dword [x] - \"...\"")
		}
		c.width = c.bits
		if d.Size == 16 || d.Size == 32 || d.Size == 64 {
			c.width = d.Size
		}
		destSize = d.Size
	default:
		c.error("приёмник let — регистр общего назначения или память")
	}

	// какие регистры читает выражение
	refsSet, count := map[int]bool{}, map[int]int{}
	walk(expr, func(node Expr) {
		var regs []*Reg
		switch n := node.(type) {
		case *RegNode:
			if n.Reg.Kind != "gpr" {
				c.error(fmt.Sprintf("в let можно использовать только регистры общего назначения (%s)", n.Reg.Name))
			}
			regs = []*Reg{n.Reg}
		case *MemNode:
			regs = memRegs(n.Mem)
		case *Str:
			if len(n.Value) > 4 {
				c.error("в let нет строк (только числа и символы вроде 'A')")
			}
		case *Var:
			c.error("[имя] в let — это чтение памяти; запишите его без do-синтаксиса")
		}
		for _, r := range regs {
			if family(r) == famESP {
				c.error("sp/esp нельзя использовать в let: временные значения хранятся в стеке")
			}
			refsSet[family(r)] = true
			count[family(r)]++
		}
		if b, ok := node.(*Binary); ok && (b.Op == "<<" || b.Op == ">>") && !isConst(b.B) {
			c.varShift = true
		}
	})
	var destRegs []*Reg
	if m, ok := dest.(*MemOperand); ok {
		destRegs = memRegs(m)
	}
	for _, r := range destRegs {
		if family(r) == famESP {
			c.error("sp/esp нельзя использовать в let")
		}
	}

	// простые случаи: число или одно значение
	if isConst(expr) {
		c.emit("mov", dest, imm(expr))
		return c.out
	}

	destFam := -1
	destFull := false
	if r, ok := dest.(*RegOperand); ok {
		destFam = family(r.Reg)
		destFull = r.Reg.Size == c.width
	}
	// рабочий регистр держит значение всё время вычисления, поэтому он не должен
	// встречаться в выражении и в адресе приёмника
	avoid := map[int]bool{}
	for f := range refsSet {
		avoid[f] = true
	}
	destRegFams := map[int]bool{}
	for _, r := range destRegs {
		avoid[family(r)] = true
		destRegFams[family(r)] = true
	}
	if destFam >= 0 {
		avoid[destFam] = true
	}

	leftmost := expr
	for {
		if u, ok := leftmost.(*Unary); ok {
			leftmost = u.X
		} else if b, ok := leftmost.(*Binary); ok {
			leftmost = b.A
		} else {
			break
		}
	}
	destSafe := destFam < 0 || !refsSet[destFam]
	if !destSafe && count[destFam] == 1 {
		if r, ok := leftmost.(*RegNode); ok && family(r.Reg) == destFam && r.Reg.Size == c.width {
			destSafe = true
		}
	}

	var work int
	saved := false
	if destFull && destSafe && !(c.varShift && destFam == famECX) {
		work = destFam
	} else {
		work = c.alloc(avoid)
		saved = true
		c.emit("push", c.reg(work))
	}
	c.busy[work] = true
	c.gen(expr, work)
	if m, ok := dest.(*MemOperand); ok {
		dest = &MemOperand{Size: m.Size, Seg: m.Seg, Base: m.Base, Index: m.Index, Scale: m.Scale, Disp: m.Disp}
	}
	inLow8 := false
	for _, f := range low8Order {
		inLow8 = inLow8 || f == work
	}
	if destSize == 8 && !inLow8 {
		// у esi/edi/ebp нет младшего байта — переносим через другой регистр
		// (выражение уже вычислено, так что любой регистр, кроме приёмника, свободен)
		spare := -1
		for _, f := range low8Order {
			if f != destFam && !destRegFams[f] {
				spare = f
				break
			}
		}
		c.emit("push", c.reg(spare))
		c.emit("mov", c.reg(spare), c.reg(work))
		c.emit("mov", dest, c.regSized(spare, 8))
		c.emit("pop", c.reg(spare))
	} else if !(destFull && work == destFam) {
		c.emit("mov", dest, c.regSized(work, destSize))
	}
	if saved {
		c.emit("pop", c.reg(work))
	}
	return c.out
}

func (c *letCompiler) alloc(avoid map[int]bool) int {
	for _, fam := range tempOrder {
		if c.busy[fam] || avoid[fam] {
			continue
		}
		if c.varShift && fam == famECX {
			continue
		}
		return fam
	}
	c.error("выражение let слишком сложное — не хватает свободных регистров; " +
		"разбейте его на несколько let")
	return -1
}

// temp вычисляет node во временный регистр (сохранив его в стеке).
//
// Регистр восстанавливается сразу после использования, поэтому ему
// достаточно не встречаться внутри самого node.
func (c *letCompiler) temp(node Expr, extra ...int) int {
	avoid := refs(node)
	for _, f := range extra {
		avoid[f] = true
	}
	fam := c.alloc(avoid)
	c.emit("push", c.reg(fam))
	c.busy[fam] = true
	c.gen(node, fam)
	return fam
}

func (c *letCompiler) release(fam int) {
	c.emit("pop", c.reg(fam))
	delete(c.busy, fam)
}

// simple — операнд, который можно подставить в команду как есть (nil — нельзя).
func (c *letCompiler) simple(node Expr) Operand {
	if isConst(node) {
		return imm(node)
	}
	if r, ok := node.(*RegNode); ok && r.Reg.Size == c.width {
		return &RegOperand{r.Reg}
	}
	if m, ok := node.(*MemNode); ok && (m.Mem.Size == 0 || m.Mem.Size == c.width) {
		x := m.Mem
		return &MemOperand{Size: c.width, Seg: x.Seg, Base: x.Base, Index: x.Index, Scale: x.Scale, Disp: x.Disp}
	}
	return nil
}

func (c *letCompiler) extend() string {
	if c.signed {
		return "movsx"
	}
	return "movzx"
}

// extendTo — расширить src (size бит) до ширины вычисления.
func (c *letCompiler) extendTo(fam int, src Operand, size int) {
	if size == 32 { // 32 → 64: movsxd или mov в 32-битную часть
		if c.signed {
			c.emit("movsxd", c.reg(fam), src)
		} else {
			c.emit("mov", c.regSized(fam, 32), src)
		}
		return
	}
	c.emit(c.extend(), c.reg(fam), src)
}

func (c *letCompiler) gen(node Expr, fam int) {
	R := c.reg(fam)
	if isConst(node) {
		if v := foldConst(node); v != nil && v.Sign() == 0 {
			c.emit("xor", R, R)
		} else {
			c.emit("mov", R, imm(node))
		}
		return
	}
	switch n := node.(type) {
	case *RegNode:
		r := n.Reg
		switch {
		case r.Size == c.width:
			if family(r) != fam {
				c.emit("mov", R, &RegOperand{r})
			}
		case r.Size < c.width:
			c.extendTo(fam, &RegOperand{r}, r.Size)
		default:
			c.emit("mov", R, c.reg(family(r)))
		}
	case *MemNode:
		m := n.Mem
		size := m.Size
		if size == 0 {
			size = c.width
		}
		if size > c.width {
			c.error(fmt.Sprintf("память размером %d бит не помещается в %d-битное вычисление", size, c.width))
		}
		mem := &MemOperand{Size: size, Seg: m.Seg, Base: m.Base, Index: m.Index, Scale: m.Scale, Disp: m.Disp}
		if size == c.width {
			c.emit("mov", R, mem)
		} else {
			c.extendTo(fam, mem, size)
		}
	case *Unary:
		c.gen(n.X, fam)
		switch n.Op {
		case "-":
			c.emit("neg", R)
		case "~":
			c.emit("not", R)
		}
	case *Binary:
		a, b, op := n.A, n.B, n.Op
		// сложную часть считаем первой — так нужно меньше временных регистров.
		// Нельзя, если слева сам рабочий регистр (let eax - "eax * (...)"):
		// его значение должно быть прочитано до того, как он изменится.
		own := false
		if r, ok := a.(*RegNode); ok && family(r.Reg) == fam {
			own = true
		}
		if c.simple(b) == nil && c.simple(a) != nil && !own {
			switch op {
			case "+", "*", "&", "|", "^":
				a, b = b, a
			case "-": // a - b = -b + a
				c.gen(b, fam)
				c.emit("neg", R)
				c.emit("add", R, c.simple(a))
				return
			}
		}
		c.gen(a, fam)
		c.apply(op, fam, b)
	default:
		c.error("в let недопустимо такое выражение")
	}
}

var letALU = map[string]string{"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}

func (c *letCompiler) apply(op string, fam int, b Expr) {
	R := c.reg(fam)
	if mn, ok := letALU[op]; ok {
		if src := c.simple(b); src != nil {
			c.emit(mn, R, src)
		} else {
			t := c.temp(b)
			c.emit(mn, R, c.reg(t))
			c.release(t)
		}
		return
	}
	switch op {
	case "*":
		if isConst(b) {
			if k := log2(b); k >= 0 {
				if k > 0 {
					c.emit("shl", R, imm(numNode(int64(k))))
				}
			} else {
				c.emit("imul", R, R, imm(b))
			}
			return
		}
		if src := c.simple(b); src != nil {
			c.emit("imul", R, src)
		} else {
			t := c.temp(b)
			c.emit("imul", R, c.reg(t))
			c.release(t)
		}
	case "<<", ">>":
		mn := "shl"
		if op == ">>" {
			mn = "shr"
			if c.signed {
				mn = "sar"
			}
		}
		if isConst(b) {
			c.emit(mn, R, imm(b))
			return
		}
		t := c.temp(b, famECX)
		c.emit("push", c.reg(famECX))
		c.emit("mov", c.reg(famECX), c.reg(t))
		c.emit(mn, R, &RegOperand{Registers["cl"]})
		c.emit("pop", c.reg(famECX))
		c.release(t)
	case "/", "%":
		k := -1
		if isConst(b) {
			k = log2(b)
		}
		if k >= 0 && !c.signed {
			if op == "/" {
				if k > 0 {
					c.emit("shr", R, imm(numNode(int64(k))))
				}
			} else {
				c.emit("and", R, imm(&Num{sub(pow2(k), bi(1)), NoCol}))
			}
			return
		}
		t := c.temp(b, famEAX, famEDX)
		if fam != famEAX {
			c.emit("push", c.reg(famEAX))
		}
		if fam != famEDX {
			c.emit("push", c.reg(famEDX))
		}
		if fam != famEAX {
			c.emit("mov", c.reg(famEAX), R)
		}
		if c.signed {
			c.emit(map[int]string{16: "cwd", 32: "cdq", 64: "cqo"}[c.width])
		} else {
			c.emit("xor", c.reg(famEDX), c.reg(famEDX))
		}
		if c.signed {
			c.emit("idiv", c.reg(t))
		} else {
			c.emit("div", c.reg(t))
		}
		res := famEAX
		if op == "%" {
			res = famEDX
		}
		if fam != res {
			c.emit("mov", R, c.reg(res))
		}
		if fam != famEDX {
			c.emit("pop", c.reg(famEDX))
		}
		if fam != famEAX {
			c.emit("pop", c.reg(famEAX))
		}
		c.release(t)
	default:
		c.error(fmt.Sprintf("операция %s в let не поддерживается", op))
	}
}
