//! `execution::paper_checks` (PAPER amendment A7): the ported checks unchanged plus the engine's order-content gates, and `ffi::rc_paper_check` across the C ABI.

use std::collections::BTreeMap;

use rust_core::execution::*;
use rust_core::ffi::rc_paper_check;
use rust_core::json::{dumps_sorted, parse, Value};

fn m(kv: &[(&str, f64)]) -> BTreeMap<String, f64> {
    kv.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

/// The plan made for the Alpaca paper account (amendments A10 and A14: the only broker), holding enough of each name it sells that no sale is a short sale.
fn alpaca(mut p: Value) -> Value {
    let sold: Vec<(String, Value)> = p.get("orders").and_then(Value::as_arr).unwrap_or(&[]).iter()
        .filter_map(|o| o.get("ticker").and_then(Value::as_str)).map(|t| (t.to_string(), Value::Float(1e9))).collect();
    if let Value::Obj(kv) = &mut p {
        kv.push(("broker".to_string(), Value::Str("alpaca".into())));
        kv.push(("held".to_string(), Value::Obj(sold)));
    }
    p
}

/// A valid plan: one buy, one sell, well under every cap, hash recorded by `make_plan`.
fn plan() -> Value {
    alpaca(make_plan(&m(&[("AAA", 10.0), ("BBB", 0.0)]), &m(&[("BBB", 4.0)]), &m(&[("AAA", 50.0), ("BBB", 100.0)]), &Value::Float(13374.89), 1.0))
}

fn hash(p: &Value) -> String {
    p.get("plan_hash").unwrap().as_str().unwrap().to_string()
}

/// The plan with its orders replaced and its hash recomputed, so only the order-content gates can object.
fn with_orders(orders: Vec<Value>, account: f64) -> Value {
    let acct = Value::Float(account);
    let h = plan_hash(&orders, &acct, None);
    alpaca(Value::obj([("orders", Value::Arr(orders)), ("skipped", Value::Arr(vec![])), ("account_value_usd", acct), ("plan_hash", Value::Str(h))]))
}

fn order(t: &str, q: f64, usd: f64, side: &str) -> Value {
    Value::obj([("ticker", Value::Str(t.into())), ("quantity", Value::Float(q)), ("usd", Value::Float(usd)), ("side", Value::Str(side.into()))])
}

#[test]
fn a_valid_confirmed_plan_passes_every_check() {
    let p = plan();
    assert_eq!(p.get("orders").unwrap().as_arr().unwrap().len(), 2);
    assert_eq!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap(), Vec::<String>::new());
}

#[test]
fn the_ported_checks_still_apply_and_fail_closed_by_default() {
    let p = plan();
    let r = paper_checks(&p, true, false, None).unwrap();
    assert_eq!(r, vec!["kill switch is on", "preflight is not GO", "no owner confirmation for this plan"]);
}

#[test]
fn a_quantity_whose_sign_disagrees_with_its_side_is_malformed() {
    let p = with_orders(vec![order("AAA", 2.0, 100.0, "SELL"), order("BBB", -1.0, 100.0, "SELL")], 13374.89);
    assert_eq!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap(), vec!["malformed orders: ['AAA']"]);
}

#[test]
fn zero_and_non_finite_quantities_and_values_are_malformed() {
    let p = with_orders(
        vec![order("AAA", 0.0, 0.0, "SELL"), order("BBB", f64::NAN, 10.0, "SELL"), order("CCC", 1.0, f64::INFINITY, "BUY")],
        13374.89,
    );
    let r = paper_checks(&p, false, true, Some(&hash(&p))).unwrap();
    assert!(r.contains(&"malformed orders: ['AAA', 'BBB', 'CCC']".to_string()), "{r:?}");
}

#[test]
fn a_ticker_listed_twice_is_refused() {
    let p = with_orders(vec![order("AAA", 1.0, 50.0, "BUY"), order("AAA", 1.0, 50.0, "BUY")], 13374.89);
    assert_eq!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap(), vec!["tickers that appear twice: ['AAA']"]);
}

#[test]
fn buys_above_the_account_are_refused_even_when_each_is_under_its_cap() {
    let orders: Vec<Value> = (0..11).map(|i| order(&format!("T{}", (b'A' + i as u8) as char), 1.0, 99.0, "BUY")).collect();
    let p = with_orders(orders, 1000.0);
    assert_eq!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap(), vec!["buys total 1089.00 USD, more than the account's 1000.00"]);
}

#[test]
fn sells_do_not_count_towards_the_buy_total() {
    let p = with_orders(vec![order("AAA", -5.0, 99.0, "SELL"), order("BBB", 1.0, 99.0, "BUY")], 1000.0);
    assert!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap().is_empty());
}

#[test]
fn a_missing_field_or_a_bad_account_is_an_error_not_a_pass() {
    let p = with_orders(vec![Value::obj([("ticker", Value::Str("AAA".into())), ("quantity", Value::Float(1.0)), ("usd", Value::Float(5.0))])], 1000.0);
    assert!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap_err().contains("side"));
    for acct in [0.0, f64::NAN, -5.0] {
        let q = with_orders(vec![], acct);
        assert!(paper_checks(&q, false, true, Some(&hash(&q))).is_err(), "account {acct}");
    }
}

fn call(plan: &str, flags: u32, confirm: Option<&str>, cap: usize) -> (i32, String, usize) {
    let mut out = vec![0u8; cap];
    let mut n = 0usize;
    let (cp, cl) = confirm.map_or((std::ptr::null(), 0), |c| (c.as_ptr(), c.len()));
    let rc = unsafe { rc_paper_check(plan.as_ptr(), plan.len(), flags, cp, cl, out.as_mut_ptr(), cap, &mut n) };
    (rc, String::from_utf8_lossy(&out[..n.min(cap)]).into_owned(), n)
}

#[test]
fn the_c_abi_returns_the_reasons_as_json() {
    let p = plan();
    let text = dumps_sorted(&p);
    assert_eq!(call(&text, 0b10, Some(&hash(&p)), 4096), (0, "[]".to_string(), 2));
    let (rc, out, _) = call(&text, 0b01, None, 4096);
    assert_eq!(rc, 0);
    assert_eq!(parse(&out).unwrap().as_arr().unwrap().len(), 3);
}

#[test]
fn the_c_abi_reports_a_malformed_plan_and_a_small_buffer() {
    let (rc, msg, _) = call("{not json", 0b10, None, 4096);
    assert_eq!(rc, -6);
    assert!(!msg.is_empty());
    let (rc, _, needed) = call(&dumps_sorted(&plan()), 0b01, None, 4);
    assert_eq!(rc, -4);
    assert!(needed > 4);
    let mut n = 0usize;
    assert_eq!(unsafe { rc_paper_check(std::ptr::null(), 0, 0, std::ptr::null(), 0, std::ptr::null_mut(), 0, &mut n) }, -1);
}

#[test]
fn an_order_value_that_is_zero_or_negative_is_malformed_whatever_its_quantity() {
    for usd in [0.0, -500.0] {
        let p = with_orders(vec![order("AAA", 100.0, usd, "BUY")], 13374.89);
        assert_eq!(paper_checks(&p, false, true, Some(&hash(&p))).unwrap(), vec!["malformed orders: ['AAA']"], "usd {usd}");
    }
}
