package gasem

// Кодирование команд x86 в машинный код (режимы 16 и 32 бит).

import (
	"fmt"
	"math/big"
	"sort"
	"strings"
)

// ---------------------------------------------------------------- регистры

type Reg struct {
	Name string
	Size int
	Num  int
	Kind string // gpr / seg / cr / dr
}

// Registers — все регистры по имени.
var Registers = func() map[string]*Reg {
	m := map[string]*Reg{}
	add := func(names []string, size int, kind string, nums []int) {
		for i, name := range names {
			num := i
			if nums != nil {
				num = nums[i]
			}
			m[name] = &Reg{name, size, num, kind}
		}
	}
	add(strings.Fields("al cl dl bl ah ch dh bh"), 8, "gpr", nil)
	add(strings.Fields("ax cx dx bx sp bp si di"), 16, "gpr", nil)
	add(strings.Fields("eax ecx edx ebx esp ebp esi edi"), 32, "gpr", nil)
	add(strings.Fields("es cs ss ds fs gs"), 16, "seg", nil)
	add([]string{"cr0", "cr2", "cr3", "cr4"}, 32, "cr", []int{0, 2, 3, 4})
	add(strings.Fields("dr0 dr1 dr2 dr3 dr4 dr5 dr6 dr7"), 32, "dr", nil)
	return m
}()

var segments = []string{"es", "cs", "ss", "ds", "fs", "gs"}
var segPrefix = map[string]byte{"es": 0x26, "cs": 0x2E, "ss": 0x36, "ds": 0x3E, "fs": 0x64, "gs": 0x65}
var prefixBytes = map[string]byte{"lock": 0xF0, "rep": 0xF3, "repe": 0xF3, "repz": 0xF3, "repne": 0xF2, "repnz": 0xF2}

// ---------------------------------------------------------------- операнды кодировщика

type Mem struct {
	Size             int
	Seg, Base, Index *Reg
	Scale            int
	Disp             *big.Int
	Known, HasDisp   bool
	Jump             string
}

// direct — адрес без регистров: [0x1234], [msg].
func (m *Mem) direct() bool { return m.Base == nil && m.Index == nil }

type Imm struct {
	Value *big.Int
	Known bool
	Size  int
	Jump  string
}

type Far struct {
	Seg, Off *big.Int
	Known    bool
	Size     int
}

type a20Op struct{}

var a20 = &a20Op{}

// ---------------------------------------------------------------- таблицы

var cc = map[string]int{
	"o": 0, "no": 1, "b": 2, "c": 2, "nae": 2, "nb": 3, "nc": 3, "ae": 3,
	"e": 4, "z": 4, "ne": 5, "nz": 5, "be": 6, "na": 6, "nbe": 7, "a": 7,
	"s": 8, "ns": 9, "p": 10, "pe": 10, "np": 11, "po": 11,
	"l": 12, "nge": 12, "nl": 13, "ge": 13, "le": 14, "ng": 14, "nle": 15, "g": 15,
}

// Команды без операндов.
var simpleOps = map[string][]byte{
	"nop": {0x90}, "hlt": {0xf4}, "cli": {0xfa}, "sti": {0xfb}, "cld": {0xfc},
	"std": {0xfd}, "clc": {0xf8}, "stc": {0xf9}, "cmc": {0xf5}, "lahf": {0x9f},
	"sahf": {0x9e}, "int3": {0xcc}, "into": {0xce}, "leave": {0xc9},
	"xlatb": {0xd7}, "wait": {0x9b}, "daa": {0x27}, "das": {0x2f}, "aaa": {0x37},
	"aas": {0x3f}, "cpuid": {0x0f, 0xa2}, "rdtsc": {0x0f, 0x31}, "rdmsr": {0x0f, 0x32},
	"wrmsr": {0x0f, 0x30}, "wbinvd": {0x0f, 0x09}, "invd": {0x0f, 0x08}, "clts": {0x0f, 0x06},
	"ud2": {0x0f, 0x0b}, "pause": {0xf3, 0x90},
}

type sizedOp struct {
	opcode byte
	size   int
}

// Команды без операндов, у которых важен размер операнда (префикс 0x66).
var sizedOps = map[string]sizedOp{
	"pusha": {0x60, 0}, "popa": {0x61, 0}, "pushf": {0x9C, 0}, "popf": {0x9D, 0},
	"iret":   {0xCF, 0},
	"pushad": {0x60, 32}, "popad": {0x61, 32}, "pushfd": {0x9C, 32}, "popfd": {0x9D, 32},
	"iretd": {0xCF, 32},
	"cbw":   {0x98, 16}, "cwde": {0x98, 32}, "cwd": {0x99, 16}, "cdq": {0x99, 32},
	"movsb": {0xA4, 8}, "movsw": {0xA5, 16}, "movsd": {0xA5, 32},
	"cmpsb": {0xA6, 8}, "cmpsw": {0xA7, 16}, "cmpsd": {0xA7, 32},
	"stosb": {0xAA, 8}, "stosw": {0xAB, 16}, "stosd": {0xAB, 32},
	"lodsb": {0xAC, 8}, "lodsw": {0xAD, 16}, "lodsd": {0xAD, 32},
	"scasb": {0xAE, 8}, "scasw": {0xAF, 16}, "scasd": {0xAF, 32},
	"insb": {0x6C, 8}, "insw": {0x6D, 16}, "insd": {0x6D, 32},
	"outsb": {0x6E, 8}, "outsw": {0x6F, 16}, "outsd": {0x6F, 32},
}

var aluOps = map[string]int{"add": 0, "or": 1, "adc": 2, "sbb": 3, "and": 4, "sub": 5, "xor": 6, "cmp": 7}
var shiftOps = map[string]int{"rol": 0, "ror": 1, "rcl": 2, "rcr": 3, "shl": 4, "sal": 4, "shr": 5, "sar": 7}
var group3Ops = map[string]int{"not": 2, "neg": 3, "mul": 4, "div": 6, "idiv": 7}

