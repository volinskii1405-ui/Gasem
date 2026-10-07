package gasem

// Многопроходная сборка программы Gasem в плоский двоичный файл.
//
// Проходы повторяются, пока адреса меток не перестанут меняться. Короткие
// формы команд (переходы на 2 байта, числа в 1 байт) выбираются оптимистично
// и только растут от прохода к проходу, поэтому процесс всегда сходится.

import (
	"bytes"
	"fmt"
	"math/big"
	"sort"
	"strings"
)

const (
	maxPasses = 100
	maxOutput = 64 * 1024 * 1024
	maxErrors = 50
)

// Symbol — имя и значение метки или константы.
type Symbol struct {
	Name  string
	Value *big.Int
}

// LineInfo — какие байты образа получились из строки исходника (для отладчика).
type LineInfo struct {
	Addr *big.Int
	Size int
	Bits int
	Loc  *SourceLoc
}

// Result — результат компиляции.
type Result struct {
	Code     []byte     // готовый машинный код
	Origin   *big.Int   // адрес загрузки (og)
	Symbols  []Symbol   // имя → значение, в порядке определения
	Lines    []LineInfo // для отладчика
	Warnings []*Error

	listing     string
	makeListing func() string // листинг строится, только когда он нужен
}

// Listing — текст листинга: адреса, байты и строки исходника, затем символы.
func (r *Result) Listing() string {
	if r.makeListing != nil {
		r.listing, r.makeListing = r.makeListing(), nil
	}
	return r.listing
}

// SymbolMap — символы в виде словаря.
func (r *Result) SymbolMap() map[string]*big.Int {
	m := make(map[string]*big.Int, len(r.Symbols))
	for _, s := range r.Symbols {
		m[s.Name] = s.Value
	}
	return m
}

// SortedSymbols — символы программы по возрастанию адреса (без служебных @...).
func (r *Result) SortedSymbols() []Symbol {
	return sortedSymbols(r.Symbols)
}

func sortedSymbols(list []Symbol) []Symbol {
	out := make([]Symbol, 0, len(list))
	for _, s := range list {
		if !strings.HasPrefix(s.Name, "@") {
			out = append(out, s)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if c := out[i].Value.Cmp(out[j].Value); c != 0 {
			return c < 0
		}
		return out[i].Name < out[j].Name
	})
	return out
}

// FormatSymbol — «адрес имя» как в листинге и карте символов.
func FormatSymbol(v *big.Int) string {
	return fmt.Sprintf("%08X", mask(v, 32))
}

// ---------------------------------------------------------------- контекст кодировщика

type passCtx struct {
	asm   *assembler
	flags map[string]int
	rep   string
	at    *big.Int
}

// level — выбранный размер кодировки (0 — самый короткий). Только растёт.
func (c *passCtx) level(key string, need int) int {
	k := c.rep + key
	prev := c.flags[k]
	if need > prev {
		c.flags[k] = need
		c.asm.changed = true
		return need
	}
	return prev
}

func (c *passCtx) addr() *big.Int { return c.at }
func (c *passCtx) bits() int      { return c.asm.bits }
func (c *passCtx) final() bool    { return c.asm.final }

// ---------------------------------------------------------------- сборщик

type record struct {
	loc  *SourceLoc
	addr *big.Int
	data []byte
}

type assembler struct {
	stmts     []Stmt
	varDefs   map[string]*DataStmt
	lines     []*SourceLoc
	prev      map[string]*big.Int
	cur       map[string]*big.Int
	curOrder  []string
	final     bool
	changed   bool
	errors    []*Error
	out       []byte
	org       *big.Int
	orgSeen   bool
	bits      int
	doStrings map[string]*big.Int
	records   []record
	lineMap   []LineInfo
	shift     *big.Int  // at: адрес, по которому код работает, минус адрес в файле
	atStack   []atFrame // блоки at
	curRel    map[string]int
	prevRel   map[string]int
}

type atFrame struct {
	shift, addr *big.Int
}

