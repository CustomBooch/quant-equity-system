//! Order PLANNING and pre-trade checks, ported from `rank_signals.execution.plan`. Pure functions: nothing here talks to a broker or the network.
//!
//! Behaviour equals the Python code (golden tests, including the fail-closed cases). Both fail closed since the Python reference was fixed (signal repository commit 4f702a4):
//!   1. A non-finite price or quantity is skipped with reason `invalid_price` / `invalid_quantity` (it is never turned into an order).
//!   2. `overlay_exposure` rejects non-finite inputs (a NaN comparison is false, which would silently mean full exposure).
//!   3. A non-finite order value counts as above the size cap; a malformed plan is an error, never a pass.

use std::collections::{BTreeMap, BTreeSet};

use crate::json::{dumps_sorted, Value};
use crate::pyfmt::{repr_str_list, round_py};
use crate::sha256::sha256_hex;

pub const K_TRANCHES: i64 = 4;
pub const WEEKDAY: i64 = 1; // Tuesday, Monday = 0
pub const DEFENSIVE_EXPOSURE: f64 = 0.5;
pub const MIN_ORDER_USD: f64 = 5.0;
pub const MAX_ORDER_FRAC: f64 = 0.10;
pub const MAX_SHORT_FRAC: f64 = 0.30; // PAPER amendment A11
pub const MAX_GROSS: f64 = 2.0; // PAPER amendment A12: leverage x (1 + 2 x short fraction) at most this
pub const MAX_SHORT_LEG: f64 = 0.40; // PAPER amendment A12: short sales per tranche at most this x account / K x 1.05
pub const QTY_DECIMALS: usize = 4;

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn valid_date(y: i64, m: i64, d: i64) -> bool {
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let dim = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    (1..=12).contains(&m) && d >= 1 && d <= dim[(m - 1) as usize]
}

/// Monday = 0, as `datetime.date.weekday()`.
pub fn weekday(y: i64, m: i64, d: i64) -> i64 {
    (days_from_civil(y, m, d) + 3).rem_euclid(7)
}

fn weeks_in_year(y: i64) -> i64 {
    let p = |y: i64| (y + y.div_euclid(4) - y.div_euclid(100) + y.div_euclid(400)).rem_euclid(7);
    if p(y) == 4 || p(y - 1) == 3 {
        53
    } else {
        52
    }
}

/// ISO-8601 week number, as `date.isocalendar().week`.
pub fn iso_week(y: i64, m: i64, d: i64) -> i64 {
    let ordinal = days_from_civil(y, m, d) - days_from_civil(y, 1, 1) + 1;
    let w = (ordinal - (weekday(y, m, d) + 1) + 10).div_euclid(7);
    if w < 1 {
        weeks_in_year(y - 1)
    } else if w > weeks_in_year(y) {
        1
    } else {
        w
    }
}

pub fn parse_date(s: &str) -> Result<(i64, i64, i64), String> {
    let p: Vec<&str> = s.split('-').collect();
    if p.len() != 3 || p[0].len() != 4 || p[1].len() != 2 || p[2].len() != 2 {
        return Err(format!("date must be YYYY-MM-DD, got {s:?}"));
    }
    let n = |t: &str| t.parse::<i64>().map_err(|e| format!("{s:?}: {e}"));
    let (y, m, d) = (n(p[0])?, n(p[1])?, n(p[2])?);
    if valid_date(y, m, d) {
        Ok((y, m, d))
    } else {
        Err(format!("{s:?} is not a calendar date"))
    }
}

pub fn is_slot(y: i64, m: i64, d: i64) -> bool {
    weekday(y, m, d) == WEEKDAY
}

/// Tranche rebuilt on a slot day (ISO week number modulo k). Errors when the day is not a Tuesday.
pub fn tranche_of(y: i64, m: i64, d: i64, k: i64) -> Result<i64, String> {
    if !is_slot(y, m, d) {
        return Err(format!("{y:04}-{m:02}-{d:02} is not a Tuesday slot"));
    }
    if k < 1 {
        return Err("k must be at least 1".into());
    }
    Ok(iso_week(y, m, d).rem_euclid(k))
}

