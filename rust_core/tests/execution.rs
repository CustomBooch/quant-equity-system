//! Golden tests: rust_core::execution against the Python reference (tools/make_exec_fixtures.py), plus the fail-closed differences.

use std::collections::BTreeMap;

use rust_core::execution::*;
use rust_core::json::{dumps_sorted, parse, Value};

fn fixtures() -> Value {
    parse(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/exec_reference.json")).unwrap()).unwrap()
}

fn map(v: &Value) -> BTreeMap<String, f64> {
    match v {
        Value::Obj(kv) => kv.iter().map(|(k, x)| (k.clone(), x.as_f64().unwrap())).collect(),
        _ => panic!("object expected"),
    }
}

fn s(v: &Value) -> String {
    dumps_sorted(v)
}

#[test]
fn plans_and_hashes_are_byte_identical_to_python() {
    let f = fixtures();
    let mut orders = 0;
    for (i, sc) in f.get("scenarios").unwrap().as_arr().unwrap().iter().enumerate() {
        let plan = make_plan(
            &map(sc.get("target").unwrap()),
            &map(sc.get("held").unwrap()),
            &map(sc.get("prices").unwrap()),
            sc.get("account").unwrap(),
            sc.get("fraction").unwrap().as_f64().unwrap(),
        );
        let want = sc.get("plan").unwrap();
        assert_eq!(s(&plan), s(want), "scenario {i}");
        orders += plan.get("orders").unwrap().as_arr().unwrap().len();
    }
    assert!(orders > 2000, "fixtures should contain many orders, got {orders}");
}

#[test]
fn pre_trade_reasons_match_python_including_tampered_plans() {
    let f = fixtures();
    let (mut with, mut without) = (0, 0);
    for (i, sc) in f.get("scenarios").unwrap().as_arr().unwrap().iter().enumerate() {
        let flags = sc.get("flags").unwrap().as_arr().unwrap();
        let (kill, go) = (matches!(flags[0], Value::Bool(true)), matches!(flags[1], Value::Bool(true)));
        let confirm = sc.get("confirm").unwrap().as_str();
        let got = pre_trade_checks(sc.get("plan").unwrap(), kill, go, confirm).unwrap();
        let want: Vec<String> = sc.get("reasons").unwrap().as_arr().unwrap().iter().map(|r| r.as_str().unwrap().to_string()).collect();
        assert_eq!(got, want, "scenario {i}");
        if want.is_empty() {
            without += 1;
        } else {
            with += 1;
        }
        let plan_hash = sc.get("plan").unwrap().get("plan_hash").unwrap().as_str().unwrap();
        let got_t = pre_trade_checks(sc.get("tampered").unwrap(), false, true, Some(plan_hash)).unwrap();
        let want_t: Vec<String> = sc.get("reasons_tampered").unwrap().as_arr().unwrap().iter().map(|r| r.as_str().unwrap().to_string()).collect();
        assert_eq!(got_t, want_t, "tampered scenario {i}");
    }
    assert!(with > 50 && without > 5, "need both outcomes: {with} with reasons, {without} clean");
}

#[test]
fn reconciliation_matches_python() {
    let f = fixtures();
    let mut diffs = 0;
    for (i, sc) in f.get("scenarios").unwrap().as_arr().unwrap().iter().enumerate() {
        let got = reconcile(sc.get("plan").unwrap(), &map(sc.get("after").unwrap()), &map(sc.get("held").unwrap()), 1e-3).unwrap();
        assert_eq!(s(&Value::Arr(got.clone())), s(sc.get("recon").unwrap()), "scenario {i}");
        diffs += got.len();
    }
    assert!(diffs > 100);
}

#[test]
fn tranche_targets_match_python() {
    for (i, t) in fixtures().get("targets").unwrap().as_arr().unwrap().iter().enumerate() {
        let got = tranche_target_shares(
            &map(t.get("weights").unwrap()),
            &map(t.get("prices").unwrap()),
            t.get("account").unwrap().as_f64().unwrap(),
            t.get("exposure").unwrap().as_f64().unwrap(),
            t.get("k").unwrap().as_f64().unwrap() as i64,
        )
        .unwrap();
        let want = map(t.get("out").unwrap());
        assert_eq!(got.len(), want.len());
        for (k, v) in &want {
            assert_eq!(got[k].to_bits(), v.to_bits(), "target case {i} {k}");
        }
    }
}

#[test]
fn calendar_and_tranches_match_python_for_22_years() {
    let mut slots = 0;
    for r in fixtures().get("dates").unwrap().as_arr().unwrap() {
        let (y, m, d) = parse_date(r.get("date").unwrap().as_str().unwrap()).unwrap();
        assert_eq!(weekday(y, m, d) as f64, r.get("weekday").unwrap().as_f64().unwrap(), "{y}-{m}-{d}");
        assert_eq!(iso_week(y, m, d) as f64, r.get("iso_week").unwrap().as_f64().unwrap(), "{y}-{m}-{d}");
        assert_eq!(is_slot(y, m, d), matches!(r.get("slot"), Some(Value::Bool(true))));
        if is_slot(y, m, d) {
            slots += 1;
            assert_eq!(tranche_of(y, m, d, K_TRANCHES).unwrap() as f64, r.get("tranche").unwrap().as_f64().unwrap());
        } else {
            assert!(tranche_of(y, m, d, K_TRANCHES).is_err());
        }
    }
    assert!(slots > 1100);
}

#[test]
fn overlay_matches_python_on_finite_inputs() {
    for o in fixtures().get("overlay").unwrap().as_arr().unwrap() {
        let g = overlay_exposure(o.get("close").unwrap().as_f64().unwrap(), o.get("mean").unwrap().as_f64().unwrap(), o.get("level").unwrap().as_f64().unwrap()).unwrap();
        assert_eq!(g, o.get("out").unwrap().as_f64().unwrap());
    }
}

// ---- deliberate fail-closed differences from the Python code ----

#[test]
fn nan_price_is_skipped_not_ordered() {
    let target = BTreeMap::from([("A_US_EQ".to_string(), 10.0)]);
    let prices = BTreeMap::from([("A_US_EQ".to_string(), f64::NAN)]);
    let plan = make_plan(&target, &BTreeMap::new(), &prices, &Value::Int(10000), 1.0);
    assert!(plan.get("orders").unwrap().as_arr().unwrap().is_empty());
    assert!(s(&plan).contains("invalid_price"));
}

#[test]
fn nan_market_data_does_not_default_to_full_exposure() {
    assert!(overlay_exposure(f64::NAN, 100.0, 0.5).is_err());
    assert!(overlay_exposure(90.0, f64::NAN, 0.5).is_err());
}

#[test]
fn malformed_plans_are_errors_not_passes() {
    assert!(pre_trade_checks(&parse("{}").unwrap(), false, true, None).is_err());
    assert!(pre_trade_checks(&parse("{\"orders\": [{\"ticker\": \"A_US_EQ\"}], \"account_value_usd\": 100, \"plan_hash\": \"x\"}").unwrap(), false, true, None).is_err());
}

#[test]
fn nan_order_value_counts_as_above_the_cap() {
    let plan = parse("{\"orders\": [{\"ticker\": \"A_US_EQ\", \"usd\": NaN, \"quantity\": 1.0, \"side\": \"BUY\"}], \"account_value_usd\": 1000, \"plan_hash\": \"x\"}").unwrap();
    let r = pre_trade_checks(&plan, false, true, Some("x")).unwrap();
    assert!(r.iter().any(|m| m.contains("above 10%")));
}

#[test]
fn invalid_calendar_dates_are_rejected() {
    assert!(parse_date("2026-02-30").is_err());
    assert!(parse_date("2026-13-01").is_err());
    assert!(parse_date("26-01-01").is_err());
}

// ---- fail-closed cases: Python (rank_signals.execution.plan) now behaves identically, so these are golden tests too ----

#[test]
fn non_finite_prices_and_quantities_give_the_same_plan_as_python() {
    for (i, c) in fixtures().get("nan_plans").unwrap().as_arr().unwrap().iter().enumerate() {
        let plan = make_plan(&map(c.get("target").unwrap()), &map(c.get("held").unwrap()), &map(c.get("prices").unwrap()), c.get("account").unwrap(), 1.0);
        assert_eq!(s(&plan), s(c.get("plan").unwrap()), "nan plan {i}");
    }
}

#[test]
fn non_finite_order_values_are_flagged_like_python() {
    for (i, c) in fixtures().get("nan_orders").unwrap().as_arr().unwrap().iter().enumerate() {
        let plan = c.get("plan").unwrap();
        let hash = plan.get("plan_hash").unwrap().as_str().unwrap();
        let got = pre_trade_checks(plan, false, true, Some(hash)).unwrap();
        let want: Vec<String> = c.get("reasons").unwrap().as_arr().unwrap().iter().map(|r| r.as_str().unwrap().to_string()).collect();
        assert_eq!(got, want, "nan order {i}");
    }
}

#[test]
fn non_finite_market_data_is_refused_like_python() {
    for c in fixtures().get("overlay_nonfinite").unwrap().as_arr().unwrap() {
        let r = overlay_exposure(c.get("close").unwrap().as_f64().unwrap(), c.get("mean").unwrap().as_f64().unwrap(), 0.5);
        assert!(r.is_err() == matches!(c.get("raises"), Some(Value::Bool(true))));
    }
}


#[test]
fn alpaca_symbols_are_plain_capitals_with_an_optional_share_class() {
    use rust_core::execution::alpaca_symbol;
    for ok in ["A", "AAPL", "GOOGL", "BRK.B", "MOG.A"] {
        assert!(alpaca_symbol(ok), "{ok}");
    }
    for bad in ["", "aapl", "AAPL_US_EQ", "TOOLONG", "BRK.", ".B", "BRK.BBB", "BR K", "BRK-B", "A.B.C"] {
        assert!(!alpaca_symbol(bad), "{bad}");
    }
}

fn alpaca_plan(tickers: &[&str], broker: &str) -> Value {
    let orders: Vec<String> = tickers.iter().map(|t| format!("{{\"ticker\": \"{t}\", \"quantity\": 1.0, \"usd\": 100.0, \"side\": \"BUY\"}}")).collect();
    let body = format!("{{\"orders\": [{}], \"account_value_usd\": 10000}}", orders.join(", "));
    let mut plan = parse(&body).unwrap();
    let h = rust_core::execution::plan_hash(plan.get("orders").unwrap().as_arr().unwrap(), plan.get("account_value_usd").unwrap());
    let with = format!("{{\"orders\": [{}], \"account_value_usd\": 10000, \"plan_hash\": \"{h}\", \"broker\": \"{broker}\", \"held\": {{}}}}", orders.join(", "));
    plan = parse(&with).unwrap();
    plan
}

#[test]
fn an_alpaca_plan_of_valid_symbols_passes_and_bad_symbols_are_named() {
    use rust_core::execution::paper_checks;
    let p = alpaca_plan(&["AAPL", "BRK.B"], "alpaca");
    let h = p.get("plan_hash").unwrap().as_str().unwrap().to_string();
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), Vec::<String>::new());
    let q = alpaca_plan(&["AAPL", "AAPL_US_EQ", "brk"], "alpaca");
    let hq = q.get("plan_hash").unwrap().as_str().unwrap().to_string();
    assert_eq!(paper_checks(&q, false, true, Some(&hq)).unwrap(), vec!["not Alpaca US equity symbols: ['AAPL_US_EQ', 'brk']".to_string()]);
}