func newAssembler(p *Parser) *assembler {
	return &assembler{stmts: p.statements, varDefs: p.varDefs, lines: p.lines, prev: map[string]*big.Int{},
		curRel: map[string]int{}}
}

func symbolsEqual(a, b map[string]*big.Int) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		w, ok := b[k]
		if !ok || v.Cmp(w) != 0 {
			return false
		}
	}
	return true
}

// ------------------------------------------------ проходы

func (a *assembler) assemble() (*Result, *Errors) {
	for i := 0; i < maxPasses; i++ {
		a.runPass(false)
		if len(a.errors) > 0 {
			return nil, &Errors{a.errors}
		}
		stable := !a.changed && symbolsEqual(a.cur, a.prev)
		a.prev = a.cur
		if !stable {
			continue
		}
		a.runPass(true)
		if len(a.errors) > 0 {
			return nil, &Errors{a.errors}
		}
		if !a.changed && symbolsEqual(a.cur, a.prev) {
			syms := make([]Symbol, len(a.curOrder))
			for i, name := range a.curOrder {
				syms[i] = Symbol{name, a.cur[name]}
			}
			code := a.out
			if code == nil {
				code = []byte{}
			}
			return &Result{Code: code, Origin: a.org, Symbols: syms, Lines: a.lineMap,
				makeListing: a.makeListing}, nil
		}
		a.prev = a.cur
	}
	return nil, &Errors{[]*Error{{Message: fmt.Sprintf(
		"размеры команд не стабилизировались за %d проходов "+
			"(возможно, адреса зависят друг от друга по кругу)", maxPasses), Col: NoCol}}}
}

func (a *assembler) runPass(final bool) {
	a.final = final
	a.prevRel = a.curRel
	a.curRel = map[string]int{}
	a.cur = map[string]*big.Int{}
	a.curOrder = nil
	a.changed = false
	a.errors = nil
	a.out = make([]byte, 0, max(len(a.out), 512)) // размер программы почти не меняется между проходами
	a.org = bi(0)
	a.orgSeen = false
	a.bits = 16
	a.doStrings = map[string]*big.Int{}
	a.records = nil
	a.lineMap = nil
	a.shift = bi(0)
	a.atStack = nil
	for _, st := range a.stmts {
		start := len(a.out)
		addr := a.here()
		if e := catch(func() { a.exec(st, "") }); e != nil {
			if e.Loc == nil {
				e.Loc = st.Location()
			}
			a.errors = append(a.errors, e)
			a.out = a.out[:start]
			if len(a.errors) >= maxErrors {
				break
			}
		}
		if final {
			switch st.(type) {
			case *OrgStmt, *AtStmt, *AtEndStmt:
				addr = a.here()
			}
			data := append([]byte(nil), a.out[start:]...)
			top := st.Location().Top()
			a.records = append(a.records, record{top, addr, data})
			if len(a.out) > start {
				a.lineMap = append(a.lineMap, LineInfo{addr, len(a.out) - start, a.bits, top})
			}
		}
	}
}

// ------------------------------------------------ символы

func (a *assembler) lookup(name string) *big.Int {
	if v, ok := a.cur[name]; ok {
		return v
	}
	return a.prev[name]
}

// knownNames — все известные имена (для подсказок).
func (a *assembler) knownNames() []string {
	seen := map[string]bool{}
	var out []string
	for _, m := range []map[string]*big.Int{a.cur, a.prev} {
		for k := range m {
			if !seen[k] {
				seen[k] = true
				out = append(out, k)
			}
		}
	}
	return out
}

func (a *assembler) define(name string, v *big.Int, col int, reloc int) {
	if _, ok := a.cur[name]; ok {
		failCol(fmt.Sprintf("имя '%s' уже определено", name), col)
	}
	a.cur[name] = v
	a.curRel[name] = reloc
	a.curOrder = append(a.curOrder, name)
}