/// Trend overlay exposure. Fails closed on non-finite inputs.
pub fn overlay_exposure(market_close: f64, ten_month_mean: f64, level: f64) -> Result<f64, String> {
    if !(market_close.is_finite() && ten_month_mean.is_finite() && level.is_finite()) {
        return Err("overlay inputs must be finite".into());
    }
    Ok(if market_close < ten_month_mean { level } else { 1.0 })
}

pub fn tranche_target_shares(
    weights: &BTreeMap<String, f64>,
    prices: &BTreeMap<String, f64>,
    account_value_usd: f64,
    exposure: f64,
    k: i64,
) -> Result<BTreeMap<String, f64>, String> {
    if !((0.0..=1.0).contains(&exposure) && k >= 1) {
        return Err("exposure must lie in [0, 1] and k must be at least 1".into());
    }
    let budget = account_value_usd / k as f64 * exposure;
    Ok(weights.iter().filter_map(|(t, w)| prices.get(t).filter(|p| **p > 0.0).map(|p| (t.clone(), round_py(w * budget / p, QTY_DECIMALS)))).collect())
}

fn order_value(ticker: &str, quantity: f64, usd: f64, side: &str) -> Value {
    Value::obj([("ticker", Value::Str(ticker.into())), ("quantity", Value::Float(quantity)), ("usd", Value::Float(usd)), ("side", Value::Str(side.into()))])
}

fn round_account(v: &Value) -> Value {
    match v {
        Value::Float(f) => Value::Float(round_py(*f, 2)),
        other => other.clone(),
    }
}

/// SHA-256 of `json.dumps({"orders": orders, "account_value_usd": round(account, 2)}, sort_keys=True)`.
pub fn plan_hash(orders: &[Value], account_value_usd: &Value) -> String {
    let payload = Value::obj([("orders", Value::Arr(orders.to_vec())), ("account_value_usd", round_account(account_value_usd))]);
    sha256_hex(dumps_sorted(&payload).as_bytes())
}

/// Orders that move `held` a fraction of the way to `target`. Returns the plan as a JSON value with keys orders, skipped, account_value_usd, plan_hash.
pub fn make_plan(target: &BTreeMap<String, f64>, held: &BTreeMap<String, f64>, prices: &BTreeMap<String, f64>, account_value_usd: &Value, tranche_fraction: f64) -> Value {
    let mut orders = Vec::new();
    let mut skipped = Vec::new();
    let names: BTreeSet<&String> = target.keys().chain(held.keys()).collect();
    for t in names {
        let raw = (target.get(t).copied().unwrap_or(0.0) - held.get(t).copied().unwrap_or(0.0)) * tranche_fraction;
        if !raw.is_finite() {
            skipped.push(Value::obj([("ticker", Value::Str(t.clone())), ("reason", Value::Str("invalid_quantity".into()))]));
            continue;
        }
        let d = round_py(raw, QTY_DECIMALS);
        if d == 0.0 {
            continue;
        }
        let Some(&px) = prices.get(t) else {
            skipped.push(Value::obj([("ticker", Value::Str(t.clone())), ("reason", Value::Str("no_price".into()))]));
            continue;
        };
        if !px.is_finite() {
            skipped.push(Value::obj([("ticker", Value::Str(t.clone())), ("reason", Value::Str("invalid_price".into()))]));
            continue;
        }
        let usd = d.abs() * px;
        if usd < MIN_ORDER_USD {
            skipped.push(Value::obj([("ticker", Value::Str(t.clone())), ("reason", Value::Str("below_min_order".into())), ("usd", Value::Float(round_py(usd, 2)))]));
            continue;
        }
        orders.push(order_value(t, d, round_py(usd, 2), if d > 0.0 { "BUY" } else { "SELL" }));
    }
    let hash = plan_hash(&orders, account_value_usd);
    Value::obj([("orders", Value::Arr(orders)), ("skipped", Value::Arr(skipped)), ("account_value_usd", account_value_usd.clone()), ("plan_hash", Value::Str(hash))])
}