#[test]
fn a_plan_for_any_broker_but_alpaca_or_for_none_is_an_error() {
    use rust_core::execution::paper_checks;
    for broker in ["ibkr", "other"] {
        let q = alpaca_plan(&["AAPL"], broker);
        let hq = q.get("plan_hash").unwrap().as_str().unwrap().to_string();
        assert!(paper_checks(&q, false, true, Some(&hq)).unwrap_err().contains("unknown broker"), "{broker}");
    }
    let none = parse("{\"orders\": [], \"account_value_usd\": 100, \"plan_hash\": \"x\"}").unwrap();
    assert!(paper_checks(&none, false, true, Some("x")).unwrap_err().contains("names no broker"));
}

fn signed_plan(orders: &[(&str, f64, f64)], held: &[(&str, f64)], short_fraction: Option<f64>) -> (Value, String) {
    levered_plan(orders, held, short_fraction, None)
}

fn levered_plan(orders: &[(&str, f64, f64)], held: &[(&str, f64)], short_fraction: Option<f64>, leverage: Option<f64>) -> (Value, String) {
    let os: Vec<String> = orders
        .iter()
        .map(|(t, q, usd)| format!("{{\"ticker\": \"{t}\", \"quantity\": {q:?}, \"usd\": {usd:?}, \"side\": \"{}\"}}", if *q > 0.0 { "BUY" } else { "SELL" }))
        .collect();
    let body = format!("{{\"orders\": [{}], \"account_value_usd\": 100000}}", os.join(", "));
    let tmp = parse(&body).unwrap();
    let h = plan_hash(tmp.get("orders").unwrap().as_arr().unwrap(), tmp.get("account_value_usd").unwrap());
    let hs: Vec<String> = held.iter().map(|(t, q)| format!("\"{t}\": {q:?}")).collect();
    let sf = short_fraction.map(|s| format!(", \"short_fraction\": {s:?}")).unwrap_or_default();
    let sf = sf + &leverage.map(|k| format!(", \"leverage\": {k:?}")).unwrap_or_default();
    let full = format!("{{\"orders\": [{}], \"account_value_usd\": 100000, \"plan_hash\": \"{h}\", \"broker\": \"alpaca\", \"held\": {{{}}}{sf}}}", os.join(", "), hs.join(", "));
    (parse(&full).unwrap(), h)
}