type btOp struct {
	opcode byte
	n      int
}

var bitTestOps = map[string]btOp{"bt": {0xA3, 4}, "bts": {0xAB, 5}, "btr": {0xB3, 6}, "btc": {0xBB, 7}}
var segLoadOps = map[string][]byte{"lds": {0xc5}, "les": {0xc4}, "lss": {0x0f, 0xb2}, "lfs": {0x0f, 0xb4}, "lgs": {0x0f, 0xb5}}

type descOp struct {
	opcode   []byte
	n        int
	allowReg bool // можно ли 16-битный регистр
}

var descriptorOps = map[string]descOp{
	"sgdt": {[]byte{0x0f, 0x01}, 0, false}, "sidt": {[]byte{0x0f, 0x01}, 1, false},
	"lgdt": {[]byte{0x0f, 0x01}, 2, false}, "lidt": {[]byte{0x0f, 0x01}, 3, false},
	"smsw": {[]byte{0x0f, 0x01}, 4, true}, "lmsw": {[]byte{0x0f, 0x01}, 6, true},
	"invlpg": {[]byte{0x0f, 0x01}, 7, false},
	"sldt":   {[]byte{0x0f, 0x00}, 0, true}, "str": {[]byte{0x0f, 0x00}, 1, true},
	"lldt": {[]byte{0x0f, 0x00}, 2, true}, "ltr": {[]byte{0x0f, 0x00}, 3, true},
}
var segPush = map[string][]byte{"es": {0x06}, "cs": {0x0e}, "ss": {0x16}, "ds": {0x1e}, "fs": {0x0f, 0xa0}, "gs": {0x0f, 0xa8}}
var segPop = map[string][]byte{"es": {0x07}, "ss": {0x17}, "ds": {0x1f}, "fs": {0x0f, 0xa1}, "gs": {0x0f, 0xa9}}

var otherOps = strings.Fields(`mov test inc dec imul push pop xchg lea movzx movsx
	jmp call ret retf loop loope loopne jcxz jecxz int in out enter bsf bsr bswap`)

// Aliases — псевдонимы Gasem и синонимы x86.
var Aliases = map[string]string{
	"nxtb": "lodsb", "nxtw": "lodsw", "nxtd": "lodsd",
	"chk":  "test",
	"retn": "ret", "loopz": "loope", "loopnz": "loopne", "xlat": "xlatb",
}

// Mnemonics — все команды x86, которые знает Gasem.
var Mnemonics = func() map[string]bool {
	m := map[string]bool{}
	for k := range simpleOps {
		m[k] = true
	}
	for k := range sizedOps {
		m[k] = true
	}
	for _, t := range []map[string]int{aluOps, shiftOps, group3Ops} {
		for k := range t {
			m[k] = true
		}
	}
	for k := range bitTestOps {
		m[k] = true
	}
	for k := range segLoadOps {
		m[k] = true
	}
	for k := range descriptorOps {
		m[k] = true
	}
	for _, k := range otherOps {
		m[k] = true
	}
	for c := range cc {
		m["j"+c] = true
		m["set"+c] = true
		m["cmov"+c] = true
	}
	return m
}()