/// An Alpaca US equity symbol (PAPER amendment A10): 1-5 capital letters, optionally a share class of 1-2 capital letters after a dot (BRK.B).
pub fn alpaca_symbol(s: &str) -> bool {
    let (base, class) = match s.split_once('.') {
        Some((b, c)) => (b, Some(c)),
        None => (s, None),
    };
    let caps = |x: &str, lo: usize, hi: usize| x.len() >= lo && x.len() <= hi && x.bytes().all(|c| c.is_ascii_uppercase());
    caps(base, 1, 5) && class.map_or(true, |c| caps(c, 1, 2))
}

/// Reasons the plan must NOT be sent (empty = all checks pass). Errors if the plan is malformed, which also means it must not be sent.
pub fn pre_trade_checks(plan: &Value, kill_switch_on: bool, preflight_go: bool, owner_confirmed_hash: Option<&str>) -> Result<Vec<String>, String> {
    let orders = plan.get("orders").and_then(Value::as_arr).ok_or("plan lacks orders")?;
    let account = plan.get("account_value_usd").ok_or("plan lacks account_value_usd")?;
    let account_f = account.as_f64().ok_or("account_value_usd is not a number")?;
    let recorded = plan.get("plan_hash").and_then(Value::as_str).ok_or("plan lacks plan_hash")?;
    let mut reasons = Vec::new();
    if kill_switch_on {
        reasons.push("kill switch is on".to_string());
    }
    if !preflight_go {
        reasons.push("preflight is not GO".to_string());
    }
    if plan_hash(orders, account) != recorded {
        reasons.push("plan changed since it was written".to_string());
    }
    if owner_confirmed_hash != Some(recorded) {
        reasons.push("no owner confirmation for this plan".to_string());
    }
    let cap = MAX_ORDER_FRAC * account_f;
    let mut big = Vec::new();
    let mut bad = Vec::new();
    for o in orders {
        let ticker = o.get("ticker").and_then(Value::as_str).ok_or("order lacks ticker")?;
        let usd = o.get("usd").and_then(Value::as_f64).ok_or("order lacks usd")?;
        if usd > cap || usd.is_nan() {
            big.push(ticker.to_string());
        }
        if !ticker.ends_with("_US_EQ") {
            bad.push(ticker.to_string());
        }
    }
    if !big.is_empty() {
        reasons.push(format!("orders above {:.0}% of the account: {}", MAX_ORDER_FRAC * 100.0, repr_str_list(&big)));
    }
    if !bad.is_empty() {
        reasons.push(format!("non-USD instrument lines: {}", repr_str_list(&bad)));
    }
    Ok(reasons)
}