#[test]
fn a_130_30_plan_with_whole_share_shorts_passes() {
    let (p, h) = signed_plan(&[("AAA", 10.0, 1000.0), ("ZZZ", -5.0, 500.0)], &[], Some(0.3));
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), Vec::<String>::new());
}

#[test]
fn short_sales_need_a_declared_fraction_and_whole_shares() {
    let (p, h) = signed_plan(&[("ZZZ", -5.0, 500.0)], &[], None);
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), vec!["short sales without a declared short fraction: ['ZZZ']".to_string()]);
    let (q, hq) = signed_plan(&[("ZZZ", -2.5, 250.0)], &[], Some(0.2));
    assert_eq!(paper_checks(&q, false, true, Some(&hq)).unwrap(), vec!["short sales that are not whole shares: ['ZZZ']".to_string()]);
}

#[test]
fn selling_a_long_holding_down_to_zero_is_not_a_short_sale() {
    let (p, h) = signed_plan(&[("AAA", -2.5, 250.0)], &[("AAA", 2.5)], None);
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), Vec::<String>::new());
}

#[test]
fn the_short_fraction_is_bounded_and_the_buy_limit_follows_it() {
    let (p, h) = signed_plan(&[("AAA", 1.0, 100.0)], &[], Some(0.5));
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), vec!["short_fraction 0.5 outside [0, 0.3]".to_string()]);
    let buys: Vec<(String, f64, f64)> = (b'A'..=b'M').map(|c| (format!("B{}", c as char), 1.0, 9900.0)).collect();
    let refs: Vec<(&str, f64, f64)> = buys.iter().map(|(t, q, u)| (t.as_str(), *q, *u)).collect();
    let (q, hq) = signed_plan(&refs, &[], Some(0.3));
    assert_eq!(paper_checks(&q, false, true, Some(&hq)).unwrap(), Vec::<String>::new());
    let (r, hr) = signed_plan(&refs, &[], Some(0.2));
    assert_eq!(paper_checks(&r, false, true, Some(&hr)).unwrap(), vec!["buys total 128700.00 USD, more than 120000.00 ((1 + short fraction) x the account)".to_string()]);
}