// CCNames — условия переходов (для jf<усл>), по алфавиту.
func CCNames() []string {
	out := make([]string, 0, len(cc))
	for k := range cc {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Canonical — имя команды → каноническое имя x86 ("" — если команды нет).
//
// Gasem-псевдонимы: nxtb = lodsb, chk = test, jf<усл> = j<усл> (jfnz = jnz).
func Canonical(word string) string {
	w := lower(word)
	if a, ok := Aliases[w]; ok {
		w = a
	}
	if strings.HasPrefix(w, "jf") {
		if _, ok := cc[w[2:]]; ok {
			w = "j" + w[2:]
		}
	}
	if Mnemonics[w] {
		return w
	}
	return ""
}

// ---------------------------------------------------------------- кодировщик

var rm16 = map[string]int{
	"bx+si": 0, "bx+di": 1, "bp+si": 2, "bp+di": 3, "si": 4, "di": 5, "bp": 6, "bx": 7,
}
var scaleBits = map[int]int{1: 0, 2: 1, 4: 2, 8: 3}

func isGpr(o any) bool {
	r, ok := o.(*Reg)
	return ok && r.Kind == "gpr"
}

func isMem(o any) bool {
	_, ok := o.(*Mem)
	return ok
}

func isImm(o any) bool {
	_, ok := o.(*Imm)
	return ok
}

func isRM(o any) bool { return isGpr(o) || isMem(o) }

func isAcc(o any) bool { return isGpr(o) && o.(*Reg).Num == 0 }

// opSizeOf — размер регистра или памяти (0 — не указан).
func opSizeOf(o any) int {
	switch x := o.(type) {
	case *Reg:
		return x.Size
	case *Mem:
		return x.Size
	case *Imm:
		return x.Size
	case *Far:
		return x.Size
	}
	return 0
}

// encodeCtx — то, что кодировщику нужно знать о текущем проходе.
type encodeCtx interface {
	level(key string, need int) int
	addr() *big.Int
	bits() int
	final() bool
}

func cat(parts ...[]byte) []byte {
	var out []byte
	for _, p := range parts {
		out = append(out, p...)
	}
	if out == nil {
		out = []byte{}
	}
	return out
}

type encoder struct {
	ctx  encodeCtx
	bits int
	pre  []byte
	mn   string
}

// encode кодирует одну команду.
func encode(mnemonic string, operands []any, prefixes []byte, ctx encodeCtx) []byte {
	e := &encoder{ctx: ctx, bits: ctx.bits(), pre: prefixes, mn: mnemonic}
	return e.encode(operands)
}

// ------------------------------------------------ вспомогательное

func (e *encoder) bad() {
	fail("недопустимые операнды для " + e.mn)
}

func (e *encoder) nops(ops []any, counts ...int) {
	for _, c := range counts {
		if len(ops) == c {
			return
		}
	}
	want := make([]string, len(counts))
	for i, c := range counts {
		want[i] = fmt.Sprint(c)
	}
	fail(fmt.Sprintf("%s: неверное число операндов (%d), нужно %s", e.mn, len(ops), strings.Join(want, " или ")))
}

func (e *encoder) checkRange(value *big.Int, bits int, signed bool) {
	if !e.ctx.final() {
		return
	}
	lo := bi(0)
	if signed {
		lo = neg(pow2(bits - 1))
	}
	hi := sub(pow2(bits), bi(1))
	if value.Cmp(lo) < 0 || value.Cmp(hi) > 0 {
		fail(fmt.Sprintf("значение %s не помещается в %d бит", value, bits))
	}
}

func (e *encoder) imm(o any, size int, signed bool) []byte {
	x, ok := o.(*Imm)
	if !ok {
		e.bad()
	}
	e.checkRange(x.Value, size, signed)
	return pack(x.Value, size/8)
}

func (e *encoder) immS8(o *Imm, size int) []byte {
	e.checkRange(o.Value, size, true)
	if e.ctx.final() && !fitsS8(o.Value, size) {
		fail(fmt.Sprintf("значение %s не помещается в знаковый байт", o.Value))
	}
	return []byte{lowByte(o.Value)}
}

// shortImm — можно ли закодировать число одним байтом со знаковым расширением.
func (e *encoder) shortImm(o *Imm, size int) bool {
	if o.Size == 8 {
		return true
	}
	need := 1
	if !o.Known || fitsS8(o.Value, size) {
		need = 0
	}
	return e.ctx.level("imm", need) == 0
}

func (e *encoder) opSize(ops ...any) int {
	var sizes []int
	addSize := func(s int) {
		for _, x := range sizes {
			if x == s {
				return
			}
		}
		sizes = append(sizes, s)
	}
	for _, o := range ops {
		if isRM(o) && opSizeOf(o) != 0 {
			addSize(opSizeOf(o))
		}
	}
	if len(sizes) > 1 {
		fail(e.mn + ": размеры операндов не совпадают")
	}
	if len(sizes) == 0 {
		for _, o := range ops {
			if isImm(o) && opSizeOf(o) != 0 {
				addSize(opSizeOf(o))
			}
		}
	}
	if len(sizes) == 0 {
		fail(fmt.Sprintf("%s: не удаётся определить размер операнда — укажите byte, word или dword "+
			"(например: %s word [x] - 1)", e.mn, e.mn))
	}
	size := sizes[0]
	if size != 8 && size != 16 && size != 32 {
		fail(fmt.Sprintf("%s: размер %d бит здесь недопустим", e.mn, size))
	}
	return size
}

func (e *encoder) checkMemSize(m *Mem, size int) {
	if m.Size != 0 && m.Size != size {
		fail(fmt.Sprintf("%s: размер памяти (%d бит) не совпадает с регистром (%d бит)", e.mn, m.Size, size))
	}
}

// ------------------------------------------------ адресация

func (e *encoder) dispLevel(m *Mem, asize int, force8 bool) int {
	need := 0
	if m.HasDisp && m.Known {
		d := m.Disp
		if asize == 16 {
			d = mask(d, 16)
			if cmpInt(d, 0x8000) >= 0 {
				d = sub(d, bi(0x10000))
			}
		}
		if d.Sign() != 0 {
			if inRange(d, -128, 127) {
				need = 1
			} else {
				need = 2
			}
		}
	}
	if force8 && need < 1 {
		need = 1
	}
	return e.ctx.level("disp", need)
}

func (e *encoder) dispBytes(m *Mem, lvl, asize int) []byte {
	if lvl == 0 {
		return []byte{}
	}
	if lvl == 1 {
		return []byte{lowByte(m.Disp)}
	}
	e.checkRange(m.Disp, asize, true)
	return pack(m.Disp, asize/8)
}

// memEncode → (размер адреса, байты ModRM [+ SIB] [+ смещение]).
func (e *encoder) memEncode(reg int, m *Mem) (int, []byte) {
	var regs []*Reg
	for _, r := range []*Reg{m.Base, m.Index} {
		if r != nil {
			regs = append(regs, r)
		}
	}
	for _, r := range regs {
		if r.Kind != "gpr" || r.Size == 8 {
			fail(fmt.Sprintf("регистр %s нельзя использовать в адресе", r.Name))
		}
	}
	if len(regs) == 2 && regs[0].Size != regs[1].Size {
		fail("в адресе нельзя смешивать 16- и 32-битные регистры")
	}
	asize := e.bits
	if len(regs) > 0 {
		asize = regs[0].Size
	}

	if asize == 16 {
		if len(regs) == 0 {
			e.checkRange(m.Disp, 16, true)
			return 16, cat([]byte{byte(reg<<3 | 6)}, pack(m.Disp, 2))
		}
		if m.Index != nil && m.Scale != 1 {
			fail("масштаб (*2, *4, *8) недоступен в 16-битной адресации")
		}
		names := make([]string, len(regs))
		for i, r := range regs {
			names[i] = r.Name
		}
		sorted := append([]string(nil), names...)
		sort.Strings(sorted)
		rm, ok := rm16[strings.Join(sorted, "+")]
		if !ok || (len(sorted) == 2 && sorted[0] == sorted[1]) {
			fail(fmt.Sprintf("недопустимый 16-битный адрес [%s]; можно: bx, bp, si, di, "+
				"bx+si, bx+di, bp+si, bp+di (плюс смещение)", strings.Join(names, "+")))
		}
		lvl := e.dispLevel(m, 16, rm == 6)
		return 16, cat([]byte{byte(lvl<<6 | reg<<3 | rm)}, e.dispBytes(m, lvl, 16))
	}

	base, index, scale := m.Base, m.Index, m.Scale
	if index != nil && base == nil && scale == 2 {
		base, scale = index, 1 // [eax*2] → [eax+eax]: короче
	}
	if base == nil && index == nil {
		e.checkRange(m.Disp, 32, true)
		return 32, cat([]byte{byte(reg<<3 | 5)}, pack(m.Disp, 4))
	}
	if index == nil {
		lvl := e.dispLevel(m, 32, base.Num == 5)
		if base.Num == 4 { // esp требует SIB
			return 32, cat([]byte{byte(lvl<<6 | reg<<3 | 4), 0x24}, e.dispBytes(m, lvl, 32))
		}
		return 32, cat([]byte{byte(lvl<<6 | reg<<3 | base.Num)}, e.dispBytes(m, lvl, 32))
	}
	if index.Num == 4 {
		fail("esp нельзя использовать как индексный регистр")
	}
	sb, ok := scaleBits[scale]
	if !ok {
		fail("масштаб должен быть 1, 2, 4 или 8")
	}
	sibHi := sb<<6 | index.Num<<3
	if base == nil {
		e.checkRange(m.Disp, 32, true)
		return 32, cat([]byte{byte(reg<<3 | 4), byte(sibHi | 5)}, pack(m.Disp, 4))
	}
	lvl := e.dispLevel(m, 32, base.Num == 5)
	return 32, cat([]byte{byte(lvl<<6 | reg<<3 | 4), byte(sibHi | base.Num)}, e.dispBytes(m, lvl, 32))
}

// build собирает команду: префиксы + опкод + ModRM/SIB/смещение + число.
func (e *encoder) build(opcode []byte, size int, rm any, reg int, immBytes []byte) []byte {
	out := append([]byte(nil), e.pre...)
	var body []byte
	asize := 0
	switch x := rm.(type) {
	case *Mem:
		asize, body = e.memEncode(reg, x)
		if x.Seg != nil {
			out = append(out, segPrefix[x.Seg.Name])
		}
	case *Reg:
		body = []byte{byte(0xC0 | reg<<3 | x.Num)}
	}
	if (size == 16 || size == 32) && size != e.bits {
		out = append(out, 0x66)
	}
	if asize != 0 && asize != e.bits {
		out = append(out, 0x67)
	}
	return cat(out, opcode, body, immBytes)
}

// moffs — mov al/ax/eax <-> [адрес]: короткая форма без ModRM.
func (e *encoder) moffs(opcode byte, size int, m *Mem) []byte {
	out := append([]byte(nil), e.pre...)
	if m.Seg != nil {
		out = append(out, segPrefix[m.Seg.Name])
	}
	if (size == 16 || size == 32) && size != e.bits {
		out = append(out, 0x66)
	}
	e.checkRange(m.Disp, e.bits, true)
	return cat(out, []byte{opcode}, pack(m.Disp, e.bits/8))
}

// ------------------------------------------------ переходы

func (e *encoder) relJump(target any, shortOp, nearOp []byte, hint string) []byte {
	t, ok := target.(*Imm)
	if !ok {
		e.bad()
	}
	a := e.ctx.addr()
	nd := 4
	if e.bits == 16 {
		nd = 2
	}
	var relS *big.Int
	if shortOp != nil {
		relS = sub(t.Value, addInt(a, len(e.pre)+len(shortOp)+1))
	}
	var lvl int
	switch {
	case nearOp == nil || hint == "short":
		lvl = 0
	case shortOp == nil || hint == "near":
		lvl = 1
	default:
		need := 1
		if !t.Known || inRange(relS, -128, 127) {
			need = 0
		}
		lvl = e.ctx.level("jmp", need)
	}
	if lvl == 0 {
		if e.ctx.final() && !inRange(relS, -128, 127) {
			sign := ""
			if relS.Sign() >= 0 {
				sign = "+"
			}
			fail(fmt.Sprintf("цель слишком далеко для короткого перехода (%s%s байт, "+
				"допустимо от -128 до +127)", sign, relS))
		}
		return cat(e.pre, shortOp, []byte{lowByte(relS)})
	}
	rel := sub(t.Value, addInt(a, len(e.pre)+len(nearOp)+nd))
	return cat(e.pre, nearOp, pack(rel, nd))
}

func (e *encoder) farPtr(opcode byte, o *Far) []byte {
	size := o.Size
	if size == 0 {
		size = e.bits
	}
	if size != 16 && size != 32 {
		fail(e.mn + ": смещение дальнего адреса может быть word или dword")
	}
	e.checkRange(o.Seg, 16, false)
	e.checkRange(o.Off, size, false)
	return e.build([]byte{opcode}, size, nil, 0, cat(pack(o.Off, size/8), pack(o.Seg, 2)))
}

func (e *encoder) indirect(o any, nearN, farN int) []byte {
	if m, ok := o.(*Mem); ok && m.Jump == "far" {
		return e.build([]byte{0xff}, 0, o, farN, nil)
	}
	if isRM(o) {
		size := opSizeOf(o)
		if size == 0 {
			size = e.bits
		}
		if size != 16 && size != 32 {
			e.bad()
		}
		return e.build([]byte{0xff}, size, o, nearN, nil)
	}
	e.bad()
	return nil
}

// ------------------------------------------------ диспетчер

func (e *encoder) encode(ops []any) []byte {
	mn := e.mn
	if b, ok := simpleOps[mn]; ok {
		e.nops(ops, 0)
		return cat(e.pre, b)
	}
	if s, ok := sizedOps[mn]; ok {
		e.nops(ops, 0)
		return e.build([]byte{s.opcode}, s.size, nil, 0, nil)
	}
	if n, ok := aluOps[mn]; ok {
		return e.opALU(ops, n)
	}
	if n, ok := shiftOps[mn]; ok {
		return e.opShift(ops, n)
	}
	if n, ok := group3Ops[mn]; ok {
		return e.opGroup3(ops, n)
	}
	if b, ok := bitTestOps[mn]; ok {
		return e.opBT(ops, b.opcode, b.n)
	}
	if b, ok := segLoadOps[mn]; ok {
		return e.opSegLoad(ops, b)
	}
	if d, ok := descriptorOps[mn]; ok {
		return e.opDescriptor(ops, d)
	}
	if strings.HasPrefix(mn, "set") {
		if c, ok := cc[mn[3:]]; ok {
			return e.opSetCC(ops, c)
		}
	}
	if strings.HasPrefix(mn, "cmov") {
		if c, ok := cc[mn[4:]]; ok {
			return e.opCmovCC(ops, c)
		}
	}
	if strings.HasPrefix(mn, "j") {
		if c, ok := cc[mn[1:]]; ok {
			e.nops(ops, 1)
			hint := ""
			if t, ok := ops[0].(*Imm); ok {
				hint = t.Jump
			}
			return e.relJump(ops[0], []byte{byte(0x70 + c)}, []byte{0x0F, byte(0x80 + c)}, hint)
		}
	}
	switch mn {
	case "mov":
		return e.opMov(ops)
	case "xchg":
		return e.opXchg(ops)
	case "lea":
		return e.opLea(ops)
	case "movzx":
		return e.movx(ops, []byte{0x0f, 0xb6}, []byte{0x0f, 0xb7})
	case "movsx":
		return e.movx(ops, []byte{0x0f, 0xbe}, []byte{0x0f, 0xbf})
	case "push":
		return e.opPush(ops)
	case "pop":
		return e.opPop(ops)
	case "test":
		return e.opTest(ops)
	case "inc":
		return e.incDec(ops, 0)
	case "dec":
		return e.incDec(ops, 1)
	case "imul":
		return e.opImul(ops)
	case "bsf":
		return e.bitScan(ops, []byte{0x0f, 0xbc})
	case "bsr":
		return e.bitScan(ops, []byte{0x0f, 0xbd})
	case "bswap":
		return e.opBswap(ops)
	case "jmp":
		return e.opJmp(ops)
	case "call":
		return e.opCall(ops)
	case "loop":
		return e.loopish(ops, 0xE2, false)
	case "loope":
		return e.loopish(ops, 0xE1, false)
	case "loopne":
		return e.loopish(ops, 0xE0, false)
	case "jcxz":
		return e.loopish(ops, 0xE3, e.bits == 32)
	case "jecxz":
		return e.loopish(ops, 0xE3, e.bits == 16)
	case "ret":
		return e.ret(ops, 0xC3, 0xC2)
	case "retf":
		return e.ret(ops, 0xCB, 0xCA)
	case "int":
		e.nops(ops, 1)
		return cat(e.pre, []byte{0xcd}, e.imm(ops[0], 8, false))
	case "enter":
		e.nops(ops, 2)
		return cat(e.pre, []byte{0xc8}, e.imm(ops[0], 16, false), e.imm(ops[1], 8, false))
	case "in":
		return e.opIn(ops)
	case "out":
		return e.opOut(ops)
	}
	panic("неизвестная команда " + mn)
}

// ------------------------------------------------ пересылка

func (e *encoder) opMov(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if d == any(a20) {
		return e.a20(s)
	}
	if s == any(a20) {
		fail("линию a20 можно только переключать: mov a20 - 1 или mov a20 - 0")
	}

	// управляющие и отладочные регистры
	if r, ok := d.(*Reg); ok && (r.Kind == "cr" || r.Kind == "dr") {
		if !(isGpr(s) && s.(*Reg).Size == 32) {
			fail(fmt.Sprintf("mov %s - ...: источник должен быть 32-битным регистром (например eax)", r.Name))
		}
		op := []byte{0x0f, 0x22}
		if r.Kind == "dr" {
			op = []byte{0x0f, 0x23}
		}
		return e.build(op, 0, s, r.Num, nil)
	}
	if r, ok := s.(*Reg); ok && (r.Kind == "cr" || r.Kind == "dr") {
		if !(isGpr(d) && d.(*Reg).Size == 32) {
			fail(fmt.Sprintf("mov ... - %s: приёмник должен быть 32-битным регистром (например eax)", r.Name))
		}
		op := []byte{0x0f, 0x20}
		if r.Kind == "dr" {
			op = []byte{0x0f, 0x21}
		}
		return e.build(op, 0, d, r.Num, nil)
	}

	// сегментные регистры
	if r, ok := d.(*Reg); ok && r.Kind == "seg" {
		if r.Name == "cs" {
			fail("cs нельзя загрузить через mov — используйте дальний переход jmp сегмент:смещение")
		}
		if isGpr(s) && s.(*Reg).Size != 8 {
			return e.build([]byte{0x8e}, 0, s, r.Num, nil)
		}
		if m, ok := s.(*Mem); ok && (m.Size == 0 || m.Size == 16) {
			return e.build([]byte{0x8e}, 0, s, r.Num, nil)
		}
		if isImm(s) {
			fail(fmt.Sprintf("число нельзя загрузить прямо в %s: сначала mov ax - число, затем mov %s - ax", r.Name, r.Name))
		}
		e.bad()
	}
	if r, ok := s.(*Reg); ok && r.Kind == "seg" {
		if isGpr(d) && d.(*Reg).Size != 8 {
			return e.build([]byte{0x8c}, d.(*Reg).Size, d, r.Num, nil)
		}
		if m, ok := d.(*Mem); ok && (m.Size == 0 || m.Size == 16) {
			return e.build([]byte{0x8c}, 0, d, r.Num, nil)
		}
		e.bad()
	}

	if isGpr(d) {
		dr := d.(*Reg)
		size := dr.Size
		w := 1
		if size == 8 {
			w = 0
		}
		if isGpr(s) {
			sr := s.(*Reg)
			if sr.Size != size {
				fail(fmt.Sprintf("mov: размеры регистров %s и %s не совпадают", dr.Name, sr.Name))
			}
			return e.build([]byte{byte(0x88 + w)}, size, d, sr.Num, nil)
		}
		if m, ok := s.(*Mem); ok {
			e.checkMemSize(m, size)
			if dr.Num == 0 && m.direct() {
				return e.moffs(byte(0xA0+w), size, m)
			}
			return e.build([]byte{byte(0x8A + w)}, size, s, dr.Num, nil)
		}
		if isImm(s) {
			op := 0xB8
			if w == 0 {
				op = 0xB0
			}
			return e.build([]byte{byte(op + dr.Num)}, size, nil, 0, e.imm(s, size, true))
		}
		e.bad()
	}
	if m, ok := d.(*Mem); ok {
		if isGpr(s) {
			sr := s.(*Reg)
			size := sr.Size
			w := 1
			if size == 8 {
				w = 0
			}
			e.checkMemSize(m, size)
			if sr.Num == 0 && m.direct() {
				return e.moffs(byte(0xA2+w), size, m)
			}
			return e.build([]byte{byte(0x88 + w)}, size, d, sr.Num, nil)
		}
		if isImm(s) {
			size := e.opSize(d, s)
			w := 1
			if size == 8 {
				w = 0
			}
			return e.build([]byte{byte(0xC6 + w)}, size, d, 0, e.imm(s, size, true))
		}
		if isMem(s) {
			fail("mov: нельзя переслать память в память — используйте регистр")
		}
	}
	e.bad()
	return nil
}

func (e *encoder) a20(s any) []byte {
	x, ok := s.(*Imm)
	if !ok || !(eqInt(x.Value, 0) || eqInt(x.Value, 1)) {
		fail("a20 принимает только 0 (выключить) или 1 (включить)")
	}
	var o32 []byte
	if e.bits == 32 {
		o32 = []byte{0x66}
	}
	push, pop := cat(o32, []byte{0x50}), cat(o32, []byte{0x58}) // сохраняем ax/eax
	var body []byte
	if eqInt(x.Value, 1) {
		// in al, 0x92 / or al, 2 / and al, 0xFE / out 0x92, al
		body = []byte{0xe4, 0x92, 0x0c, 0x02, 0x24, 0xfe, 0xe6, 0x92}
	} else {
		// in al, 0x92 / and al, 0xFC / out 0x92, al
		body = []byte{0xe4, 0x92, 0x24, 0xfc, 0xe6, 0x92}
	}
	return cat(e.pre, push, body, pop)
}

func (e *encoder) opXchg(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if isGpr(d) && isGpr(s) {
		dr, sr := d.(*Reg), s.(*Reg)
		if dr.Size != sr.Size {
			fail("xchg: размеры регистров не совпадают")
		}
		if dr.Size != 8 && (dr.Num == 0 || sr.Num == 0) {
			other := dr
			if dr.Num == 0 {
				other = sr
			}
			return e.build([]byte{byte(0x90 + other.Num)}, dr.Size, nil, 0, nil)
		}
		op := byte(0x87)
		if dr.Size == 8 {
			op = 0x86
		}
		return e.build([]byte{op}, dr.Size, s, dr.Num, nil)
	}
	if isGpr(d) && isMem(s) {
		d, s = s, d
	}
	if m, ok := d.(*Mem); ok && isGpr(s) {
		sr := s.(*Reg)
		e.checkMemSize(m, sr.Size)
		op := byte(0x87)
		if sr.Size == 8 {
			op = 0x86
		}
		return e.build([]byte{op}, sr.Size, d, sr.Num, nil)
	}
	e.bad()
	return nil
}

func (e *encoder) opLea(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isMem(s)) {
		fail("lea: нужно lea регистр16/32 - [адрес]")
	}
	return e.build([]byte{0x8d}, d.(*Reg).Size, s, d.(*Reg).Num, nil)
}

func (e *encoder) opSegLoad(ops []any, opcode []byte) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isMem(s)) {
		e.bad()
	}
	return e.build(opcode, d.(*Reg).Size, s, d.(*Reg).Num, nil)
}