/// The PAPER engine's pre-trade checks (lab/paper/SPEC.md section 4 and amendment A7): `pre_trade_checks` unchanged, plus the order-content gates that
/// `lab.paper.engine.check_gates` applies and the ported reference does not: every order has a finite, non-zero quantity whose sign matches its side; no ticker
/// appears twice; buys together are at most the account. The engine calls this through `ffi::rc_paper_check` after its own gates pass, and refuses on any
/// reason or error. A missing or non-numeric field, or a non-finite account, is an error (the plan must not be sent), never a pass.
pub fn paper_checks(plan: &Value, kill_switch_on: bool, preflight_go: bool, owner_confirmed_hash: Option<&str>) -> Result<Vec<String>, String> {
    let mut reasons = pre_trade_checks(plan, kill_switch_on, preflight_go, owner_confirmed_hash)?;
    let orders = plan.get("orders").and_then(Value::as_arr).ok_or("plan lacks orders")?;
    // PAPER amendments A10 and A14: a plan names its broker, and the only broker is "alpaca": plain US symbols, so the ported reference's
    // instrument-line reason is replaced by the Alpaca symbol rule. A plan without the field, or naming any other broker, is an error (never sent).
    match plan.get("broker").map(Value::as_str) {
        Some(Some("alpaca")) => {
            reasons.retain(|r| !r.starts_with("non-USD instrument lines: "));
            let mut bad = Vec::new();
            for o in orders {
                let ticker = o.get("ticker").and_then(Value::as_str).ok_or("order lacks ticker")?;
                if !alpaca_symbol(ticker) {
                    bad.push(ticker.to_string());
                }
            }
            if !bad.is_empty() {
                reasons.push(format!("not Alpaca US equity symbols: {}", repr_str_list(&bad)));
            }
        }
        None => return Err("plan names no broker (made before amendment A10): only alpaca exists".into()),
        _ => return Err("plan names an unknown broker: only alpaca exists (amendment A14)".into()),
    }
    let account = plan.get("account_value_usd").and_then(Value::as_f64).ok_or("account_value_usd is not a number")?;
    if !account.is_finite() || account <= 0.0 {
        return Err("account_value_usd must be a positive finite number".into());
    }
    // PAPER amendment A11: a declared short fraction s in [0, 0.30] raises the buy limit to (1 + s) x the account; an order that takes the
    // tranche's recorded holding (`held`) below zero is a short sale: it needs s > 0 and a whole number of shares, and such sales together are at
    // most MAX_SHORT_LEG x account / K x 1.05 (A12 raised the cap from MAX_SHORT_FRAC).
    let short_frac = match plan.get("short_fraction") {
        None => 0.0,
        Some(v) => v.as_f64().ok_or("short_fraction is not a number")?,
    };
    if !short_frac.is_finite() || !(0.0..=MAX_SHORT_FRAC).contains(&short_frac) {
        reasons.push(format!("short_fraction {short_frac} outside [0, {MAX_SHORT_FRAC}]"));
    }
    // PAPER amendment A12: a declared leverage k >= 1 with k x (1 + 2 s) <= MAX_GROSS scales the buy limit to k x (1 + s) x the account; only
    // the Alpaca margin account may be levered.
    let leverage = match plan.get("leverage") {
        None => 1.0,
        Some(v) => v.as_f64().ok_or("leverage is not a number")?,
    };
    let sf_c = if short_frac.is_finite() { short_frac.clamp(0.0, MAX_SHORT_FRAC) } else { 0.0 };
    if !leverage.is_finite() || leverage < 1.0 || leverage * (1.0 + 2.0 * sf_c) > MAX_GROSS + 1e-9 {
        reasons.push(format!("leverage {leverage} outside [1, {MAX_GROSS} / (1 + 2 x short fraction)]"));
    }
    if leverage > 1.0 && plan.get("broker").and_then(Value::as_str) != Some("alpaca") {
        reasons.push("leverage above 1 needs the Alpaca margin account".into());
    }
    let lev_c = if leverage.is_finite() { leverage.clamp(1.0, MAX_GROSS / (1.0 + 2.0 * sf_c)) } else { 1.0 };
    // Short-sale detection needs the tranche's holdings: every Alpaca plan records `held` (a plan without it is malformed).
    let alpaca = plan.get("broker").and_then(Value::as_str) == Some("alpaca");
    if alpaca && plan.get("held").is_none() {
        return Err("an Alpaca plan lacks held".into());
    }
    let held_of = |t: &str| plan.get("held").and_then(|h| h.get(t)).and_then(Value::as_f64).unwrap_or(0.0);
    let mut no_fraction = Vec::new();
    let mut not_whole = Vec::new();
    let mut opened_usd = 0.0;
    let mut malformed = Vec::new();
    let mut seen = BTreeSet::new();
    let mut repeated = Vec::new();
    let mut buys = 0.0;
    for o in orders {
        let ticker = o.get("ticker").and_then(Value::as_str).ok_or("order lacks ticker")?;
        let quantity = o.get("quantity").and_then(Value::as_f64).ok_or("order lacks quantity")?;
        let side = o.get("side").and_then(Value::as_str).ok_or("order lacks side")?;
        let usd = o.get("usd").and_then(Value::as_f64).ok_or("order lacks usd")?;
        let expected = if quantity > 0.0 { "BUY" } else { "SELL" };
        if !quantity.is_finite() || quantity == 0.0 || side != expected || !usd.is_finite() || usd <= 0.0 {
            malformed.push(ticker.to_string());
        }
        if !seen.insert(ticker) {
            repeated.push(ticker.to_string());
        }
        if side == "BUY" {
            buys += usd;
        }
        let before = held_of(ticker);
        let after = before + quantity;
        if alpaca && quantity < 0.0 && after < -1e-9 {
            if short_frac <= 0.0 {
                no_fraction.push(ticker.to_string());
            }
            if quantity.fract() != 0.0 {
                not_whole.push(ticker.to_string());
            }
            let opened = (-after).max(0.0) - (-before).max(0.0);
            if opened > 0.0 {
                opened_usd += opened * usd / quantity.abs();
            }
        }
    }
    if !no_fraction.is_empty() {
        reasons.push(format!("short sales without a declared short fraction: {}", repr_str_list(&no_fraction)));
    }
    if !not_whole.is_empty() {
        reasons.push(format!("short sales that are not whole shares: {}", repr_str_list(&not_whole)));
    }
    let short_cap = MAX_SHORT_LEG * account / K_TRANCHES as f64 * 1.05;
    if opened_usd > short_cap || opened_usd.is_nan() {
        reasons.push(format!("short sales total {opened_usd:.2} USD, more than {short_cap:.2}"));
    }
    if !malformed.is_empty() {
        reasons.push(format!("malformed orders: {}", repr_str_list(&malformed)));
    }
    if !repeated.is_empty() {
        reasons.push(format!("tickers that appear twice: {}", repr_str_list(&repeated)));
    }
    let buy_limit = account * lev_c * (1.0 + sf_c);
    if buys > buy_limit || buys.is_nan() {
        if lev_c > 1.0 {
            reasons.push(format!("buys total {buys:.2} USD, more than {buy_limit:.2} (leverage x (1 + short fraction) x the account)"));
        } else if short_frac > 0.0 {
            reasons.push(format!("buys total {buys:.2} USD, more than {buy_limit:.2} ((1 + short fraction) x the account)"));
        } else {
            reasons.push(format!("buys total {buys:.2} USD, more than the account's {account:.2}"));
        }
    }
    Ok(reasons)
}

