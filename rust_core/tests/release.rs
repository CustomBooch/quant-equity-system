//! Release contract v1: the Rust verifier accepts the Python-written example release and refuses every kind of tampering.

use std::fs;
use std::path::{Path, PathBuf};

use rust_core::release::verify_release;

fn example() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../contracts/examples/release_example_v1")
}

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for e in fs::read_dir(from).unwrap() {
        let p = e.unwrap().path();
        let dest = to.join(p.file_name().unwrap());
        if p.is_dir() {
            copy_dir(&p, &dest);
        } else {
            fs::copy(&p, &dest).unwrap();
        }
    }
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("rust_core_release_test_{}_{name}", std::process::id()));
    let _ = fs::remove_dir_all(&d);
    copy_dir(&example(), &d);
    d
}

#[test]
fn python_written_release_verifies() {
    let doc = verify_release(&example()).unwrap();
    assert_eq!(doc.get("release").and_then(|v| v.as_str()), Some("release_example_v1"));
}

#[test]
fn each_kind_of_tampering_is_refused() {
    type Mutation<'a> = (&'a str, &'a dyn Fn(&Path), &'a str);
    let cases: [Mutation; 5] = [
        ("hash", &|d| fs::write(d.join("series.txt"), "month,value\n2020-01-01,0.01\n2020-02-01,-0.02\n2020-03-01,0.04\n").unwrap(), "hash differs"),
        ("size", &|d| fs::write(d.join("series.txt"), "short").unwrap(), "size differs"),
        ("missing", &|d| fs::remove_file(d.join("data").join("note.txt")).unwrap(), "missing"),
        ("extra", &|d| fs::write(d.join("sneaked.csv"), "x").unwrap(), "not listed"),
        (
            "schema",
            &|d| {
                let m = fs::read_to_string(d.join("VERSION.json")).unwrap().replace("release/1", "release/2");
                fs::write(d.join("VERSION.json"), m).unwrap()
            },
            "unsupported schema",
        ),
    ];
    for (name, mutate, want) in cases {
        let d = scratch(name);
        mutate(&d);
        let err = verify_release(&d).unwrap_err();
        assert!(err.contains(want), "{name}: expected {want:?} in {err:?}");
        let _ = fs::remove_dir_all(&d);
    }
}

#[test]
fn a_directory_without_a_manifest_is_refused() {
    let d = std::env::temp_dir().join(format!("rust_core_release_test_{}_empty", std::process::id()));
    fs::create_dir_all(&d).unwrap();
    assert!(verify_release(&d).is_err());
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn validation_is_required_only_when_asked_and_gates_must_all_pass() {
    use rust_core::release::require_validation;
    let ex = |n: &str| Path::new(env!("CARGO_MANIFEST_DIR")).join("../contracts/examples").join(n);
    let ok = verify_release(&ex("release_validated_v1")).unwrap();
    assert!(require_validation(&ok).is_ok());
    let bad = verify_release(&ex("release_failed_gate_v1")).unwrap(); // intact files, but a gate failed
    assert!(require_validation(&bad).unwrap_err().contains("gates not passed"));
    let plain = verify_release(&example()).unwrap();
    assert!(require_validation(&plain).unwrap_err().contains("no validation block"));
}

#[test]
fn a_nested_version_json_must_be_listed_only_the_root_manifest_is_exempt() {
    let d = scratch("nested");
    fs::write(d.join("data").join("VERSION.json"), "{}").unwrap();        // added after the release was written (amendment A15)
    let err = verify_release(&d).unwrap_err();
    assert!(err.contains("not listed") && err.contains("data/VERSION.json"), "{err}");
    let _ = fs::remove_dir_all(&d);
}