func (e *encoder) movx(ops []any, op8, op16 []byte) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isRM(s)) {
		e.bad()
	}
	dr := d.(*Reg)
	ss := opSizeOf(s)
	if ss == 0 {
		fail(e.mn + ": укажите размер источника: byte или word")
	}
	if ss == 8 {
		return e.build(op8, dr.Size, s, dr.Num, nil)
	}
	if ss == 16 && dr.Size == 32 {
		return e.build(op16, dr.Size, s, dr.Num, nil)
	}
	e.bad()
	return nil
}

func (e *encoder) opPush(ops []any) []byte {
	e.nops(ops, 1)
	switch o := ops[0].(type) {
	case *Reg:
		if o.Kind == "seg" {
			return cat(e.pre, segPush[o.Name])
		}
		if o.Kind == "gpr" && o.Size != 8 {
			return e.build([]byte{byte(0x50 + o.Num)}, o.Size, nil, 0, nil)
		}
		e.bad()
	case *Imm:
		size := e.bits
		if o.Size == 16 || o.Size == 32 {
			size = o.Size
		}
		if e.shortImm(o, size) {
			return e.build([]byte{0x6a}, size, nil, 0, e.immS8(o, size))
		}
		return e.build([]byte{0x68}, size, nil, 0, e.imm(o, size, true))
	case *Mem:
		if o.Size != 16 && o.Size != 32 {
			fail("push: укажите размер памяти: word или dword")
		}
		return e.build([]byte{0xff}, o.Size, o, 6, nil)
	}
	e.bad()
	return nil
}

