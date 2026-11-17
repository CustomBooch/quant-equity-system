//! Cell extraction from the XML parts of an .xlsx workbook, equal to the ElementTree reader in lab/ciqfill/xlsx.py.
//!
//! Python unzips the parts (zlib) and passes the XML bytes; this module tokenises them. It mirrors what the ElementTree reader sees:
//! * text is decoded like Expat: the five predefined entities and numeric character references; literal CR LF and lone CR become LF before references are
//!   expanded (so `&#13;` stays a CR); CDATA is taken verbatim; comments, processing instructions and declarations are skipped;
//! * a sheet's cells are the `c` children of the `row` children of the first `sheetData` child of the root; a cell's value is the text of its first `v`
//!   child (none when the `v` is absent or empty); an inline string is the concatenation of every `t` element below the cell;
//! * a shared string is the concatenation of every `t` element below one `si` child of the root.
//! Element names are matched by local name (any namespace prefix). Anything else (another encoding, an unknown entity, malformed markup) is an error and the
//! Python caller falls back to ElementTree for that file.
//!
//! Output, one UTF-8 buffer: for a sheet, records `R<row number>` and `C<column letters>\x1f<kind>\x1f<text>` separated by `\x1e`, kind `N` (no value),
//! `V` (value as written), `S` (shared-string index), `I` (inline string); for shared strings, the strings separated by `\x1e`. XML 1.0 cannot carry \x1e or
//! \x1f, so the separators are unambiguous.

pub const REC: u8 = 0x1e;
pub const FLD: u8 = 0x1f;

struct Lexer<'a> {
    b: &'a [u8],
    i: usize,
}

enum Tok<'a> {
    Start { name: &'a [u8], attrs: &'a [u8], empty: bool },
    End,
    Text(&'a [u8]),
    CData(&'a [u8]),
}

fn find(b: &[u8], from: usize, pat: &[u8]) -> Option<usize> {
    if pat.is_empty() || from > b.len() {
        return None;
    }
    b[from..].windows(pat.len()).position(|w| w == pat).map(|p| p + from)
}

impl<'a> Lexer<'a> {
    fn new(b: &'a [u8]) -> Result<Self, String> {
        let mut i = 0;
        if b.starts_with(&[0xEF, 0xBB, 0xBF]) {
            i = 3;
        }
        if b[i..].starts_with(b"<?xml") {
            let end = find(b, i, b"?>").ok_or("unterminated XML declaration")?;
            let decl = std::str::from_utf8(&b[i..end]).map_err(|_| "declaration is not UTF-8")?.to_ascii_lowercase();
            if let Some(p) = decl.find("encoding") {
                let rest = &decl[p..];
                if !(rest.contains("utf-8") || rest.contains("utf8")) {
                    return Err("encoding other than UTF-8".into());
                }
            }
            i = end + 2;
        }
        Ok(Lexer { b, i })
    }

    fn next(&mut self) -> Result<Option<Tok<'a>>, String> {
        let b = self.b;
        if self.i >= b.len() {
            return Ok(None);
        }
        if b[self.i] != b'<' {
            let end = b[self.i..].iter().position(|&c| c == b'<').map_or(b.len(), |p| p + self.i);
            let t = &b[self.i..end];
            self.i = end;
            return Ok(Some(Tok::Text(t)));
        }
        let rest = &b[self.i..];
        if rest.starts_with(b"<!--") {
            self.i = find(b, self.i + 4, b"-->").ok_or("unterminated comment")? + 3;
            return self.next();
        }
        if rest.starts_with(b"<![CDATA[") {
            let end = find(b, self.i + 9, b"]]>").ok_or("unterminated CDATA")?;
            let t = &b[self.i + 9..end];
            self.i = end + 3;
            return Ok(Some(Tok::CData(t)));
        }
        if rest.starts_with(b"<?") {
            self.i = find(b, self.i + 2, b"?>").ok_or("unterminated processing instruction")? + 2;
            return self.next();
        }
        if rest.starts_with(b"<!") {
            return Err("DOCTYPE or other declaration inside the document".into());
        }
        // tag: find the closing '>' outside quotes
        let mut j = self.i + 1;
        let mut quote = 0u8;
        while j < b.len() {
            let c = b[j];
            if quote != 0 {
                if c == quote {
                    quote = 0;
                }
            } else if c == b'"' || c == b'\'' {
                quote = c;
            } else if c == b'>' {
                break;
            }
            j += 1;
        }
        if j >= b.len() {
            return Err("unterminated tag".into());
        }
        let inner = &b[self.i + 1..j];
        self.i = j + 1;
        if inner.first() == Some(&b'/') {
            return Ok(Some(Tok::End));
        }
        let empty = inner.last() == Some(&b'/');
        let inner = if empty { &inner[..inner.len() - 1] } else { inner };
        let n = inner.iter().position(|c| c.is_ascii_whitespace()).unwrap_or(inner.len());
        if n == 0 {
            return Err("tag without a name".into());
        }
        Ok(Some(Tok::Start { name: &inner[..n], attrs: &inner[n..], empty }))
    }
}

