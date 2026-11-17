//! rust_core command line. Read-only tools: nothing here talks to a broker or the network.
//!
//!   rust_core plan <input.json>                   -> plan JSON on stdout
//!   rust_core check <plan.json> [options]         -> reasons JSON on stdout; exit 1 if the plan must not be sent
//!        --kill-switch on|off   (default on)      --preflight go|nogo  (default nogo)      --confirm <plan_hash>
//!   rust_core tranche <YYYY-MM-DD>                -> tranche number, or an error when the day is not a Tuesday slot
//!   rust_core verify-release <release_dir> [--require-validation]  -> verify VERSION.json (release/1): sizes, SHA-256, nothing unlisted; exit 1 on any problem
//!   rust_core check-frozen <release_dir>          -> verify, then recompute the statistics in the manifest's `expected` block
//!
//! Defaults fail closed: with no flags `check` reports the kill switch on, preflight not GO and no confirmation.

use std::collections::BTreeMap;
use std::path::Path;
use std::process::ExitCode;

use rust_core::bootstrap::{mean, stationary_ci};
use rust_core::execution::{make_plan, parse_date, pre_trade_checks, tranche_of, K_TRANCHES};
use rust_core::hac::nw_t;
use rust_core::json::{dumps_sorted, parse, Value};
use rust_core::release::{require_validation, verify_release};

const TOL_STAT: f64 = 1e-9;
const TOL_BOOT: f64 = 1e-9;

fn rel(a: f64, b: f64) -> f64 {
    (a - b).abs() / b.abs().max(1.0)
}

fn read_json(path: &str) -> Result<Value, String> {
    parse(&std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?)
}

fn number_map(v: &Value, key: &str) -> Result<BTreeMap<String, f64>, String> {
    match v.get(key) {
        Some(Value::Obj(kv)) => kv.iter().map(|(k, x)| x.as_f64().map(|f| (k.clone(), f)).ok_or(format!("{key}.{k} is not a number"))).collect(),
        _ => Err(format!("input lacks object {key}")),
    }
}

fn cmd_plan(path: &str) -> Result<bool, String> {
    let v = read_json(path)?;
    let account = v.get("account_value_usd").filter(|a| a.as_f64().is_some()).ok_or("input lacks numeric account_value_usd")?;
    let fraction = v.get("tranche_fraction").map_or(Ok(1.0), |f| f.as_f64().ok_or("tranche_fraction is not a number"))?;
    let plan = make_plan(&number_map(&v, "target")?, &number_map(&v, "held")?, &number_map(&v, "prices")?, account, fraction);
    println!("{}", dumps_sorted(&plan));
    Ok(true)
}

fn cmd_check(path: &str, opts: &[String]) -> Result<bool, String> {
    let (mut kill, mut go, mut confirm) = (true, false, None);
    let mut it = opts.iter();
    while let Some(o) = it.next() {
        let val = it.next().ok_or(format!("{o} needs a value"))?;
        match o.as_str() {
            "--kill-switch" => {
                kill = match val.as_str() {
                    "on" => true,
                    "off" => false,
                    _ => return Err("--kill-switch takes on|off".into()),
                }
            }
            "--preflight" => {
                go = match val.as_str() {
                    "go" => true,
                    "nogo" => false,
                    _ => return Err("--preflight takes go|nogo".into()),
                }
            }
            "--confirm" => confirm = Some(val.clone()),
            _ => return Err(format!("unknown option {o}")),
        }
    }
    let reasons = pre_trade_checks(&read_json(path)?, kill, go, confirm.as_deref())?;
    println!("{}", dumps_sorted(&Value::Arr(reasons.iter().map(|r| Value::Str(r.clone())).collect())));
    Ok(reasons.is_empty())
}

fn cmd_tranche(date: &str) -> Result<bool, String> {
    let (y, m, d) = parse_date(date)?;
    println!("{}", tranche_of(y, m, d, K_TRANCHES)?);
    Ok(true)
}

