"""Exercise Linux kernel limits in a child, without changing the test runner."""

import subprocess
import sys

import pytest


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Requires Linux mmap/RLIMIT_DATA enforcement")
def test_private_writable_limit_allows_sparse_browser_reservation():
    program = r'''
import ctypes, errno, mmap
from iatest.execution.worker import apply_resource_limits
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.mmap.restype = ctypes.c_void_p
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
apply_resource_limits({"mode": "process", "memory_mb": 128})
flags = mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS
failed = ctypes.c_void_p(-1).value
reservation = libc.mmap(None, 4 * 1024**3, 0, flags, -1, 0)
assert reservation != failed, "Sparse PROT_NONE reservation must remain available"
try:
    writable = libc.mmap(None, 256 * 1024**2, mmap.PROT_READ | mmap.PROT_WRITE, flags, -1, 0)
    assert writable == failed and ctypes.get_errno() == errno.ENOMEM, "Writable allocation must remain kernel-limited"
finally:
    assert libc.munmap(reservation, 4 * 1024**3) == 0
print("sparse reservation allowed; private writable allocation rejected")
'''
    result = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True, timeout=10)
    assert "private writable allocation rejected" in result.stdout
