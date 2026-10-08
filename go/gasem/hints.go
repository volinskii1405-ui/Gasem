package gasem

// Подсказки «может быть, вы имели в виду…» для опечаток.

import (
	"sort"
	"strings"
)

var directiveWords = []string{"og", "align", "include", "incbin", "incprog", "do", "pool", "args", "equ",
	"if", "elif", "else", "end", "while", "for", "repeat", "until",
	"break", "continue", "let", "macro", "proc", "struct", "at", "local", "return"}

// CommandWords — все слова, которые могут стоять на месте команды, по алфавиту.
var CommandWords = func() []string {
	set := map[string]bool{}
	for k := range Mnemonics {
		set[k] = true
	}
	for k := range Aliases {
		set[k] = true
	}
	for k := range cc {
		set["jf"+k] = true
	}
	for k := range prefixBytes {
		set[k] = true
	}
	for _, k := range directiveWords {
		set[k] = true
	}
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}()

// distance — число правок (вставка, удаление, замена, перестановка соседних букв).
func distance(as, bs string) int {
	a, b := []rune(as), []rune(bs)
	var prev2 []int
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 0
			if a[i-1] != b[j-1] {
				cost = 1
			}
			cur[j] = min(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
			if i > 1 && j > 1 && a[i-1] == b[j-2] && a[i-2] == b[j-1] {
				cur[j] = min(cur[j], prev2[j-2]+1)
			}
		}
		prev2, prev = prev, cur
	}
	return prev[len(b)]
}

var keyRows = []string{"1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"}

type keyPos struct{ r, c int }

var keyPositions = func() map[rune]keyPos {
	m := map[rune]keyPos{}
	for r, row := range keyRows {
		for c, ch := range row {
			m[ch] = keyPos{r, c}
		}
	}
	return m
}()

func absInt(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// neighbours — сколько замен приходится на соседние клавиши (ebz → ebx правдоподобнее, чем ebp).
func neighbours(as, bs string) int {
	a, b := []rune(as), []rune(bs)
	if len(a) != len(b) {
		return 0
	}
	n := 0
	for i := range a {
		x, y := a[i], b[i]
		p1, ok1 := keyPositions[x]
		p2, ok2 := keyPositions[y]
		if x != y && ok1 && ok2 && absInt(p1.r-p2.r) <= 1 && absInt(p1.c-p2.c) <= 1 {
			n++
		}
	}
	return n
}

// closest — самое похожее слово из candidates ("" — если такого нет).
func closest(word string, candidates []string) string {
	low := lower(word)
	limit := 2
	if runeLen(low) <= 4 {
		limit = 1
	}
	type key struct {
		d, dl, nb int
		c         string
	}
	less := func(x, y key) bool {
		if x.d != y.d {
			return x.d < y.d
		}
		if x.dl != y.dl {
			return x.dl < y.dl
		}
		if x.nb != y.nb {
			return x.nb < y.nb
		}
		return x.c < y.c
	}
	var best *key
	for _, c := range candidates {
		if c == word {
			continue
		}
		d := distance(low, lower(c))
		if d > limit {
			continue
		}
		// ближе; той же длины; опечатка на соседней клавише
		k := key{d, absInt(runeLen(c) - runeLen(word)), -neighbours(low, lower(c)), c}
		if best == nil || less(k, *best) {
			best = &k
		}
	}
	if best == nil {
		return ""
	}
	return best.c
}

// suggestName — подсказка для неизвестного имени: похожая метка или регистр.
func suggestName(name string, symbols []string) string {
	var cands []string
	for _, s := range symbols {
		if !strings.HasPrefix(s, "@") {
			cands = append(cands, s)
		}
	}
	if found := closest(name, cands); found != "" {
		return " — может быть, '" + found + "'?"
	}
	regs := make([]string, 0, len(Registers))
	for k := range Registers {
		regs = append(regs, k)
	}
	if reg := closest(name, regs); reg != "" {
		return " — может быть, регистр '" + reg + "'?"
	}
	return ""
}

// suggestCommand — похожая команда: перестановка, лишняя или пропущенная буква (mvo → mov).
func suggestCommand(word string) string {
	var cands []string
	n := runeLen(word)
	for _, c := range CommandWords {
		if absInt(runeLen(c)-n) <= 1 {
			cands = append(cands, c)
		}
	}
	if found := closest(word, cands); found != "" {
		return " — может быть, '" + found + "'?"
	}
	return ""
}
