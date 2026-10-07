package gasem

// Вычисление выражений Gasem.
//
// Значение выражения — целое число или строка (байты). В обычных выражениях
// строка превращается в число (байты в порядке little-endian, как 'A' = 0x41).
// В выражениях do оператор + со строкой означает конкатенацию:
// "Hello" + 1 = "Hello1".

import (
	"fmt"
	"math/big"
)

// value — число или строка.
type value struct {
	i     *big.Int
	s     []byte
	isStr bool
}

func intValue(v *big.Int) value { return value{i: v} }
func strValue(b []byte) value   { return value{s: b, isStr: true} }

func toInt(v value, col int) *big.Int {
	if v.isStr {
		if len(v.s) > 8 {
			failCol("строка слишком длинная, чтобы использовать её как число", col)
		}
		return fromLittle(v.s)
	}
	return v.i
}

func toText(v value) []byte {
	if v.isStr {
		return v.s
	}
	return []byte(v.i.String())
}

type evaluator struct {
	asm   *assembler
	here  *big.Int
	mode  string // "" или "do"
	known bool   // все ли символы уже определены
}

func newEvaluator(asm *assembler, here *big.Int, mode string) *evaluator {
	return &evaluator{asm: asm, here: here, mode: mode, known: true}
}

func (ev *evaluator) error(msg string, node Expr) {
	col := NoCol
	if node != nil {
		col = node.Col()
	}
	failCol(msg, col)
}

func (ev *evaluator) eval(node Expr) value {
	switch n := node.(type) {
	case *Num:
		return intValue(n.Value)
	case *Str:
		return strValue(n.Value)
	case *Sym:
		v := ev.asm.lookup(n.Name)
		if v == nil {
			if ev.asm.final {
				hint := suggestName(n.Name, ev.asm.knownNames())
				ev.error(fmt.Sprintf("неизвестное имя '%s'%s", n.Name, hint), node)
			}
			ev.known = false
			return intValue(bi(0))
		}
		return intValue(v)
	case *Here:
		return intValue(ev.here)
	case *Start:
		return intValue(ev.asm.org)
	case *RegNode:
		if ev.mode == "do" {
			ev.error(fmt.Sprintf("регистр %s нельзя использовать в do: "+
				"выражение do вычисляется при компиляции", n.Reg.Name), node)
		}
		ev.error(fmt.Sprintf("регистр %s нельзя использовать в выражении", n.Reg.Name), node)
	case *Var:
		if ev.mode != "do" {
			ev.error("[имя] как значение переменной допускается только в do", node)
		}
		return ev.asm.varValue(n.Name, ev, n)
	case *Unary:
		v := ev.eval(n.X)
		if v.isStr && ev.mode == "do" {
			ev.error(fmt.Sprintf("операция '%s' не применима к строке", n.Op), node)
		}
		x := toInt(v, n.C)
		switch n.Op {
		case "-":
			return intValue(neg(x))
		case "~":
			return intValue(bnot(x))
		}
		return intValue(x)
	case *Binary:
		return ev.binary(n)
	}
	panic(fmt.Sprintf("неизвестный узел %T", node))
}

func (ev *evaluator) binary(node *Binary) value {
	av := ev.eval(node.A)
	bv := ev.eval(node.B)
	op := node.Op
	if av.isStr || bv.isStr {
		if ev.mode == "do" {
			if op == "+" {
				return strValue(append(append([]byte{}, toText(av)...), toText(bv)...))
			}
			ev.error(fmt.Sprintf("операция '%s' не применима к строкам (для строк есть только +)", op), node)
		}
	}
	a, b := toInt(av, node.C), toInt(bv, node.C)
	switch op {
	case "+":
		return intValue(add(a, b))
	case "-":
		return intValue(sub(a, b))
	case "*":
		return intValue(mul(a, b))
	case "/", "%":
		if b.Sign() == 0 {
			if ev.asm.final {
				ev.error("деление на ноль", node)
			}
			return intValue(bi(0))
		}
		if op == "/" {
			return intValue(truncDiv(a, b))
		}
		return intValue(truncMod(a, b))
	case "<<", ">>":
		if b.Sign() < 0 || cmpInt(b, 4096) > 0 {
			if ev.asm.final {
				ev.error(fmt.Sprintf("недопустимая величина сдвига %s", b), node)
			}
			return intValue(bi(0))
		}
		if op == "<<" {
			return intValue(new(big.Int).Lsh(a, uint(b.Int64())))
		}
		return intValue(new(big.Int).Rsh(a, uint(b.Int64())))
	case "&":
		return intValue(band(a, b))
	case "|":
		return intValue(bor(a, b))
	case "^":
		return intValue(bxor(a, b))
	}
	panic(op)
}