// reloc — сколько раз в выражение входит адрес (метка, $, $$): 1 — это адрес в программе,
// 0 — просто число. В режиме b 64 [адрес] кодируется относительно rip, [число] — как есть.
func (a *assembler) reloc(node Expr) int {
	switch n := node.(type) {
	case *Sym:
		if r, ok := a.curRel[n.Name]; ok {
			return r
		}
		if r, ok := a.prevRel[n.Name]; ok {
			return r
		}
		return 1
	case *Here, *Start:
		return 1
	case *Unary:
		r := a.reloc(n.X)
		switch n.Op {
		case "-":
			return -r
		case "+":
			return r
		}
		return 0
	case *Binary:
		x, y := a.reloc(n.A), a.reloc(n.B)
		switch n.Op {
		case "+":
			return x + y
		case "-":
			return x - y
		}
		return 0
	}
	return 0
}

func (a *assembler) here() *big.Int { return add(addInt(a.org, len(a.out)), a.shift) }

// start — $$: начало программы (og) или текущего блока at.
func (a *assembler) start() *big.Int {
	if n := len(a.atStack); n > 0 {
		return a.atStack[n-1].addr
	}
	return a.org
}

func (a *assembler) evalInt(expr Expr, here *big.Int, needKnown bool, what string) (*big.Int, bool) {
	ev := newEvaluator(a, here, "")
	v := toInt(ev.eval(expr), expr.Col())
	if needKnown && !ev.known {
		failCol(what+" должно быть известно в этом месте (без ссылок вперёд)", expr.Col())
	}
	return v, ev.known
}

// ------------------------------------------------ выполнение операторов

func (a *assembler) exec(st Stmt, rep string) {
	here := a.here()

	switch s := st.(type) {
	case *LabelStmt:
		a.define(s.Name, here, s.Col, 1)

	case *ConstStmt:
		v, _ := a.evalInt(s.Expr, here, false, "значение")
		a.define(s.Name, v, s.Col, a.reloc(s.Expr))

	case *OrgStmt:
		if n := len(a.atStack); n > 0 {
			v, _ := a.evalInt(s.Expr, here, true, "адрес og")
			if v.Cmp(a.atStack[n-1].addr) != 0 {
				fail(fmt.Sprintf("og внутри at должен совпадать с адресом at (%#x), указано %#x", a.atStack[n-1].addr, v))
			}
			return
		}
		if a.orgSeen {
			fail("og можно указать только один раз")
		}
		if len(a.out) > 0 {
			fail("og должна стоять до первой команды или данных")
		}
		v, _ := a.evalInt(s.Expr, here, true, "адрес og")
		if v.Sign() < 0 {
			fail("адрес og не может быть отрицательным")
		}
		a.org = v
		a.orgSeen = true

	case *BitsStmt:
		v, _ := a.evalInt(s.Expr, here, true, "режим")
		if !eqInt(v, 16) && !eqInt(v, 32) && !eqInt(v, 64) {
			fail(fmt.Sprintf("поддерживаются режимы b 16, b 32 и b 64 (указано %s)", v))
		}
		a.bits = small(v)

	case *AlignStmt:
		n, _ := a.evalInt(s.Expr, here, true, "выравнивание")
		if n.Sign() <= 0 || band(n, sub(n, bi(1))).Sign() != 0 {
			fail(fmt.Sprintf("выравнивание должно быть степенью двойки (указано %s)", n))
		}
		fill := bi(0x90)
		if s.Fill != nil {
			fill, _ = a.evalInt(s.Fill, here, false, "значение")
			if !inRange(fill, 0, 255) {
				fail("байт заполнения должен быть от 0 до 255")
			}
		}
		count := new(big.Int).Mod(neg(here), n)
		if cmpInt(count, int64(maxOutput-len(a.out))) > 0 {
			fail("программа получается слишком большой (больше 64 МБ)")
		}
		a.emit(bytes.Repeat([]byte{byte(fill.Int64())}, small(count)))

	case *DataStmt:
		a.emit(a.dataBytes(s, here))

	case *TimesStmt:
		n, _ := a.evalInt(s.Count, here, false, "значение")
		if n.Sign() < 0 {
			if a.final {
				fail(fmt.Sprintf("отрицательное число повторений (%s): код не помещается в отведённое место "+
					"(например, загрузочный сектор длиннее 510 байт)", n))
			}
			n = bi(0)
		}
		if cmpInt(n, maxOutput) > 0 {
			fail(fmt.Sprintf("слишком большое число повторений (%s)", n))
		}
		count := small(n)
		for i := 0; i < count; i++ {
			a.exec(s.Body, fmt.Sprintf("%s%d,", rep, i))
		}

	case *InstrStmt:
		ctx := &passCtx{asm: a, flags: s.Flags, rep: rep + "|", at: here}
		ops := make([]any, len(s.Operands))
		for i, o := range s.Operands {
			ops[i] = a.resolve(o, here)
		}
		a.emit(encode(s.Mnemonic, ops, s.Prefixes, ctx))

	case *DoStmt:
		a.execDo(s, rep, here)

	case *IncbinStmt:
		a.emit(s.Data)

	case *AtStmt:
		a.atStack = append(a.atStack, atFrame{a.shift, here}) // при ошибке в адресе блок остаётся на месте
		v, _ := a.evalInt(s.Expr, here, true, "адрес at")
		if v.Sign() < 0 {
			fail("адрес at не может быть отрицательным")
		}
		a.atStack[len(a.atStack)-1] = atFrame{a.shift, v}
		a.shift = sub(v, addInt(a.org, len(a.out)))

	case *AtEndStmt:
		n := len(a.atStack)
		a.shift = a.atStack[n-1].shift
		a.atStack = a.atStack[:n-1]

	default:
		panic(fmt.Sprintf("неизвестный оператор %T", st))
	}
}