fn local(name: &[u8]) -> &[u8] {
    name.iter().rposition(|&c| c == b':').map_or(name, |p| &name[p + 1..])
}

/// Value of attribute `key` (exact name, no prefix), decoded.
fn attr(attrs: &[u8], key: &[u8]) -> Result<Option<String>, String> {
    let mut i = 0;
    while i < attrs.len() {
        while i < attrs.len() && attrs[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= attrs.len() {
            break;
        }
        let ns = i;
        while i < attrs.len() && attrs[i] != b'=' && !attrs[i].is_ascii_whitespace() {
            i += 1;
        }
        let name = &attrs[ns..i];
        while i < attrs.len() && (attrs[i].is_ascii_whitespace() || attrs[i] == b'=') {
            i += 1;
        }
        if i >= attrs.len() || (attrs[i] != b'"' && attrs[i] != b'\'') {
            return Err("malformed attribute".into());
        }
        let q = attrs[i];
        let vs = i + 1;
        let ve = attrs[vs..].iter().position(|&c| c == q).ok_or("unterminated attribute value")? + vs;
        if name == key {
            let mut s = Vec::new();
            decode(&attrs[vs..ve], &mut s)?;
            return Ok(Some(String::from_utf8(s).map_err(|_| "attribute is not UTF-8")?));
        }
        i = ve + 1;
    }
    Ok(None)
}

/// Append decoded character data: line endings normalised first, then references expanded.
fn decode(t: &[u8], out: &mut Vec<u8>) -> Result<(), String> {
    let mut i = 0;
    while i < t.len() {
        match t[i] {
            b'\r' => {
                out.push(b'\n');
                i += if t.get(i + 1) == Some(&b'\n') { 2 } else { 1 };
            }
            b'&' => {
                let end = t[i..].iter().position(|&c| c == b';').ok_or("unterminated reference")? + i;
                let r = &t[i + 1..end];
                match r {
                    b"lt" => out.push(b'<'),
                    b"gt" => out.push(b'>'),
                    b"amp" => out.push(b'&'),
                    b"quot" => out.push(b'"'),
                    b"apos" => out.push(b'\''),
                    _ if r.first() == Some(&b'#') => {
                        let s = std::str::from_utf8(&r[1..]).map_err(|_| "bad character reference")?;
                        let cp = if let Some(h) = s.strip_prefix('x') { u32::from_str_radix(h, 16) } else { s.parse::<u32>() }.map_err(|_| "bad character reference")?;
                        let ch = char::from_u32(cp).ok_or("bad character reference")?;
                        if (cp < 0x20 && cp != 0x9 && cp != 0xA && cp != 0xD) || cp == 0xFFFE || cp == 0xFFFF {
                            return Err("character reference not allowed in XML 1.0".into());
                        }
                        let mut buf = [0u8; 4];
                        out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                    }
                    _ => return Err("unknown entity".into()),
                }
                i = end + 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    Ok(())
}

fn cdata(t: &[u8], out: &mut Vec<u8>) {
    let mut i = 0;
    while i < t.len() {
        if t[i] == b'\r' {
            out.push(b'\n');
            i += if t.get(i + 1) == Some(&b'\n') { 2 } else { 1 };
        } else {
            out.push(t[i]);
            i += 1;
        }
    }
}

/// Cells of one worksheet part, encoded as described in the module documentation.
pub fn sheet_cells(xml: &[u8]) -> Result<Vec<u8>, String> {
    let mut lx = Lexer::new(xml)?;
    let mut out = Vec::with_capacity(xml.len() / 2);
    let mut stack: Vec<&[u8]> = Vec::new();
    let mut sheetdata_seen = false;
    let mut in_sheetdata = false;
    // current cell
    let mut cell_col: Option<Vec<u8>> = None;
    let mut cell_type: Option<String> = None;
    let mut v_seen = false;
    let mut v_text: Vec<u8> = Vec::new();
    let mut v_has_text = false;
    let mut in_first_v = false;
    let mut inline: Vec<u8> = Vec::new();
    let mut in_t = 0usize;
    let mut t_children = false;
    while let Some(tok) = lx.next()? {
        match tok {
            Tok::Start { name, attrs, empty } => {
                let ln = local(name);
                let depth = stack.len();
                if depth == 1 && ln == b"sheetData" && !sheetdata_seen {
                    sheetdata_seen = true;
                    in_sheetdata = true;
                } else if in_sheetdata && depth == 2 && ln == b"row" {
                    let r = attr(attrs, b"r")?.ok_or("row without r")?;
                    let r: i64 = r.trim().parse().map_err(|_| "row r is not an integer")?;
                    out.push(b'R');
                    out.extend_from_slice(r.to_string().as_bytes());
                    out.push(REC);
                } else if in_sheetdata && depth == 3 && ln == b"c" && stack[2] == b"row" {
                    let r = attr(attrs, b"r")?.ok_or("cell without r")?;
                    let col: Vec<u8> = r.bytes().take_while(|c| c.is_ascii_uppercase()).collect();
                    if col.is_empty() {
                        return Err("cell reference without column letters".into());
                    }
                    cell_col = Some(col);
                    cell_type = attr(attrs, b"t")?;
                    v_seen = false;
                    v_has_text = false;
                    v_text.clear();
                    inline.clear();
                } else if cell_col.is_some() && depth == 4 && ln == b"v" && !v_seen {
                    v_seen = true;
                    in_first_v = !empty;
                } else if cell_col.is_some() && depth >= 4 && ln == b"t" {
                    if in_t > 0 {
                        t_children = true;
                    }
                    in_t += 1;
                } else if in_first_v || in_t > 0 {
                    // a child element: ElementTree's .text stops at the first child
                    if in_first_v {
                        in_first_v = false;
                    }
                    if in_t > 0 {
                        t_children = true;
                    }
                }
                if !empty {
                    stack.push(ln);
                } else if cell_col.is_some() && depth == 3 && ln == b"c" {
                    emit_cell(&mut out, &mut cell_col, &cell_type, v_seen, v_has_text, &v_text, &inline);
                } else if ln == b"t" && cell_col.is_some() && depth >= 4 {
                    in_t -= 1;
                    t_children = false;
                }
            }
            Tok::End => {
                let ln = stack.pop().ok_or("unbalanced end tag")?;
                let depth = stack.len();
                if in_sheetdata && depth == 1 && ln == b"sheetData" {
                    in_sheetdata = false;
                } else if cell_col.is_some() && depth == 3 && ln == b"c" {
                    emit_cell(&mut out, &mut cell_col, &cell_type, v_seen, v_has_text, &v_text, &inline);
                } else if in_first_v && depth == 4 && ln == b"v" {
                    in_first_v = false;
                } else if in_t > 0 && ln == b"t" {
                    in_t -= 1;
                    t_children = false;
                }
            }
            Tok::Text(t) | Tok::CData(t) => {
                let is_cdata = matches!(tok, Tok::CData(_));
                if in_first_v && stack.last() == Some(&&b"v"[..]) {
                    if !t.is_empty() {
                        v_has_text = true;
                    }
                    if is_cdata { cdata(t, &mut v_text) } else { decode(t, &mut v_text)? }
                } else if in_t > 0 && !t_children && stack.last() == Some(&&b"t"[..]) {
                    if is_cdata { cdata(t, &mut inline) } else { decode(t, &mut inline)? }
                }
            }
        }
    }
    if !stack.is_empty() {
        return Err("unclosed elements".into());
    }
    if !sheetdata_seen {
        return Err("no sheetData".into());
    }
    Ok(out)
}

fn emit_cell(out: &mut Vec<u8>, col: &mut Option<Vec<u8>>, ty: &Option<String>, v_seen: bool, v_has_text: bool, v_text: &[u8], inline: &[u8]) {
    let c = col.take().expect("cell");
    out.push(b'C');
    out.extend_from_slice(&c);
    out.push(FLD);
    let has_v = v_seen && v_has_text;
    match ty.as_deref() {
        Some("inlineStr") => {
            out.push(b'I');
            out.push(FLD);
            out.extend_from_slice(inline);
        }
        Some("s") if has_v => {
            out.push(b'S');
            out.push(FLD);
            out.extend_from_slice(v_text);
        }
        _ if has_v => {
            out.push(b'V');
            out.push(FLD);
            out.extend_from_slice(v_text);
        }
        _ => {
            out.push(b'N');
            out.push(FLD);
        }
    }
    out.push(REC);
}

/// The shared strings of `xl/sharedStrings.xml`, separated by \x1e; also returns their number.
pub fn shared_strings(xml: &[u8]) -> Result<(Vec<u8>, usize), String> {
    let mut lx = Lexer::new(xml)?;
    let mut out = Vec::with_capacity(xml.len() / 2);
    let mut stack: Vec<&[u8]> = Vec::new();
    let mut n = 0usize;
    let mut in_si = false;
    let mut in_t = 0usize;
    let mut t_children = false;
    while let Some(tok) = lx.next()? {
        match tok {
            Tok::Start { name, empty, .. } => {
                let ln = local(name);
                let depth = stack.len();
                if depth == 1 && ln == b"si" {
                    if n > 0 {
                        out.push(REC);
                    }
                    n += 1;
                    in_si = !empty;
                } else if in_si && ln == b"t" {
                    if in_t > 0 {
                        t_children = true;
                    }
                    if !empty {
                        in_t += 1;
                    }
                } else if in_t > 0 {
                    t_children = true;
                }
                if !empty {
                    stack.push(ln);
                }
            }
            Tok::End => {
                let ln = stack.pop().ok_or("unbalanced end tag")?;
                if stack.len() == 1 && ln == b"si" {
                    in_si = false;
                } else if in_t > 0 && ln == b"t" {
                    in_t -= 1;
                    t_children = false;
                }
            }
            Tok::Text(t) | Tok::CData(t) => {
                if in_t > 0 && !t_children && stack.last() == Some(&&b"t"[..]) {
                    if matches!(tok, Tok::CData(_)) { cdata(t, &mut out) } else { decode(t, &mut out)? }
                }
            }
        }
    }
    if !stack.is_empty() {
        return Err("unclosed elements".into());
    }
    Ok((out, n))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(b: &[u8]) -> String {
        String::from_utf8(b.to_vec()).unwrap().replace('\u{1e}', "|").replace('\u{1f}', "^")
    }

    #[test]
    fn cells_kinds_entities_and_line_endings() {
        let x = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\r\n<worksheet xmlns=\"m\"><sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>3</v></c><c r=\"B1\"><v>1.5</v></c><c r=\"C1\"/><c r=\"D1\" t=\"inlineStr\"><is><t>a&amp;b\r\nc</t><r><t>&#13;d</t></r></is></c><c r=\"E1\"><v></v></c></row><row r=\"3\"/></sheetData></worksheet>";
        assert_eq!(s(&sheet_cells(x).unwrap()), "R1|CA^S^3|CB^V^1.5|CC^N^|CD^I^a&b\nc\rd|CE^N^|R3|");
    }

    #[test]
    fn shared_strings_join_rich_text_runs() {
        let x = b"<sst><si><t>plain</t></si><si><r><t>a</t></r><r><t xml:space=\"preserve\"> b</t></r></si><si><t/></si></sst>";
        let (o, n) = shared_strings(x).unwrap();
        assert_eq!(n, 3);
        assert_eq!(s(&o), "plain|a b|");
    }

    #[test]
    fn prefixed_names_and_refusals() {
        let x = b"<x:worksheet xmlns:x=\"m\"><x:sheetData><x:row r=\"2\"><x:c r=\"AB2\"><x:v>7</x:v></x:c></x:row></x:sheetData></x:worksheet>";
        assert_eq!(s(&sheet_cells(x).unwrap()), "R2|CAB^V^7|");
        assert!(sheet_cells(b"<?xml version=\"1.0\" encoding=\"UTF-16\"?><a/>").is_err());
        assert!(sheet_cells(b"<w><sheetData><row r=\"1\"><c r=\"A1\"><v>&nbsp;</v></c></row></sheetData></w>").is_err());
    }
}
