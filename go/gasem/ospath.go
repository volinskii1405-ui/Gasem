package gasem

import "runtime"

var pathSeparators = func() string {
	if runtime.GOOS == "windows" {
		return `/\`
	}
	return "/"
}()
