//! Python-compatible number and string formatting, needed so hashes of JSON payloads agree with the Python implementation byte for byte.

/// Python `round(x, n)` for a float: correctly rounded decimal, ties to even on the exact binary value.
pub fn round_py(x: f64, n: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.n$}").parse().expect("formatted float parses")
}

/// Python `repr(float)` (shortest round-trip digits; positional for -4 < decpt <= 16, else scientific with a two-digit exponent).
pub fn repr_f64(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let sci = format!("{:e}", x.abs()); // shortest digits, e.g. "1.2345e-5"
    let (mant, exp) = sci.split_once('e').expect("scientific format");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp.parse::<i32>().expect("exponent") + 1;
    let nd = digits.len() as i32;
    let body = if -4 < decpt && decpt <= 16 {
        if decpt <= 0 {
            format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
        } else if decpt >= nd {
            format!("{}{}.0", digits, "0".repeat((decpt - nd) as usize))
        } else {
            format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
        }
    } else {
        let e = decpt - 1;
        let m = if nd > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits.clone() };
        format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs())
    };
    if x < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}

/// Python `repr(str)` for the strings used here (tickers): single quotes unless the text has a single and no double quote.
pub fn repr_str(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

/// Python `repr(list_of_str)`.
pub fn repr_str_list(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|s| repr_str(s)).collect::<Vec<_>>().join(", "))
}
