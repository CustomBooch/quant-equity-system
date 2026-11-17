//! Rust kernels for the quant project.
//!
//! * `execution`: order planning and pre-trade checks (ported from `rank_signals.execution.plan`; pure, no broker, no network).
//! * `bootstrap`, `hac`, `numpy_rng`: simulation kernels. `numpy_rng` reproduces NumPy's `default_rng(seed)`, so a seed gives the same resamples as the Python code.
//! * `release`: verification of release directories (contracts/RELEASE_V1.md); streaming `sha256`.
//! * `json`, `pyfmt`, `sha256`: exact Python-compatible JSON and number formatting and hashing, so payload hashes agree across languages.
//! * `xlsx`: cell and shared-string extraction from .xlsx XML parts, equal to the ElementTree reader of lab/ciqfill/xlsx.py.
//! * `rolling`: fixed-window rolling sum, mean and max replaying pandas' floating-point operations (bit-identical).
//! * `rank`: within-group centred ranks and the the model design matrix, bit-identical to the pandas reference; `ffi` exposes it to Python through a C ABI
//!   (the crate also builds as a cdylib, `rust_core.dll`, loaded by `lab/kernels` with ctypes).
//!
//! Everything is tested against fixtures written by the Python reference. No third-party crates.

pub mod bootstrap;
pub mod execution;
pub mod ffi;
pub mod hac;
pub mod json;
pub mod numpy_rng;
pub mod pyfmt;
pub mod rank;
pub mod rolling;
pub mod release;
pub mod sha256;
pub mod xlsx;
