"""Filesystem helpers. Everything here is read-only."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import struct
import sys
from dataclasses import dataclass
from datetime import datetime

CHUNK = 1024 * 1024
PARTIAL = 64 * 1024


@dataclass
class Size:
    allocated: int = 0
    reclaimable: int = 0
    files: int = 0
    errors: int = 0


def measure(path: str) -> Size:
    """Allocated bytes under path, plus bytes actually freed if path were deleted.

    Hardlinked files count once, and only toward `reclaimable` when every link
    lives inside `path` (e.g. pnpm node_modules linking into a global store free ~0).
    APFS clones are not detected here (see private_size for per-file checks).
    """
    size = Size()
    shared: dict[tuple[int, int], list[int]] = {}

    def account(st: os.stat_result) -> None:
        blocks = st.st_blocks * 512
        if stat.S_ISDIR(st.st_mode) or st.st_nlink <= 1:
            size.allocated += blocks
            size.reclaimable += blocks
            if not stat.S_ISDIR(st.st_mode):
                size.files += 1
            return
        size.files += 1
        key = (st.st_dev, st.st_ino)
        rec = shared.get(key)
        if rec is None:
            shared[key] = [1, st.st_nlink, blocks]
            size.allocated += blocks
        else:
            rec[0] += 1

    try:
        top = os.lstat(path)
    except OSError:
        size.errors += 1
        return size
    account(top)
    if stat.S_ISDIR(top.st_mode):
        stack = [path]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            size.errors += 1
                            continue
                        account(st)
                        if stat.S_ISDIR(st.st_mode):
                            stack.append(e.path)
            except OSError:
                size.errors += 1
    for seen, nlink, blocks in shared.values():
        if seen >= nlink:
            size.reclaimable += blocks
    return size


def file_hash(path: str) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def partial_hash(path: str, size: int) -> str | None:
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(PARTIAL))
            if size > 2 * PARTIAL:
                f.seek(size - PARTIAL)
                h.update(f.read(PARTIAL))
    except OSError:
        return None
    return h.hexdigest()


# --- APFS clone awareness -------------------------------------------------


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


_ATTR_CMNEXT_PRIVATESIZE = 0x00000008
_FSOPT_NOFOLLOW = 0x00000001
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020
_getattrlist = None


def private_size(path: str) -> int | None:
    """Bytes owned only by this file (excludes blocks shared with APFS clones).

    Returns None when unavailable (non-macOS or the call fails).
    """
    global _getattrlist
    if sys.platform != "darwin":
        return None
    if _getattrlist is None:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.getattrlist
        fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
        fn.restype = ctypes.c_int
        _getattrlist = fn
    al = _AttrList(5, 0, 0, 0, 0, 0, _ATTR_CMNEXT_PRIVATESIZE)
    buf = ctypes.create_string_buffer(64)
    rc = _getattrlist(os.fsencode(path), ctypes.byref(al), buf, len(buf), _FSOPT_NOFOLLOW | _FSOPT_ATTR_CMN_EXTENDED)
    if rc != 0:
        return None
    length = struct.unpack_from("<I", buf.raw, 0)[0]
    if length < 12:
        return None
    return struct.unpack_from("<q", buf.raw, 4)[0]


# --- formatting -------------------------------------------------------------


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(text: str) -> int:
    t = text.strip().upper().removesuffix("B").removesuffix("I")
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if t and t[-1] in mult:
        return int(float(t[:-1]) * mult[t[-1]])
    return int(float(t))


def fmt_ts(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def is_within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)