/// Differences between the positions expected after the plan and those read back.
pub fn reconcile(plan: &Value, held_after: &BTreeMap<String, f64>, held_before: &BTreeMap<String, f64>, tol: f64) -> Result<Vec<Value>, String> {
    let mut exp = held_before.clone();
    for o in plan.get("orders").and_then(Value::as_arr).ok_or("plan lacks orders")? {
        let t = o.get("ticker").and_then(Value::as_str).ok_or("order lacks ticker")?;
        let q = o.get("quantity").and_then(Value::as_f64).ok_or("order lacks quantity")?;
        let e = exp.entry(t.to_string()).or_insert(0.0);
        *e = round_py(*e + q, QTY_DECIMALS);
    }
    let names: BTreeSet<&String> = exp.keys().chain(held_after.keys()).collect();
    let mut diffs = Vec::new();
    for t in names {
        let (e, a) = (exp.get(t).copied().unwrap_or(0.0), held_after.get(t).copied().unwrap_or(0.0));
        let d = a - e;
        if d.abs() > tol {
            diffs.push(Value::obj([
                ("ticker", Value::Str(t.clone())),
                ("expected", Value::Float(e)),
                ("actual", Value::Float(a)),
                ("diff", Value::Float(round_py(d, QTY_DECIMALS))),
            ]));
        }
    }
    Ok(diffs)
}
