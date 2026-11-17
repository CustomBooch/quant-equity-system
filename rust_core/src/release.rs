//! Release contract v1 (contracts/RELEASE_V1.md): verify a release directory before any file in it is read.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::json::{parse, Value};
use crate::sha256::sha256_file;

pub const SCHEMA: &str = "release/1";
pub const MANIFEST: &str = "VERSION.json";

fn walk(dir: &Path, root: &Path, out: &mut BTreeSet<String>) -> Result<(), String> {
    for e in std::fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))? {
        let p: PathBuf = e.map_err(|e| e.to_string())?.path();
        if p.is_dir() {
            walk(&p, root, out)?;
        } else {
            let rel = p.strip_prefix(root).map_err(|e| e.to_string())?.to_string_lossy().replace('\\', "/");
            if rel != MANIFEST {
                out.insert(rel);                                // only the root manifest is not a listed file; a nested VERSION.json is (A15)
            }
        }
    }
    Ok(())
}

/// Returns the manifest when every listed file matches (size and SHA-256) and nothing unlisted is present. The error lists every problem found.
pub fn verify_release(dir: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(dir.join(MANIFEST)).map_err(|e| format!("cannot read {MANIFEST}: {e}"))?;
    let doc = parse(&text).map_err(|e| format!("{MANIFEST}: {e}"))?;
    match doc.get("schema").and_then(Value::as_str) {
        Some(SCHEMA) => {}
        other => return Err(format!("unsupported schema {other:?}; expected {SCHEMA:?}")),
    }
    let files = match doc.get("files") {
        Some(Value::Obj(kv)) if !kv.is_empty() => kv,
        _ => return Err("manifest lists no files".into()),
    };
    let mut problems = Vec::new();
    for (rel, meta) in files {
        let p = dir.join(rel);
        if !p.is_file() {
            problems.push(format!("missing: {rel}"));
            continue;
        }
        let want_bytes = meta.get("bytes").and_then(Value::as_f64);
        let want_hash = meta.get("sha256").and_then(Value::as_str);
        let size = std::fs::metadata(&p).map_err(|e| e.to_string())?.len() as f64;
        if want_bytes != Some(size) {
            problems.push(format!("size differs: {rel}"));
        } else if want_hash != Some(sha256_file(&p).map_err(|e| format!("{rel}: {e}"))?.as_str()) {
            problems.push(format!("hash differs: {rel}"));
        }
    }
    let mut present = BTreeSet::new();
    walk(dir, dir, &mut present)?;
    problems.extend(present.iter().filter(|rel| !files.iter().any(|(k, _)| k == *rel)).map(|rel| format!("not listed in manifest: {rel}")));
    if problems.is_empty() {
        Ok(doc)
    } else {
        Err(problems.join("; "))
    }
}

/// For data-layer releases: a validation block whose report hash is that of a listed file, with every gate exactly "pass".
pub fn require_validation(doc: &Value) -> Result<(), String> {
    let v = doc.get("validation").ok_or("release has no validation block")?;
    let gates = match v.get("gates") {
        Some(Value::Obj(kv)) if !kv.is_empty() => kv,
        _ => return Err("validation lists no gates".into()),
    };
    let mut failed: Vec<&str> = gates.iter().filter(|(_, g)| g.as_str() != Some("pass")).map(|(k, _)| k.as_str()).collect();
    failed.sort();
    if !failed.is_empty() {
        return Err(format!("validation gates not passed: {failed:?}"));
    }
    let report = v.get("report_sha256").and_then(Value::as_str);
    let listed = match doc.get("files") {
        Some(Value::Obj(kv)) => kv.iter().any(|(_, m)| m.get("sha256").and_then(Value::as_str) == report),
        _ => false,
    };
    if report.is_none() || !listed {
        return Err("validation report hash is not that of a listed file".into());
    }
    Ok(())
}
