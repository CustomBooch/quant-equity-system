"""Rust kernels for the Python pipeline (rust_core built as a C-ABI library, loaded with ctypes; no third-party crates, no Python build dependency).

Every kernel has a Python reference and must give bit-identical results (lab/kernels/tests). Selection, by the environment variable QUANT_KERNELS:
  auto   (default) use Rust when a library built from the current rust_core sources is installed, otherwise the Python reference (one warning);
  rust   require the library (error if it is missing or stale);
  python always use the Python reference.

    tools/py -m quant.core.kernels build     # cargo build --release, install the DLL and a stamp of the sources it was built from
    tools/py -m quant.core.kernels status    # which implementation is active and why

The library lives outside the repository (QUANT_RUST_CORE_DLL, default %LOCALAPPDATA%/quant/rust_core/rust_core.dll) next to rust_core.stamp.json.
A library whose stamp does not match the current sources, or whose ABI version differs from ABI_VERSION, is never used.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.core.kernels import reference
from quant.core.paths import REPO

CRATE = REPO / "rust_core"
ABI_VERSION = 4
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "quant" / "rust_core"
log = logging.getLogger("quant.core.kernels")


def library_path() -> Path:
    return Path(os.environ.get("QUANT_RUST_CORE_DLL", DEFAULT_DIR / "rust_core.dll"))


def source_hash() -> str:
    """SHA-256 over Cargo.toml, Cargo.lock and src/**/*.rs (paths and bytes), so any source change makes an installed library stale."""
    h = hashlib.sha256()
    for p in sorted([CRATE / "Cargo.toml", CRATE / "Cargo.lock", *(CRATE / "src").rglob("*.rs")]):
        h.update(p.relative_to(CRATE).as_posix().encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


_LIB: ctypes.CDLL | None = None
_STATE: dict[str, Any] = {}


def _load() -> ctypes.CDLL | None:
    global _LIB
    if _STATE:
        return _LIB
    path, stamp = library_path(), library_path().with_name("rust_core.stamp.json")
    why = None
    if not path.exists() or not stamp.exists():
        why = f"no library at {path}"
    elif json.loads(stamp.read_text()).get("source_sha256") != source_hash():
        why = f"library at {path} was built from other rust_core sources (stale)"
    else:
        lib = ctypes.CDLL(str(path))
        lib.rc_abi_version.restype = ctypes.c_uint32
        if lib.rc_abi_version() != ABI_VERSION:
            why = f"library ABI {lib.rc_abi_version()} != {ABI_VERSION}"
        else:
            f32p, u32p = np.ctypeslib.ndpointer(np.float32, flags="C_CONTIGUOUS"), np.ctypeslib.ndpointer(np.uint32, flags="C_CONTIGUOUS")
            lib.rc_design_crank.argtypes = [f32p, ctypes.c_size_t, ctypes.c_size_t, u32p, f32p, ctypes.c_size_t]
            lib.rc_design_crank.restype = ctypes.c_int32
            f64p = np.ctypeslib.ndpointer(np.float64, flags="C_CONTIGUOUS")
            lib.rc_rolling.argtypes = [f64p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32, f64p, ctypes.c_size_t]
            lib.rc_rolling.restype = ctypes.c_int32
            szp = ctypes.POINTER(ctypes.c_size_t)
            lib.rc_xlsx_sheet.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, szp]
            lib.rc_xlsx_shared.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, szp, szp]
            lib.rc_xlsx_sheet.restype = lib.rc_xlsx_shared.restype = ctypes.c_int32
            lib.rc_paper_check.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, szp]
            lib.rc_paper_check.restype = ctypes.c_int32
            _LIB = lib
    _STATE.update(path=str(path), reason=why, active="rust" if _LIB else "python")
    return _LIB


def backend() -> str:
    """'rust' or 'python' for this process, honouring QUANT_KERNELS."""
    mode = os.environ.get("QUANT_KERNELS", "auto").lower()
    if mode == "python":
        return "python"
    lib = _load()
    if lib is None:
        if mode == "rust":
            raise RuntimeError(f"QUANT_KERNELS=rust but {_STATE['reason']}; run: tools/py -m quant.core.kernels build")
        if not _STATE.get("warned"):
            log.warning("Rust kernels not used (%s); Python reference in use. Build with: tools/py -m quant.core.kernels build", _STATE["reason"])
            _STATE["warned"] = True
        return "python"
    return "rust"


def threads() -> int:
    return int(os.environ.get("QUANT_KERNEL_THREADS", max(1, min(10, (os.cpu_count() or 2) // 2))))


# ---------------------------------------------------------------- kernels

def _loaded() -> ctypes.CDLL:
    """The library that backend() loaded; every kernel below calls it only after backend() said rust."""
    return cast(ctypes.CDLL, _LIB)


def design_all(d: pd.DataFrame, cols: list[str]) -> np.ndarray:
    """Same result, bit for bit, as quant.core.kernels.reference.design_all (within-month centred ranks, missing as 0, then a missing flag for every column)."""
    if backend() == "python" or d["month"].isna().any() or len(d) == 0 or len(d) >= 2 ** 32:
        return reference.design_all(d, cols)
    n, k = len(d), len(cols)
    x = np.empty((k, n), np.float32)                                  # column-major input: one row of x per column of the frame
    for j, c in enumerate(cols):
        x[j] = d[c].astype("float32").to_numpy()                      # the reference's cast, column by column
    group = pd.factorize(d["month"], sort=True)[0].astype(np.uint32)
    out = np.empty((n, 2 * k), np.float32)
    rc = _loaded().rc_design_crank(np.ascontiguousarray(x), n, k, group, out, threads())
    if rc != 0:
        raise RuntimeError(f"rc_design_crank failed with code {rc}")
    return out


_HOW = {"sum": 0, "mean": 1, "max": 2}


def rolling(x: pd.DataFrame | pd.Series, window: int, min_periods: int | None = None, how: str = "sum") -> Any:
    """Same result, bit for bit, as x.rolling(window, min_periods=min_periods).<how>() for how in sum, mean, max (fixed window, not centred)."""
    minp = window if min_periods is None else min_periods
    if backend() == "python" or len(x) == 0 or minp > window:
        return getattr(x.rolling(window, min_periods=min_periods), how)()
    frame = x.to_frame() if isinstance(x, pd.Series) else x
    v = np.ascontiguousarray(frame.to_numpy(np.float64).T)            # column-major: one row of v per column
    out = np.empty_like(v)
    rc = _loaded().rc_rolling(v, frame.shape[0], frame.shape[1], window, minp, _HOW[how], out, threads())
    if rc != 0:
        raise RuntimeError(f"rc_rolling failed with code {rc}")
    res = pd.DataFrame(out.T, index=frame.index, columns=frame.columns)
    return res.iloc[:, 0].rename(cast(str, x.name)) if isinstance(x, pd.Series) else res


def _bytes_call(fn: Any, xml: bytes, *extra: Any) -> bytes | None:
    """Run an xlsx entry point; None when the Rust parser declines the XML (the caller then uses ElementTree)."""
    cap = len(xml) + 1024
    while True:
        buf = ctypes.create_string_buffer(cap)
        n = ctypes.c_size_t(0)
        rc = fn(xml, len(xml), buf, cap, ctypes.byref(n), *extra)
        if rc == -4:
            cap = n.value + 1024
            continue
        if rc == -5:
            return None
        if rc != 0:
            raise RuntimeError(f"{fn.__name__} failed with code {rc}")
        return buf.raw[: n.value]


def xlsx_shared(xml: bytes) -> list[str] | None:
    """Shared strings of xl/sharedStrings.xml as quant.core.xlsx reads them; None if Rust is not in use or declines the part."""
    if backend() == "python":
        return None
    count = ctypes.c_size_t(0)
    raw = _bytes_call(_loaded().rc_xlsx_shared, xml, ctypes.byref(count))
    if raw is None:
        return None
    out = raw.decode("utf-8").split("") if count.value else []
    if len(out) != count.value:
        raise RuntimeError("rc_xlsx_shared: string count mismatch")
    return out


def xlsx_rows(xml: bytes, shared: list[str]) -> dict[int, dict[str, str | None]] | None:
    """{row number: {column letters: value}} of one worksheet part as quant.core.xlsx reads it; None if Rust is not in use or declines the part."""
    if backend() == "python":
        return None
    raw = _bytes_call(_loaded().rc_xlsx_sheet, xml)
    if raw is None:
        return None
    rows: dict[int, dict[str, str | None]] = {}
    cells: dict[str, str | None] = {}
    for rec in raw.decode("utf-8").split("")[:-1]:
        if rec[0] == "R":
            cells = {}
            rows[int(rec[1:])] = cells
        else:
            col, kind, val = rec[1:].split("", 2)
            cells[col] = None if kind == "N" else shared[int(val)] if kind == "S" else val
    return rows


class PreTradeCheckUnavailable(RuntimeError):
    """The Rust pre-trade check could not run (no current library, or the plan was malformed); the plan must not be sent."""


def pre_trade_check(plan_json: str, kill_switch_on: bool, preflight_go: bool, owner_token: str | None) -> list[str]:
    """Reasons the plan must not be sent, from rust_core's `paper_checks` (PAPER amendment A7); empty when every check passes. There is no Python fallback: the
    check is the independent second implementation of the engine's gates, so QUANT_KERNELS is ignored and a missing or stale library raises
    PreTradeCheckUnavailable, as does a plan Rust finds malformed."""
    lib = _load()
    if lib is None:
        raise PreTradeCheckUnavailable(f"{_STATE['reason']}; run: tools/py -m quant.core.kernels build")
    data = plan_json.encode("utf-8")
    tok = (owner_token or "").encode("utf-8")
    flags = (1 if kill_switch_on else 0) | (2 if preflight_go else 0)
    cap = 4096
    while True:
        buf = ctypes.create_string_buffer(cap)
        n = ctypes.c_size_t(0)
        rc = lib.rc_paper_check(data, len(data), flags, tok or None, len(tok), buf, cap, ctypes.byref(n))
        if rc == -4:
            cap = n.value + 1024
            continue
        text = buf.raw[: n.value].decode("utf-8", "replace")
        if rc == -6:
            raise PreTradeCheckUnavailable(f"plan rejected as malformed: {text}")
        if rc != 0:
            raise PreTradeCheckUnavailable(f"rc_paper_check failed with code {rc}")
        reasons: list[str] = json.loads(text)
        return reasons


# ---------------------------------------------------------------- build

def _cargo_env() -> dict[str, Any]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(DEFAULT_DIR.parent / "rust_target"))        # under the user's profile, not the shared temporary folder
    extra = [str(Path.home() / ".cargo" / "bin")]
    wl = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    extra += [str(p / "mingw64" / "bin") for p in wl.glob("BrechtSanders.WinLibs*")] if wl.exists() else []
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(extra)
    return env


def build() -> Path:
    env = _cargo_env()
    subprocess.run(["cargo", "build", "--release", "--lib", "--manifest-path", str(CRATE / "Cargo.toml")], check=True, env=env)
    built = Path(env["CARGO_TARGET_DIR"]) / "release" / ("rust_core.dll" if sys.platform == "win32" else "librust_core.so")
    dest = library_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, dest)
    dest.with_name("rust_core.stamp.json").write_text(json.dumps({"source_sha256": source_hash(), "abi_version": ABI_VERSION,
                                                                  "library_sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}, indent=1) + "\n")
    return dest


def main(argv: list[str] | None = None) -> None:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "build":
        print("installed", build())
        return
    b = backend()
    print(json.dumps({"backend": b, **{k: v for k, v in _STATE.items() if k != "warned"}, "threads": threads()}, indent=1))
