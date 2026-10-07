package gasem

// Унифицированный diff, как difflib.unified_diff в Python (для gasem fmt --diff).

import (
	"fmt"
	"sort"
)

type match struct{ a, b, size int }

type opcode struct {
	tag            byte // 'r' replace, 'd' delete, 'i' insert, 'e' equal
	i1, i2, j1, j2 int
}

type sequenceMatcher struct {
	a, b []string
	b2j  map[string][]int
}

func newSequenceMatcher(a, b []string) *sequenceMatcher {
	m := &sequenceMatcher{a: a, b: b, b2j: map[string][]int{}}
	for i, elt := range b {
		m.b2j[elt] = append(m.b2j[elt], i)
	}
	// «популярные» строки (больше 1% длинного текста) не ищутся — как autojunk в difflib
	if n := len(b); n >= 200 {
		ntest := n/100 + 1
		for elt, idxs := range m.b2j {
			if len(idxs) > ntest {
				delete(m.b2j, elt)
			}
		}
	}
	return m
}

func (m *sequenceMatcher) findLongestMatch(alo, ahi, blo, bhi int) match {
	a, b := m.a, m.b
	besti, bestj, bestsize := alo, blo, 0
	j2len := map[int]int{}
	for i := alo; i < ahi; i++ {
		newj2len := map[int]int{}
		for _, j := range m.b2j[a[i]] {
			if j < blo {
				continue
			}
			if j >= bhi {
				break
			}
			k := j2len[j-1] + 1
			newj2len[j] = k
			if k > bestsize {
				besti, bestj, bestsize = i-k+1, j-k+1, k
			}
		}
		j2len = newj2len
	}
	for besti > alo && bestj > blo && a[besti-1] == b[bestj-1] {
		besti, bestj, bestsize = besti-1, bestj-1, bestsize+1
	}
	for besti+bestsize < ahi && bestj+bestsize < bhi && a[besti+bestsize] == b[bestj+bestsize] {
		bestsize++
	}
	return match{besti, bestj, bestsize}
}

func (m *sequenceMatcher) matchingBlocks() []match {
	la, lb := len(m.a), len(m.b)
	queue := [][4]int{{0, la, 0, lb}}
	var blocks []match
	for len(queue) > 0 {
		q := queue[len(queue)-1]
		queue = queue[:len(queue)-1]
		alo, ahi, blo, bhi := q[0], q[1], q[2], q[3]
		x := m.findLongestMatch(alo, ahi, blo, bhi)
		i, j, k := x.a, x.b, x.size
		if k > 0 {
			blocks = append(blocks, x)
			if alo < i && blo < j {
				queue = append(queue, [4]int{alo, i, blo, j})
			}
			if i+k < ahi && j+k < bhi {
				queue = append(queue, [4]int{i + k, ahi, j + k, bhi})
			}
		}
	}
	sort.Slice(blocks, func(x, y int) bool {
		if blocks[x].a != blocks[y].a {
			return blocks[x].a < blocks[y].a
		}
		if blocks[x].b != blocks[y].b {
			return blocks[x].b < blocks[y].b
		}
		return blocks[x].size < blocks[y].size
	})
	i1, j1, k1 := 0, 0, 0
	var out []match
	for _, x := range blocks {
		if i1+k1 == x.a && j1+k1 == x.b {
			k1 += x.size
		} else {
			if k1 > 0 {
				out = append(out, match{i1, j1, k1})
			}
			i1, j1, k1 = x.a, x.b, x.size
		}
	}
	if k1 > 0 {
		out = append(out, match{i1, j1, k1})
	}
	return append(out, match{la, lb, 0})
}

func (m *sequenceMatcher) opcodes() []opcode {
	i, j := 0, 0
	var answer []opcode
	for _, x := range m.matchingBlocks() {
		var tag byte
		switch {
		case i < x.a && j < x.b:
			tag = 'r'
		case i < x.a:
			tag = 'd'
		case j < x.b:
			tag = 'i'
		}
		if tag != 0 {
			answer = append(answer, opcode{tag, i, x.a, j, x.b})
		}
		i, j = x.a+x.size, x.b+x.size
		if x.size > 0 {
			answer = append(answer, opcode{'e', x.a, i, x.b, j})
		}
	}
	return answer
}

func (m *sequenceMatcher) groupedOpcodes(n int) [][]opcode {
	codes := m.opcodes()
	if len(codes) == 0 {
		codes = []opcode{{'e', 0, 1, 0, 1}}
	}
	if c := &codes[0]; c.tag == 'e' {
		c.i1, c.j1 = max(c.i1, c.i2-n), max(c.j1, c.j2-n)
	}
	if c := &codes[len(codes)-1]; c.tag == 'e' {
		c.i2, c.j2 = min(c.i2, c.i1+n), min(c.j2, c.j1+n)
	}
	nn := n + n
	var groups [][]opcode
	var group []opcode
	for _, c := range codes {
		if c.tag == 'e' && c.i2-c.i1 > nn {
			group = append(group, opcode{'e', c.i1, min(c.i2, c.i1+n), c.j1, min(c.j2, c.j1+n)})
			groups = append(groups, group)
			group = nil
			c.i1, c.j1 = max(c.i1, c.i2-n), max(c.j1, c.j2-n)
		}
		group = append(group, c)
	}
	if len(group) > 0 && !(len(group) == 1 && group[0].tag == 'e') {
		groups = append(groups, group)
	}
	return groups
}

func formatRange(start, stop int) string {
	beginning := start + 1
	length := stop - start
	if length == 1 {
		return fmt.Sprint(beginning)
	}
	if length == 0 {
		beginning--
	}
	return fmt.Sprintf("%d,%d", beginning, length)
}

// UnifiedDiff — строки (с концами строк) унифицированного diff.
func UnifiedDiff(a, b []string, fromFile, toFile string) []string {
	var out []string
	for gi, group := range newSequenceMatcher(a, b).groupedOpcodes(3) {
		if gi == 0 {
			out = append(out, "--- "+fromFile+"\n", "+++ "+toFile+"\n")
		}
		first, last := group[0], group[len(group)-1]
		out = append(out, fmt.Sprintf("@@ -%s +%s @@\n", formatRange(first.i1, last.i2), formatRange(first.j1, last.j2)))
		for _, c := range group {
			if c.tag == 'e' {
				for _, line := range a[c.i1:c.i2] {
					out = append(out, " "+line)
				}
				continue
			}
			if c.tag == 'r' || c.tag == 'd' {
				for _, line := range a[c.i1:c.i2] {
					out = append(out, "-"+line)
				}
			}
			if c.tag == 'r' || c.tag == 'i' {
				for _, line := range b[c.j1:c.j2] {
					out = append(out, "+"+line)
				}
			}
		}
	}
	return out
}

// SplitLinesKeep — str.splitlines(True).
func SplitLinesKeep(s string) []string { return splitLinesKeep(s) }

// UniversalNewlines — \r\n и \r → \n, как при чтении файла в Python.
func UniversalNewlines(s string) string { return universalNewlines(s) }