func (e *encoder) opPop(ops []any) []byte {
	e.nops(ops, 1)
	switch o := ops[0].(type) {
	case *Reg:
		if o.Kind == "seg" {
			if o.Name == "cs" {
				fail("pop cs недопустим")
			}
			return cat(e.pre, segPop[o.Name])
		}
		if o.Kind == "gpr" && o.Size != 8 {
			return e.build([]byte{byte(0x58 + o.Num)}, o.Size, nil, 0, nil)
		}
		e.bad()
	case *Mem:
		if o.Size != 16 && o.Size != 32 {
			fail("pop: укажите размер памяти: word или dword")
		}
		return e.build([]byte{0x8f}, o.Size, o, 0, nil)
	}
	e.bad()
	return nil
}

// ------------------------------------------------ арифметика и логика

func (e *encoder) opALU(ops []any, n int) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if si, ok := s.(*Imm); ok && isRM(d) {
		size := e.opSize(d, s)
		if size == 8 {
			if isAcc(d) {
				return e.build([]byte{byte(n*8 + 4)}, 8, nil, 0, e.imm(s, 8, true))
			}
			return e.build([]byte{0x80}, 8, d, n, e.imm(s, 8, true))
		}
		if e.shortImm(si, size) {
			return e.build([]byte{0x83}, size, d, n, e.immS8(si, size))
		}
		if isAcc(d) {
			return e.build([]byte{byte(n*8 + 5)}, size, nil, 0, e.imm(s, size, true))
		}
		return e.build([]byte{0x81}, size, d, n, e.imm(s, size, true))
	}
	if isGpr(s) && isRM(d) {
		size := e.opSize(d, s)
		w := 1
		if size == 8 {
			w = 0
		}
		return e.build([]byte{byte(n*8 + w)}, size, d, s.(*Reg).Num, nil)
	}
	if isGpr(d) && isMem(s) {
		size := e.opSize(d, s)
		w := 3
		if size == 8 {
			w = 2
		}
		return e.build([]byte{byte(n*8 + w)}, size, s, d.(*Reg).Num, nil)
	}
	if isMem(d) && isMem(s) {
		fail(e.mn + ": нельзя использовать два операнда в памяти")
	}
	e.bad()
	return nil
}