#[test]
fn short_sales_above_the_tranche_cap_are_refused() {
    let shorts: Vec<(String, f64, f64)> = (b'A'..=b'K').map(|c| (format!("S{}", c as char), -10.0, 1000.0)).collect();
    let refs: Vec<(&str, f64, f64)> = shorts.iter().map(|(t, q, u)| (t.as_str(), *q, *u)).collect();
    let (p, h) = signed_plan(&refs, &[], Some(0.3));
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), vec!["short sales total 11000.00 USD, more than 10500.00".to_string()]);
}

#[test]
fn a_levered_plan_within_the_gross_cap_raises_the_buy_limit_to_k_times_one_plus_s() {
    // k = 2 / (1 + 2 x 0.2) = 1.428..., buy limit 1.714... x 100,000; 17 buys of 9,900 = 168,300 pass, 18 = 178,200 do not
    let k = 2.0 / 1.4;
    let buys: Vec<(String, f64, f64)> = (b'A'..=b'R').map(|c| (format!("B{}", c as char), 1.0, 9900.0)).collect();
    let refs: Vec<(&str, f64, f64)> = buys.iter().map(|(t, q, u)| (t.as_str(), *q, *u)).collect();
    let (p, h) = levered_plan(&refs[..17], &[], Some(0.2), Some(k));
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), Vec::<String>::new());
    let (q, hq) = levered_plan(&refs, &[], Some(0.2), Some(k));
    assert_eq!(paper_checks(&q, false, true, Some(&hq)).unwrap(), vec!["buys total 178200.00 USD, more than 171428.57 (leverage x (1 + short fraction) x the account)".to_string()]);
}

#[test]
fn leverage_beyond_the_gross_cap_or_below_one_is_refused() {
    let (p, h) = levered_plan(&[("AAA", 1.0, 100.0)], &[], Some(0.3), Some(1.3));
    assert_eq!(paper_checks(&p, false, true, Some(&h)).unwrap(), vec!["leverage 1.3 outside [1, 2 / (1 + 2 x short fraction)]".to_string()]);
    let (q, hq) = levered_plan(&[("AAA", 1.0, 100.0)], &[], Some(0.2), Some(0.5));
    assert_eq!(paper_checks(&q, false, true, Some(&hq)).unwrap(), vec!["leverage 0.5 outside [1, 2 / (1 + 2 x short fraction)]".to_string()]);
}

#[test]
fn a_nan_infinite_or_non_positive_account_value_is_a_reason_not_to_send() {
    for acct in ["NaN", "Infinity", "0", "-5"] {
        let plan = parse(&format!("{{\"orders\": [{{\"ticker\": \"A_US_EQ\", \"usd\": 1e12, \"quantity\": 1.0, \"side\": \"BUY\"}}], \"account_value_usd\": {acct}, \"plan_hash\": \"x\"}}")).unwrap();
        let r = pre_trade_checks(&plan, false, true, Some("x")).unwrap();
        assert!(r.iter().any(|m| m.contains("not a finite positive number")), "{acct}: {r:?}");
    }
}
