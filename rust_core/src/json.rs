//! Minimal JSON reader/writer. Keeps the int/float distinction and writes exactly what Python's `json.dumps(obj, sort_keys=True)` writes
//! (separators ", " and ": ", `ensure_ascii`, float `repr`), so a payload hashed in either language gives the same digest.

use crate::pyfmt::repr_f64;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Int(i) => Some(*i as f64),
            Value::Float(f) => Some(*f),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        if let Value::Str(s) = self {
            Some(s)
        } else {
            None
        }
    }
    pub fn as_arr(&self) -> Option<&[Value]> {
        if let Value::Arr(a) = self {
            Some(a)
        } else {
            None
        }
    }
    pub fn obj<const N: usize>(items: [(&str, Value); N]) -> Value {
        Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }
}

pub fn parse(s: &str) -> Result<Value, String> {
    let mut p = Parser { b: s.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(format!("trailing characters at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn lit(&mut self, word: &str, v: Value) -> Result<Value, String> {
        if self.b[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            Err(format!("bad literal at byte {}", self.i))
        }
    }

    fn value(&mut self) -> Result<Value, String> {
        self.ws();
        match self.b.get(self.i) {
            None => Err("unexpected end".into()),
            Some(b'n') => self.lit("null", Value::Null),
            Some(b't') => self.lit("true", Value::Bool(true)),
            Some(b'f') => self.lit("false", Value::Bool(false)),
            Some(b'N') => self.lit("NaN", Value::Float(f64::NAN)),
            Some(b'I') => self.lit("Infinity", Value::Float(f64::INFINITY)),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b'[') => {
                self.i += 1;
                let mut a = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Ok(Value::Arr(a));
                }
                loop {
                    a.push(self.value()?);
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(Value::Arr(a));
                        }
                        _ => return Err(format!("expected , or ] at byte {}", self.i)),
                    }
                }
            }
            Some(b'{') => {
                self.i += 1;
                let mut o = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Ok(Value::Obj(o));
                }
                loop {
                    self.ws();
                    let k = self.string()?;
                    self.ws();
                    if self.b.get(self.i) != Some(&b':') {
                        return Err(format!("expected : at byte {}", self.i));
                    }
                    self.i += 1;
                    o.push((k, self.value()?));
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(Value::Obj(o));
                        }
                        _ => return Err(format!("expected , or }} at byte {}", self.i)),
                    }
                }
            }
            Some(b'-') if self.b[self.i..].starts_with(b"-Infinity") => {
                self.i += 9;
                Ok(Value::Float(f64::NEG_INFINITY))
            }
            Some(_) => self.number(),
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        while self.i < self.b.len() && matches!(self.b[self.i], b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') {
            self.i += 1;
        }
        let t = std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?;
        if t.is_empty() {
            return Err(format!("unexpected character at byte {start}"));
        }
        if t.contains(['.', 'e', 'E']) {
            t.parse::<f64>().map(Value::Float).map_err(|e| format!("{t}: {e}"))
        } else {
            t.parse::<i64>().map(Value::Int).or_else(|_| t.parse::<f64>().map(Value::Float)).map_err(|e| format!("{t}: {e}"))
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.b.get(self.i..self.i + 4).ok_or("short \\u escape")?;
        self.i += 4;
        u32::from_str_radix(std::str::from_utf8(h).map_err(|e| e.to_string())?, 16).map_err(|e| e.to_string())
    }

    fn string(&mut self) -> Result<String, String> {
        if self.b.get(self.i) != Some(&b'"') {
            return Err(format!("expected string at byte {}", self.i));
        }
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = *self.b.get(self.i).ok_or("unterminated string")?;
            self.i += 1;
            match c {
                b'"' => return String::from_utf8(out).map_err(|e| e.to_string()),
                b'\\' => {
                    let e = *self.b.get(self.i).ok_or("bad escape")?;
                    self.i += 1;
                    let ch = match e {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&hi) && self.b[self.i..].starts_with(b"\\u") {
                                self.i += 2;
                                let lo = self.hex4()?;
                                0x10000 + ((hi - 0xD800) << 10) + (lo.wrapping_sub(0xDC00))
                            } else {
                                hi
                            };
                            char::from_u32(cp).ok_or("invalid code point")?
                        }
                        _ => return Err("bad escape".into()),
                    };
                    let mut buf = [0u8; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                }
                c => out.push(c),
            }
        }
    }
}

fn dump_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (' '..='~').contains(&c) => out.push(c),
            c => {
                let mut u = [0u16; 2];
                for unit in c.encode_utf16(&mut u) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn dump(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::Float(f) => out.push_str(&if f.is_nan() {
            "NaN".to_string()
        } else if f.is_infinite() {
            if *f > 0.0 { "Infinity" } else { "-Infinity" }.to_string()
        } else {
            repr_f64(*f)
        }),
        Value::Str(s) => dump_str(s, out),
        Value::Arr(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump(x, out);
            }
            out.push(']');
        }
        Value::Obj(kv) => {
            let mut sorted: Vec<&(String, Value)> = kv.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, x)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump_str(k, out);
                out.push_str(": ");
                dump(x, out);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(v, sort_keys=True)` as Python writes it.
pub fn dumps_sorted(v: &Value) -> String {
    let mut s = String::new();
    dump(v, &mut s);
    s
}