func (e *encoder) opTest(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if isImm(d) && isRM(s) {
		d, s = s, d
	}
	if isImm(s) && isRM(d) {
		size := e.opSize(d, s)
		w := 1
		if size == 8 {
			w = 0
		}
		if isAcc(d) {
			return e.build([]byte{byte(0xA8 + w)}, size, nil, 0, e.imm(s, size, true))
		}
		return e.build([]byte{byte(0xF6 + w)}, size, d, 0, e.imm(s, size, true))
	}
	if isGpr(d) && isMem(s) {
		d, s = s, d
	}
	if isGpr(s) && isRM(d) {
		size := e.opSize(d, s)
		w := 1
		if size == 8 {
			w = 0
		}
		return e.build([]byte{byte(0x84 + w)}, size, d, s.(*Reg).Num, nil)
	}
	e.bad()
	return nil
}

func (e *encoder) incDec(ops []any, n int) []byte {
	e.nops(ops, 1)
	o := ops[0]
	if !isRM(o) {
		e.bad()
	}
	size := e.opSize(o)
	if isGpr(o) && size != 8 {
		return e.build([]byte{byte(0x40 + 8*n + o.(*Reg).Num)}, size, nil, 0, nil)
	}
	op := byte(0xff)
	if size == 8 {
		op = 0xfe
	}
	return e.build([]byte{op}, size, o, n, nil)
}