fn cmd_verify_release(dir: &Path, require: bool) -> Result<bool, String> {
    let doc = verify_release(dir)?;
    if require {
        require_validation(&doc)?;
    }
    let n = match doc.get("files") {
        Some(Value::Obj(kv)) => kv.len(),
        _ => 0,
    };
    println!("release {} verified: {n} files", doc.get("release").and_then(Value::as_str).unwrap_or("?"));
    Ok(true)
}

fn cmd_check_frozen(dir: &Path) -> Result<bool, String> {
    let doc = verify_release(dir)?;
    let e = doc.get("expected").ok_or("manifest lacks expected")?;
    let num = |v: &Value, k: &str| -> Result<f64, String> { v.get(k).and_then(Value::as_f64).ok_or(format!("expected.{k} missing or not a number")) };
    let boot = e.get("bootstrap").ok_or("expected.bootstrap missing")?;
    let want_ci = e.get("boot95_ann_bp_stationary").and_then(Value::as_arr).ok_or("expected.boot95_ann_bp_stationary missing")?;
    let (lo, hi) = (want_ci.first().and_then(Value::as_f64).ok_or("bad interval")?, want_ci.get(1).and_then(Value::as_f64).ok_or("bad interval")?);

    let text = std::fs::read_to_string(dir.join("diff_series.csv")).map_err(|e| format!("diff_series.csv: {e}"))?;
    let x: Vec<f64> = text.lines().skip(1).map(|l| l.rsplit(',').next().unwrap().parse::<f64>().map_err(|e| e.to_string())).collect::<Result<_, _>>()?;

    let ann = mean(&x) * 12e4;
    let t = nw_t(&x, 6);
    let seed = num(boot, "seed")? as u64;
    let ci = stationary_ci(&x, &mean, num(boot, "reps")? as usize, num(boot, "mean_block")? as usize, seed).map(|v| v * 12e4);
    let checks = [
        ("n", x.len() as f64 == num(e, "n")?, x.len() as f64, num(e, "n")?),
        ("ann_bp", rel(ann, num(e, "ann_bp")?) < TOL_STAT, ann, num(e, "ann_bp")?),
        ("nw_t_lags6", rel(t, num(e, "nw_t_lags6")?) < TOL_STAT, t, num(e, "nw_t_lags6")?),
        ("boot95_lo", rel(ci[0], lo) < TOL_BOOT, ci[0], lo),
        ("boot95_hi", rel(ci[1], hi) < TOL_BOOT, ci[1], hi),
    ];
    println!("release {} verified (all files match the manifest)", doc.get("release").and_then(Value::as_str).unwrap_or("?"));
    let mut all = true;
    for (name, ok, rust, py) in checks {
        println!("{name:<12} rust {rust:<24.15} python {py:<24.15} diff {:.2e}  {}", rust - py, if ok { "OK" } else { "FAIL" });
        all &= ok;
    }
    Ok(all)
}

fn main() -> ExitCode {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let r = match a.first().map(String::as_str) {
        Some("plan") if a.len() == 2 => cmd_plan(&a[1]),
        Some("check") if a.len() >= 2 => cmd_check(&a[1], &a[2..]),
        Some("tranche") if a.len() == 2 => cmd_tranche(&a[1]),
        Some("verify-release") if a.len() == 2 => cmd_verify_release(Path::new(&a[1]), false),
        Some("verify-release") if a.len() == 3 && a[2] == "--require-validation" => cmd_verify_release(Path::new(&a[1]), true),
        Some("check-frozen") if a.len() == 2 => cmd_check_frozen(Path::new(&a[1])),
        _ => {
            eprintln!(
                "usage: rust_core plan <input.json> | check <plan.json> [--kill-switch on|off] [--preflight go|nogo] [--confirm HASH] | tranche <YYYY-MM-DD> | verify-release <dir> [--require-validation] | check-frozen <dir>"
            );
            return ExitCode::from(2);
        }
    };
    match r {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(1)
        }
    }
}