func (a *assembler) emit(data []byte) {
	if len(a.out)+len(data) > maxOutput {
		fail("программа получается слишком большой (больше 64 МБ)")
	}
	a.out = append(a.out, data...)
}

// resolve — операнд из дерева → операнд для кодировщика (с вычисленными числами).
func (a *assembler) resolve(op Operand, here *big.Int) any {
	switch o := op.(type) {
	case *RegOperand:
		return o.Reg
	case *A20Operand:
		return a20
	case *MemOperand:
		disp, known := bi(0), true
		if o.Disp != nil {
			disp, known = a.evalInt(o.Disp, here, false, "значение")
		}
		rel := a.bits == 64 && o.Base == nil && o.Index == nil && o.Disp != nil && o.Mode != "abs" &&
			!(o.Seg != nil && (o.Seg.Name == "fs" || o.Seg.Name == "gs")) && a.reloc(o.Disp) == 1
		return &Mem{Size: o.Size, Seg: o.Seg, Base: o.Base, Index: o.Index, Scale: o.Scale, Disp: disp,
			Known: known, HasDisp: o.Disp != nil, Jump: o.Jump, Rel: rel}
	case *ImmOperand:
		v, known := a.evalInt(o.Expr, here, false, "значение")
		return &Imm{Value: v, Known: known, Size: o.Size, Jump: o.Jump}
	case *FarOperand:
		seg, k1 := a.evalInt(o.Seg, here, false, "значение")
		off, k2 := a.evalInt(o.Off, here, false, "значение")
		return &Far{Seg: seg, Off: off, Known: k1 && k2, Size: o.Size}
	}
	panic(fmt.Sprintf("неизвестный операнд %T", op))
}

// ------------------------------------------------ данные

func (a *assembler) itemBytes(node Expr, unit int, ev *evaluator, name string) []byte {
	if s, ok := node.(*Str); ok {
		data := append([]byte{}, s.Value...)
		if len(data)%unit != 0 {
			data = append(data, make([]byte, unit-len(data)%unit)...)
		}
		return data
	}
	v := toInt(ev.eval(node), node.Col())
	bits := unit * 8
	if a.final && (v.Cmp(neg(pow2(bits-1))) < 0 || v.Cmp(pow2(bits)) >= 0) {
		failCol(fmt.Sprintf("значение %s не помещается в %s: (%d бит)", v, name, bits), node.Col())
	}
	return pack(v, unit)
}