func (e *encoder) opGroup3(ops []any, n int) []byte {
	e.nops(ops, 1)
	o := ops[0]
	if !isRM(o) {
		e.bad()
	}
	size := e.opSize(o)
	op := byte(0xf7)
	if size == 8 {
		op = 0xf6
	}
	return e.build([]byte{op}, size, o, n, nil)
}

func (e *encoder) opImul(ops []any) []byte {
	e.nops(ops, 1, 2, 3)
	if len(ops) == 1 {
		return e.opGroup3(ops, 5)
	}
	if len(ops) == 2 && isImm(ops[1]) {
		ops = []any{ops[0], ops[0], ops[1]}
	}
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isRM(s)) {
		e.bad()
	}
	size := e.opSize(d, s)
	dn := d.(*Reg).Num
	if len(ops) == 2 {
		return e.build([]byte{0x0f, 0xaf}, size, s, dn, nil)
	}
	k, ok := ops[2].(*Imm)
	if !ok {
		e.bad()
	}
	if e.shortImm(k, size) {
		return e.build([]byte{0x6b}, size, s, dn, e.immS8(k, size))
	}
	return e.build([]byte{0x69}, size, s, dn, e.imm(k, size, true))
}

func (e *encoder) opShift(ops []any, n int) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !isRM(d) {
		e.bad()
	}
	size := e.opSize(d)
	w := 1
	if size == 8 {
		w = 0
	}
	if r, ok := s.(*Reg); ok && r.Name == "cl" {
		return e.build([]byte{byte(0xD2 + w)}, size, d, n, nil)
	}
	if x, ok := s.(*Imm); ok {
		if x.Known && eqInt(x.Value, 1) {
			return e.build([]byte{byte(0xD0 + w)}, size, d, n, nil)
		}
		e.checkRange(x.Value, 8, false)
		return e.build([]byte{byte(0xC0 + w)}, size, d, n, []byte{lowByte(x.Value)})
	}
	fail(e.mn + ": величина сдвига — число или регистр cl")
	return nil
}

