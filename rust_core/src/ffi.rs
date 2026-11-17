//! C ABI for Python (`lab/kernels`, loaded with ctypes; no third-party crates, so no build scripts).
//!
//! Conventions: every function returns 0 on success and a negative code on bad arguments (it never panics across the boundary; a panic inside is caught and
//! reported as -99). Arrays are passed as pointer + length and are owned by the caller; outputs are written into caller-allocated buffers.
//! `rc_abi_version` is bumped whenever a signature or a result changes, and the Python loader refuses a library with another version.

use std::panic::{catch_unwind, AssertUnwindSafe};

use crate::rank;
use crate::rolling::{rolling_matrix, How};

pub const ABI_VERSION: u32 = 4;

#[no_mangle]
pub extern "C" fn rc_abi_version() -> u32 {
    ABI_VERSION
}

/// `design_all` of lab/core/c5_experts.py. `x`: k columns of n float32 values, column-major; `group`: n group ids (u32); `out`: n x 2k float32, row-major.
///
/// # Safety
/// `x` must point to `n * k` floats, `group` to `n` u32 and `out` to `n * 2 * k` writable floats.
#[no_mangle]
pub unsafe extern "C" fn rc_design_crank(x: *const f32, n: usize, k: usize, group: *const u32, out: *mut f32, threads: usize) -> i32 {
    if x.is_null() || group.is_null() || out.is_null() {
        return -1;
    }
    if n == 0 || k == 0 || n > u32::MAX as usize {
        return -2;
    }
    let x = std::slice::from_raw_parts(x, n * k);
    let group = std::slice::from_raw_parts(group, n);
    let out = std::slice::from_raw_parts_mut(out, n * 2 * k);
    match catch_unwind(AssertUnwindSafe(|| rank::design(x, n, k, group, out, threads.max(1)))) {
        Ok(()) => 0,
        Err(_) => -99,
    }
}

/// pandas `DataFrame.rolling(window, min_periods).sum() | .mean() | .max()` for `k` float64 columns of `n` rows, column-major in and out. `how`: 0 sum, 1 mean, 2 max.
///
/// # Safety
/// `x` and `out` must point to `n * k` float64 values (`out` writable).
#[no_mangle]
pub unsafe extern "C" fn rc_rolling(x: *const f64, n: usize, k: usize, window: usize, min_periods: usize, how: u32, out: *mut f64, threads: usize) -> i32 {
    if x.is_null() || out.is_null() {
        return -1;
    }
    if window == 0 || min_periods > window {
        return -2;
    }
    let how = match how {
        0 => How::Sum,
        1 => How::Mean,
        2 => How::Max,
        _ => return -3,
    };
    if n == 0 || k == 0 {
        return 0;
    }
    let x = std::slice::from_raw_parts(x, n * k);
    let out = std::slice::from_raw_parts_mut(out, n * k);
    match catch_unwind(AssertUnwindSafe(|| rolling_matrix(x, n, k, window, min_periods, how, out, threads.max(1)))) {
        Ok(()) => 0,
        Err(_) => -99,
    }
}

unsafe fn write_out(res: Result<Vec<u8>, String>, out: *mut u8, cap: usize, out_len: *mut usize) -> i32 {
    match res {
        Err(_) => -5,
        Ok(v) => {
            *out_len = v.len();
            if v.len() > cap {
                return -4;
            }
            std::ptr::copy_nonoverlapping(v.as_ptr(), out, v.len());
            0
        }
    }
}

/// Cells of a worksheet XML part (see `xlsx`). Returns 0, -4 when `cap` is too small (`*out_len` then holds the size needed) or -5 when the XML is not
/// handled (the caller falls back to ElementTree).
///
/// # Safety
/// `xml` must point to `len` bytes, `out` to `cap` writable bytes and `out_len` to one writable usize.
#[no_mangle]
pub unsafe extern "C" fn rc_xlsx_sheet(xml: *const u8, len: usize, out: *mut u8, cap: usize, out_len: *mut usize) -> i32 {
    if xml.is_null() || out.is_null() || out_len.is_null() {
        return -1;
    }
    let xml = std::slice::from_raw_parts(xml, len);
    match catch_unwind(AssertUnwindSafe(|| crate::xlsx::sheet_cells(xml))) {
        Ok(r) => write_out(r, out, cap, out_len),
        Err(_) => -99,
    }
}

/// The PAPER pre-trade checks (`execution::paper_checks`) on a plan given as JSON text. `flags`: bit 0 kill switch on, bit 1 preflight GO. `confirm`: the owner's
/// token text (`confirm_len` 0 for none). Writes the reasons as a JSON array (empty: every check passes) and returns 0; returns -6 with the error message
/// written instead when the plan is malformed or not JSON (the plan must not be sent); -4 when `cap` is too small (`*out_len` then holds the size needed).
///
/// # Safety
/// `plan` must point to `len` bytes, `confirm` to `confirm_len` bytes (or be null when it is 0), `out` to `cap` writable bytes and `out_len` to one writable usize.
#[no_mangle]
pub unsafe extern "C" fn rc_paper_check(plan: *const u8, len: usize, flags: u32, confirm: *const u8, confirm_len: usize, out: *mut u8, cap: usize, out_len: *mut usize) -> i32 {
    if plan.is_null() || out.is_null() || out_len.is_null() || (confirm.is_null() && confirm_len > 0) {
        return -1;
    }
    let plan = std::slice::from_raw_parts(plan, len);
    let confirm = if confirm_len == 0 { None } else { Some(std::slice::from_raw_parts(confirm, confirm_len)) };
    let run = || -> Result<Vec<String>, String> {
        let text = std::str::from_utf8(plan).map_err(|e| format!("plan is not UTF-8: {e}"))?;
        let token = confirm.map(std::str::from_utf8).transpose().map_err(|e| format!("token is not UTF-8: {e}"))?;
        crate::execution::paper_checks(&crate::json::parse(text)?, flags & 1 != 0, flags & 2 != 0, token)
    };
    match catch_unwind(AssertUnwindSafe(run)) {
        Ok(Ok(reasons)) => {
            let v = crate::json::Value::Arr(reasons.into_iter().map(crate::json::Value::Str).collect());
            write_out(Ok(crate::json::dumps_sorted(&v).into_bytes()), out, cap, out_len)
        }
        Ok(Err(msg)) => match write_out(Ok(msg.into_bytes()), out, cap, out_len) {
            0 => -6,
            rc => rc,
        },
        Err(_) => -99,
    }
}

/// Shared strings of `xl/sharedStrings.xml`, separated by \x1e; `*count` receives their number. Return codes as `rc_xlsx_sheet`.
///
/// # Safety
/// As `rc_xlsx_sheet`, and `count` must point to one writable usize.
#[no_mangle]
pub unsafe extern "C" fn rc_xlsx_shared(xml: *const u8, len: usize, out: *mut u8, cap: usize, out_len: *mut usize, count: *mut usize) -> i32 {
    if xml.is_null() || out.is_null() || out_len.is_null() || count.is_null() {
        return -1;
    }
    let xml = std::slice::from_raw_parts(xml, len);
    match catch_unwind(AssertUnwindSafe(|| crate::xlsx::shared_strings(xml))) {
        Ok(Ok((v, n))) => {
            *count = n;
            write_out(Ok(v), out, cap, out_len)
        }
        Ok(Err(e)) => write_out(Err(e), out, cap, out_len),
        Err(_) => -99,
    }
}
