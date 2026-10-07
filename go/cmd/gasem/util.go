package main

import (
	"math/big"
	"unicode/utf8"
)

func bigInt(v int64) *big.Int { return big.NewInt(v) }

func utf8Valid(b []byte) bool { return utf8.Valid(b) }
