package gasem

// Числа Gasem, как и в Python, не ограничены по размеру: выражение может
// дать сколь угодно большое значение, а проверка «помещается ли» делается
// при записи в команду или данные.

import "math/big"

func bi(v int64) *big.Int { return big.NewInt(v) }

func add(a, b *big.Int) *big.Int { return new(big.Int).Add(a, b) }
func sub(a, b *big.Int) *big.Int { return new(big.Int).Sub(a, b) }
func mul(a, b *big.Int) *big.Int { return new(big.Int).Mul(a, b) }
func neg(a *big.Int) *big.Int    { return new(big.Int).Neg(a) }
func bnot(a *big.Int) *big.Int   { return new(big.Int).Not(a) }
func band(a, b *big.Int) *big.Int {
	return new(big.Int).And(a, b)
}
func bor(a, b *big.Int) *big.Int  { return new(big.Int).Or(a, b) }
func bxor(a, b *big.Int) *big.Int { return new(big.Int).Xor(a, b) }
func addInt(a *big.Int, n int) *big.Int {
	return new(big.Int).Add(a, big.NewInt(int64(n)))
}

// cmpInt сравнивает большое число с обычным.
func cmpInt(a *big.Int, v int64) int {
	if a.IsInt64() {
		x := a.Int64()
		switch {
		case x < v:
			return -1
		case x > v:
			return 1
		}
		return 0
	}
	return a.Sign()
}

func eqInt(a *big.Int, v int64) bool { return cmpInt(a, v) == 0 }

// inRange: lo <= v <= hi.
func inRange(v *big.Int, lo, hi int64) bool { return cmpInt(v, lo) >= 0 && cmpInt(v, hi) <= 0 }

// Степени двойки и маски для размеров до 64 бит считаются один раз
// (их нельзя изменять: результаты pow2 и маски общие).
var pow2s, masks = func() ([]*big.Int, []*big.Int) {
	p, m := make([]*big.Int, 65), make([]*big.Int, 65)
	for n := range p {
		p[n] = new(big.Int).Lsh(big.NewInt(1), uint(n))
		m[n] = new(big.Int).Sub(p[n], big.NewInt(1))
	}
	return p, m
}()

// pow2 = 2**n. Результат нельзя изменять.
func pow2(n int) *big.Int {
	if n < len(pow2s) {
		return pow2s[n]
	}
	return new(big.Int).Lsh(big.NewInt(1), uint(n))
}

// mask = v & (2**bits - 1).
func mask(v *big.Int, bits int) *big.Int {
	if bits < len(masks) {
		if v.Sign() >= 0 && v.BitLen() <= bits {
			return v
		}
		return new(big.Int).And(v, masks[bits])
	}
	return new(big.Int).And(v, new(big.Int).Sub(pow2(bits), big.NewInt(1)))
}

// pack — младшие n байт числа (дополнительный код), little-endian.
func pack(v *big.Int, n int) []byte {
	m := mask(v, 8*n)
	out := make([]byte, n)
	be := m.FillBytes(make([]byte, n))
	for i := 0; i < n; i++ {
		out[i] = be[n-1-i]
	}
	return out
}

// lowByte = v & 0xFF.
func lowByte(v *big.Int) byte { return byte(mask(v, 8).Uint64()) }

// small — значение, о котором уже известно, что оно умещается в int.
func small(v *big.Int) int {
	if v.IsInt64() {
		return int(v.Int64())
	}
	if v.Sign() < 0 {
		return -1 << 62
	}
	return 1 << 62
}

// fitsS8 — помещается ли значение в знаковый байт (с учётом размера операнда).
func fitsS8(v *big.Int, size int) bool {
	m := mask(v, size)
	return cmpInt(m, 0x80) < 0 || m.Cmp(sub(pow2(size), bi(0x80))) >= 0
}

// fromLittle — int.from_bytes(b, "little").
func fromLittle(b []byte) *big.Int {
	be := make([]byte, len(b))
	for i := range b {
		be[len(b)-1-i] = b[i]
	}
	return new(big.Int).SetBytes(be)
}

// truncDiv — деление с отбрасыванием дробной части (как в Gasem: -7/2 = -3).
func truncDiv(a, b *big.Int) *big.Int { return new(big.Int).Quo(a, b) }
func truncMod(a, b *big.Int) *big.Int { return new(big.Int).Rem(a, b) }
