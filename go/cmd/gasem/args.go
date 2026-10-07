package main

// Разбор аргументов командной строки так же, как это делает argparse в Python
// (та же грамматика, те же сообщения об ошибках), чтобы Go- и Python-версии
// gasem вели себя одинаково.

import (
	"fmt"
	"io"
	"regexp"
	"strings"
)

type action struct {
	opts  []string // "-o", "--output"; пусто у позиционных
	dest  string
	nargs string // "1" — одно значение, "0" — флаг, "*", "+"
	kind  string // store / true / append / help
}

type argParser struct {
	prog    string
	help    string // полный текст --help
	actions []*action
}

type argError struct{ msg string }

type parsedArgs struct {
	values map[string][]string
	flags  map[string]bool
}

func (a *parsedArgs) str(dest string) string {
	if v := a.values[dest]; len(v) > 0 {
		return v[len(v)-1]
	}
	return ""
}

func (a *parsedArgs) list(dest string) []string { return a.values[dest] }

func (p *argParser) usage() string {
	i := strings.Index(p.help, "\n\n")
	return p.help[:i+1]
}

func (p *argParser) optionMap() ([]string, map[string]*action) {
	var order []string
	m := map[string]*action{}
	for _, a := range p.actions {
		for _, o := range a.opts {
			order = append(order, o)
			m[o] = a
		}
	}
	return order, m
}

func (p *argParser) positionals() []*action {
	var out []*action
	for _, a := range p.actions {
		if len(a.opts) == 0 {
			out = append(out, a)
		}
	}
	return out
}

func actionName(a *action) string {
	if len(a.opts) > 0 {
		return strings.Join(a.opts, "/")
	}
	return a.dest
}

func nargsPattern(a *action) string {
	var pat string
	switch a.nargs {
	case "1":
		pat = "(-*A-*)"
	case "0":
		pat = "(-*-*)"
	case "*":
		pat = "(-*[A-]*)"
	case "+":
		pat = "(-*A[A-]*)"
	}
	if len(a.opts) > 0 {
		pat = strings.ReplaceAll(pat, "-*", "")
		pat = strings.ReplaceAll(pat, "-", "")
	}
	return pat
}

var negativeNumber = regexp.MustCompile(`^-\d+$|^-\d*\.\d+$`)

type optTuple struct {
	a        *action
	opt      string
	sep      *string
	explicit *string
}

func sp(s string) *string { return &s }

func (p *argParser) parseOptional(arg string) []optTuple {
	if arg == "" || arg[0] != '-' {
		return nil
	}
	order, m := p.optionMap()
	if a, ok := m[arg]; ok {
		return []optTuple{{a, arg, nil, nil}}
	}
	if len([]rune(arg)) == 1 {
		return nil
	}
	if i := strings.Index(arg, "="); i >= 0 {
		if a, ok := m[arg[:i]]; ok {
			return []optTuple{{a, arg[:i], sp("="), sp(arg[i+1:])}}
		}
	}
	var result []optTuple
	prefix, sep, explicit := arg, (*string)(nil), (*string)(nil)
	if i := strings.Index(arg, "="); i >= 0 {
		prefix, sep, explicit = arg[:i], sp("="), sp(arg[i+1:])
	}
	if strings.HasPrefix(arg, "--") {
		for _, o := range order {
			if strings.HasPrefix(o, prefix) {
				result = append(result, optTuple{m[o], o, sep, explicit})
			}
		}
	} else {
		r := []rune(arg)
		short, shortExplicit := string(r[:2]), string(r[2:])
		for _, o := range order {
			if o == short {
				result = append(result, optTuple{m[o], o, sp(""), sp(shortExplicit)})
			} else if strings.HasPrefix(o, prefix) {
				result = append(result, optTuple{m[o], o, sep, explicit})
			}
		}
	}
	if len(result) > 0 {
		return result
	}
	if negativeNumber.MatchString(arg) {
		return nil
	}
	if strings.Contains(arg, " ") {
		return nil
	}
	return []optTuple{{nil, arg, nil, nil}}
}

func matchLen(pattern, s string) (int, bool) {
	m := regexp.MustCompile("^" + pattern).FindStringSubmatch(s)
	if m == nil {
		return 0, false
	}
	return len(m[1]), true
}