func (a *assembler) dataBytes(st *DataStmt, here *big.Int) []byte {
	ev := newEvaluator(a, here, "")
	if st.Kind == "list" {
		var out []byte
		for _, it := range st.Items {
			out = append(out, a.itemBytes(it, st.Unit, ev, st.Name)...)
		}
		return out
	}
	n, _ := a.evalInt(st.Count, here, false, "значение")
	if cmpInt(mul(n, bi(int64(st.Unit))), maxOutput) > 0 {
		failCol(fmt.Sprintf("слишком большой размер данных (%s)", n), st.Count.Col())
	}
	if st.Kind == "fill" {
		if n.Sign() < 0 {
			if a.final {
				failCol(fmt.Sprintf("%s-N: количество не может быть отрицательным (%s)", st.Name, n), st.Count.Col())
			}
			n = bi(0)
		}
		return make([]byte, small(n)*st.Unit)
	}
	// fixed: b/ N: значения
	if n.Sign() <= 0 {
		if a.final {
			failCol(fmt.Sprintf("%s/ N: N должно быть больше нуля (указано %s)", st.Name, n), st.Count.Col())
		}
		n = bi(0)
	}
	var data []byte
	for _, it := range st.Items {
		data = append(data, a.itemBytes(it, st.Unit, ev, st.Name)...)
	}
	size := small(n) * st.Unit
	if len(data) > size {
		if a.final {
			failCol(fmt.Sprintf("инициализатор (%d байт) не помещается в %s/ %s (%d байт)", len(data), st.Name, n, size),
				st.Count.Col())
		}
		data = data[:size]
	}
	return append(data, make([]byte, size-len(data))...)
}

// varValue — значение переменной для [имя] в do.
func (a *assembler) varValue(name string, ev *evaluator, node *Var) value {
	st := a.varDefs[name]
	if st == nil {
		if a.lookup(name) != nil {
			failCol(fmt.Sprintf("'%s' — не переменная с данными; адрес получается без скобок: %s", name, name), node.C)
		}
		if !a.final {
			ev.known = false
			return intValue(bi(0))
		}
		names := make([]string, 0, len(a.varDefs))
		for k := range a.varDefs {
			names = append(names, k)
		}
		failCol(fmt.Sprintf("неизвестная переменная '%s'%s", name, suggestName(name, names)), node.C)
	}
	if st.Kind == "fill" {
		return intValue(bi(0))
	}
	items := st.Items
	if len(items) == 0 {
		return strValue([]byte{})
	}
	sub := newEvaluator(a, ev.here, "")
	defer func() { ev.known = ev.known && sub.known }()
	hasStr := false
	for _, it := range items {
		if _, ok := it.(*Str); ok {
			hasStr = true
		}
	}
	if hasStr || (st.Unit == 1 && len(items) > 1) {
		var data []byte
		for _, it := range items {
			data = append(data, a.itemBytes(it, st.Unit, sub, st.Name)...)
		}
		if i := bytes.IndexByte(data, 0); i >= 0 {
			data = data[:i]
		}
		if data == nil {
			data = []byte{}
		}
		return strValue(data)
	}
	if len(items) == 1 {
		return intValue(toInt(sub.eval(items[0]), items[0].Col()))
	}
	failCol(fmt.Sprintf("переменная '%s' содержит несколько чисел — в do её значение "+
		"можно использовать, только если это строка или одно число", name), node.C)
	return value{}
}

// ------------------------------------------------ do