func (e *encoder) opBT(ops []any, opcode byte, n int) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !isRM(d) {
		e.bad()
	}
	if isGpr(s) {
		size := e.opSize(d, s)
		if size == 8 {
			e.bad()
		}
		return e.build([]byte{0x0F, opcode}, size, d, s.(*Reg).Num, nil)
	}
	if isImm(s) {
		size := e.opSize(d)
		if size == 8 {
			e.bad()
		}
		return e.build([]byte{0x0f, 0xba}, size, d, n, e.imm(s, 8, false))
	}
	e.bad()
	return nil
}

func (e *encoder) bitScan(ops []any, opcode []byte) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isRM(s)) {
		e.bad()
	}
	size := e.opSize(d, s)
	return e.build(opcode, size, s, d.(*Reg).Num, nil)
}

func (e *encoder) opBswap(ops []any) []byte {
	e.nops(ops, 1)
	o := ops[0]
	if !(isGpr(o) && o.(*Reg).Size == 32) {
		fail("bswap: нужен 32-битный регистр")
	}
	return e.build([]byte{0x0F, byte(0xC8 + o.(*Reg).Num)}, 32, nil, 0, nil)
}

func (e *encoder) opSetCC(ops []any, c int) []byte {
	e.nops(ops, 1)
	o := ops[0]
	if !isRM(o) || (opSizeOf(o) != 0 && opSizeOf(o) != 8) {
		fail(e.mn + ": нужен 8-битный регистр или byte [память]")
	}
	return e.build([]byte{0x0F, byte(0x90 + c)}, 0, o, 0, nil)
}

func (e *encoder) opCmovCC(ops []any, c int) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !(isGpr(d) && d.(*Reg).Size != 8 && isRM(s)) {
		e.bad()
	}
	size := e.opSize(d, s)
	return e.build([]byte{0x0F, byte(0x40 + c)}, size, s, d.(*Reg).Num, nil)
}

// ------------------------------------------------ переходы и вызовы

func (e *encoder) opJmp(ops []any) []byte {
	e.nops(ops, 1)
	switch o := ops[0].(type) {
	case *Far:
		return e.farPtr(0xEA, o)
	case *Imm:
		if o.Jump == "far" {
			fail("дальний переход записывается так: jmp сегмент:смещение")
		}
		return e.relJump(o, []byte{0xeb}, []byte{0xe9}, o.Jump)
	}
	return e.indirect(ops[0], 4, 5)
}

func (e *encoder) opCall(ops []any) []byte {
	e.nops(ops, 1)
	switch o := ops[0].(type) {
	case *Far:
		return e.farPtr(0x9A, o)
	case *Imm:
		if o.Jump == "far" || o.Jump == "short" {
			fail("call бывает ближним (call метка) или дальним (call сегмент:смещение)")
		}
		return e.relJump(o, nil, []byte{0xe8}, "near")
	}
	return e.indirect(ops[0], 2, 3)
}

func (e *encoder) loopish(ops []any, opcode byte, addr32 bool) []byte {
	e.nops(ops, 1)
	if !isImm(ops[0]) {
		e.bad()
	}
	op := []byte{opcode}
	if addr32 {
		op = []byte{0x67, opcode}
	}
	return e.relJump(ops[0], op, nil, "short")
}

func (e *encoder) ret(ops []any, op0, opn byte) []byte {
	e.nops(ops, 0, 1)
	if len(ops) == 0 {
		return cat(e.pre, []byte{op0})
	}
	return cat(e.pre, []byte{opn}, e.imm(ops[0], 16, false))
}

// ------------------------------------------------ порты ввода-вывода

func (e *encoder) opIn(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !isAcc(d) {
		fail("in: приёмник должен быть al, ax или eax")
	}
	dr := d.(*Reg)
	w := 1
	if dr.Size == 8 {
		w = 0
	}
	if isImm(s) {
		return e.build([]byte{byte(0xE4 + w)}, dr.Size, nil, 0, e.imm(s, 8, false))
	}
	if r, ok := s.(*Reg); ok && r.Name == "dx" {
		return e.build([]byte{byte(0xEC + w)}, dr.Size, nil, 0, nil)
	}
	fail("in: номер порта — число 0..255 или регистр dx")
	return nil
}

func (e *encoder) opOut(ops []any) []byte {
	e.nops(ops, 2)
	d, s := ops[0], ops[1]
	if !isAcc(s) {
		fail("out: источник должен быть al, ax или eax")
	}
	sr := s.(*Reg)
	w := 1
	if sr.Size == 8 {
		w = 0
	}
	if isImm(d) {
		return e.build([]byte{byte(0xE6 + w)}, sr.Size, nil, 0, e.imm(d, 8, false))
	}
	if r, ok := d.(*Reg); ok && r.Name == "dx" {
		return e.build([]byte{byte(0xEE + w)}, sr.Size, nil, 0, nil)
	}
	fail("out: номер порта — число 0..255 или регистр dx")
	return nil
}

// ------------------------------------------------ системные

func (e *encoder) opDescriptor(ops []any, d descOp) []byte {
	e.nops(ops, 1)
	o := ops[0]
	if isMem(o) || (d.allowReg && isGpr(o) && o.(*Reg).Size == 16) {
		return e.build(d.opcode, 0, o, d.n, nil)
	}
	msg := e.mn + ": нужен операнд в памяти"
	if d.allowReg {
		msg += " или 16-битный регистр"
	}
	fail(msg)
	return nil
}
