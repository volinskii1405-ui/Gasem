//go:build !windows

package gasem

import (
	"os/exec"
	"syscall"
)

// detach запускает процесс в своей группе: Ctrl+C в терминале получает GDB, а не QEMU.
func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}
