"""Unprivileged Linux Landlock write confinement for simulation workers."""
import ctypes
import os
from pathlib import Path
import sys


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def restrict_writes(root):
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("隔离执行需要 Linux x86_64 和 Landlock ABI >= 3；拒绝无保护运行")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError(f"Landlock ABI={abi} 不支持写保护；拒绝运行")
    # WRITE_FILE, REMOVE_*, MAKE_*, REFER, TRUNCATE. Reads/execution remain available.
    writes = (1 << 1) | sum(1 << bit for bit in range(4, 15))
    policy = Ruleset(writes, 0)
    fd = libc.syscall(444, ctypes.byref(policy), ctypes.sizeof(policy), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for path, access in [(Path(root).resolve(), writes), (Path("/dev/null"), (1 << 1)),
                             (Path("/dev/tty"), (1 << 1))]:
            if not path.exists():
                continue
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, pfd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "landlock_add_rule")
            finally:
                os.close(pfd)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve(strict=True)
    restrict_writes(root)
    os.execv(sys.argv[2], sys.argv[2:])