// parse разбирает аргументы. done=true — программа должна завершиться с кодом code.
func (p *argParser) parse(argv []string, stdout, stderr io.Writer) (res *parsedArgs, code int, done bool) {
	res = &parsedArgs{values: map[string][]string{}, flags: map[string]bool{}}
	seen := map[*action]bool{}
	defer func() {
		if r := recover(); r != nil {
			e, ok := r.(argError)
			if !ok {
				panic(r)
			}
			fmt.Fprint(stderr, p.usage())
			fmt.Fprintf(stderr, "%s: error: %s\n", p.prog, e.msg)
			res, code, done = nil, 2, true
		}
	}()
	type helpExit struct{}
	defer func() {
		if r := recover(); r != nil {
			if _, ok := r.(helpExit); !ok {
				panic(r)
			}
			fmt.Fprint(stdout, p.help)
			res, code, done = nil, 0, true
		}
	}()

	// O — параметр, A — значение, '-' — первый '--'
	optionAt := map[int][]optTuple{}
	var pattern strings.Builder
	dashes := false
	for i, arg := range argv {
		switch {
		case dashes:
			pattern.WriteByte('A')
		case arg == "--":
			pattern.WriteByte('-')
			dashes = true
		default:
			if t := p.parseOptional(arg); t != nil {
				optionAt[i] = t
				pattern.WriteByte('O')
			} else {
				pattern.WriteByte('A')
			}
		}
	}
	pat := pattern.String()

	take := func(a *action, args []string) {
		seen[a] = true
		switch a.kind {
		case "help":
			panic(helpExit{})
		case "true":
			res.flags[a.dest] = true
		case "append", "store":
			if a.kind == "store" && len(a.opts) == 0 {
				res.values[a.dest] = append([]string{}, args...)
			} else {
				res.values[a.dest] = append(res.values[a.dest], args...)
			}
		}
	}
	argErr := func(a *action, msg string) {
		if a != nil {
			msg = "argument " + actionName(a) + ": " + msg
		}
		panic(argError{msg})
	}

	var extras []string
	consumeOptional := func(start int) int {
		tuples := optionAt[start]
		if len(tuples) > 1 {
			var names []string
			for _, t := range tuples {
				names = append(names, t.opt)
			}
			argErr(nil, fmt.Sprintf("ambiguous option: %s could match %s", argv[start], strings.Join(names, ", ")))
		}
		t := tuples[0]
		a, opt, sep, explicit := t.a, t.opt, t.sep, t.explicit
		type pending struct {
			a    *action
			args []string
		}
		var acts []pending
		var stop int
		for {
			if a == nil {
				extras = append(extras, argv[start])
				return start + 1
			}
			if explicit != nil {
				count, _ := matchLen(nargsPattern(a), "A")
				if count == 0 && opt[1] != '-' && *explicit != "" {
					if (sep != nil && *sep != "") || (*explicit)[0] == '-' {
						argErr(a, "ignored explicit argument "+pyRepr(*explicit))
					}
					acts = append(acts, pending{a, nil})
					ex := []rune(*explicit)
					opt = "-" + string(ex[0])
					_, m := p.optionMap()
					if next, ok := m[opt]; ok {
						a = next
						rest := string(ex[1:])
						switch {
						case rest == "":
							sep, explicit = nil, nil
						case rest[0] == '=':
							sep, explicit = sp("="), sp(rest[1:])
						default:
							sep, explicit = sp(""), sp(rest)
						}
					} else {
						extras = append(extras, "-"+*explicit)
						stop = start + 1
						break
					}
				} else if count == 1 {
					stop = start + 1
					acts = append(acts, pending{a, []string{*explicit}})
					break
				} else {
					argErr(a, "ignored explicit argument "+pyRepr(*explicit))
				}
			} else {
				s := start + 1
				count, ok := matchLen(nargsPattern(a), pat[s:])
				if !ok {
					argErr(a, "expected one argument")
				}
				stop = s + count
				acts = append(acts, pending{a, argv[s:stop]})
				break
			}
		}
		for _, x := range acts {
			take(x.a, x.args)
		}
		return stop
	}

	positionals := p.positionals()
	consumePositionals := func(start int) int {
		sel := pat[start:]
		var counts []int
		for i := len(positionals); i > 0; i-- {
			var b strings.Builder
			b.WriteString("^")
			for _, a := range positionals[:i] {
				b.WriteString(nargsPattern(a))
			}
			re := regexp.MustCompile(b.String())
			m := re.FindStringSubmatchIndex(sel)
			if m == nil {
				continue
			}
			for g := 1; g <= i; g++ {
				counts = append(counts, m[2*g+1]-m[2*g])
			}
			if m[1] < len(sel) && sel[m[1]] == 'O' {
				for len(counts) > 0 && counts[len(counts)-1] == 0 {
					counts = counts[:len(counts)-1]
				}
			}
			break
		}
		for k, count := range counts {
			a := positionals[k]
			args := append([]string{}, argv[start:start+count]...)
			if strings.Contains(pat[start:start+count], "-") {
				for j, s := range args {
					if s == "--" {
						args = append(args[:j], args[j+1:]...)
						break
					}
				}
			}
			start += count
			take(a, args)
		}
		positionals = positionals[len(counts):]
		return start
	}

	maxOpt := -1
	for i := range optionAt {
		if i > maxOpt {
			maxOpt = i
		}
	}
	start := 0
	for start <= maxOpt {
		next := start
		for next <= maxOpt {
			if _, ok := optionAt[next]; ok {
				break
			}
			next++
		}
		if start != next {
			end := consumePositionals(start)
			if end > start {
				start = end
				continue
			}
			start = end
		}
		if _, ok := optionAt[start]; !ok {
			extras = append(extras, argv[start:next]...)
			start = next
		}
		start = consumeOptional(start)
	}
	stop := consumePositionals(start)
	extras = append(extras, argv[stop:]...)

	var required []string
	for _, a := range p.actions {
		if !seen[a] && len(a.opts) == 0 && a.nargs != "*" {
			required = append(required, actionName(a))
		}
	}
	if len(required) > 0 {
		argErr(nil, "the following arguments are required: "+strings.Join(required, ", "))
	}
	if len(extras) > 0 {
		argErr(nil, "unrecognized arguments: "+strings.Join(extras, " "))
	}
	return res, 0, false
}

// pyRepr — строка в кавычках, как repr() в Python (для сообщений об ошибках).
func pyRepr(s string) string {
	q := "'"
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		q = `"`
	}
	r := strings.ReplaceAll(s, `\`, `\\`)
	if q == "'" {
		r = strings.ReplaceAll(r, "'", `\'`)
	}
	return q + r + q
}