func (a *assembler) execDo(st *DoStmt, rep string, here *big.Int) {
	ev := newEvaluator(a, here, "do")
	v := ev.eval(st.Expr)
	dest := a.resolve(st.Dest, here)
	ctx := &passCtx{asm: a, flags: st.Flags, rep: rep + "|", at: here}
	if !v.isStr {
		a.emit(encode("mov", []any{dest, &Imm{Value: v.i, Known: ev.known}}, nil, ctx))
		return
	}
	// Строка: кладём её прямо в код (с обходом) и загружаем её адрес.
	text := append(append([]byte{}, v.s...), 0)
	addr, ok := a.doStrings[string(text)]
	var inline []byte
	if !ok {
		n := len(text)
		var jump []byte
		if n <= 127 {
			jump = []byte{0xEB, byte(n)}
		} else {
			size := 4
			if a.bits == 16 {
				size = 2
			}
			jump = cat([]byte{0xe9}, pack(bi(int64(n)), size))
		}
		addr = addInt(here, len(jump))
		a.doStrings[string(text)] = addr
		inline = cat(jump, text)
	}
	ctx.at = addInt(here, len(inline))
	code := encode("mov", []any{dest, &Imm{Value: addr, Known: true}}, nil, ctx)
	a.emit(cat(inline, code))
}

// ------------------------------------------------ листинг

func padRight(s string, n int) string {
	if k := runeLen(s); k < n {
		return s + strings.Repeat(" ", n-k)
	}
	return s
}

func (a *assembler) makeListing() string {
	type entry struct {
		addr *big.Int
		data []byte
	}
	grouped := map[*SourceLoc]*entry{}
	for _, r := range a.records {
		if e, ok := grouped[r.loc]; ok {
			e.data = append(e.data, r.data...)
		} else {
			grouped[r.loc] = &entry{r.addr, append([]byte(nil), r.data...)}
		}
	}
	var rows []string
	const width = 8
	files := map[string]bool{}
	for _, loc := range a.lines {
		files[loc.File] = true
	}
	multiFile := len(files) > 1
	for _, loc := range a.lines {
		addrS, hexS := "", ""
		if e, ok := grouped[loc]; ok {
			addrS = fmt.Sprintf("%08X", e.addr)
			n := min(len(e.data), width)
			hexS = strings.ToUpper(fmt.Sprintf("%x", e.data[:n]))
			if len(e.data) > width {
				hexS += fmt.Sprintf("+%d", len(e.data)-width)
			}
		}
		prefix := ""
		if multiFile {
			prefix = basename(loc.File) + ":"
		}
		rows = append(rows, fmt.Sprintf("%s%s %s  %s  %s", prefix, padRight(fmt.Sprint(loc.Line), 5),
			padRight(addrS, 8), padRight(hexS, 20), rstrip(loc.Text)))
	}
	if len(a.cur) > 0 {
		rows = append(rows, "", "Символы:")
		list := make([]Symbol, len(a.curOrder))
		for i, name := range a.curOrder {
			list[i] = Symbol{name, a.cur[name]}
		}
		for _, s := range sortedSymbols(list) {
			rows = append(rows, fmt.Sprintf("  %s  %s", FormatSymbol(s.Value), s.Name))
		}
	}
	return strings.Join(rows, "\n") + "\n"
}

// ---------------------------------------------------------------- API

func assembleParsed(p *Parser) (*Result, *Errors) {
	p.finish()
	if len(p.errors) > 0 {
		return nil, &Errors{p.errors}
	}
	res, errs := newAssembler(p).assemble()
	if errs != nil {
		return nil, errs
	}
	res.Warnings = p.warnings
	return res, nil
}

// SourceName — имя «файла» для текста, переданного без имени.
const SourceName = "<источник>"

// CompileSource компилирует текст программы. baseDir — папка для include ("" — текущая).
func CompileSource(text, filename, baseDir string) (*Result, *Errors) {
	if filename == "" {
		filename = SourceName
	}
	p := NewParser()
	p.ParseText(text, filename, baseDir)
	return assembleParsed(p)
}

// CompileFile компилирует файл.
func CompileFile(path string) (*Result, *Errors) {
	p := NewParser()
	if e := catch(func() { p.ParseFile(path, nil) }); e != nil {
		return nil, &Errors{[]*Error{e}}
	}
	return assembleParsed(p)
}
