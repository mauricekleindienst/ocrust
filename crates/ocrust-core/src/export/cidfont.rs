//! A Unicode-capable font for PDF text layers.
//!
//! The base-14 font both PDF writers use covers WinAnsi (CP1252), which is
//! Western European and nothing else. Greek, Cyrillic, CJK and everything past
//! `U+00FF` had to be written as `?`, so a searchable PDF of a Japanese scan was
//! searchable only in theory.
//!
//! This module supplies the alternative: a Type0 font with Identity-H encoding
//! whose two-byte codes *are* UTF-16 code units, plus a `ToUnicode` CMap that
//! says so. Nothing is embedded, because nothing is drawn — the layer is written
//! in rendering mode 3, so no glyph outline is ever needed, only the positions
//! and the text behind them. That keeps the wheel free of a bundled font file
//! and its license, and keeps the added objects to about nine kilobytes.

/// Resource name for the Unicode font in a page's `/Font` dictionary.
pub(crate) const UNICODE_FONT_NAME: &str = "OcrU";

/// PostScript name of the font. Nothing paints it, so it names what it is.
pub(crate) const UNICODE_BASE_FONT: &str = "OCRustGlyphless";

/// Average advance per character, in em, used to stretch a line onto its box.
///
/// The CID font declares `/DW 1000`, so every character is one em wide.
pub(crate) const UNICODE_EM_PER_CHAR: f32 = 1.0;

/// True when `text` holds a character WinAnsi cannot represent.
pub(crate) fn needs_unicode(text: &str) -> bool {
    text.chars().any(|c| super::pdf::winansi_byte(c).is_none())
}

/// UTF-16BE hex digits for a PDF hex string, without the angle brackets.
///
/// Characters outside the basic plane become their surrogate pair, which the
/// identity `ToUnicode` map hands back unchanged — UTF-16 reassembles them.
pub(crate) fn utf16_hex(text: &str) -> String {
    let mut out = String::with_capacity(text.len() * 4);
    let mut buf = [0u16; 2];
    for ch in text.chars() {
        for unit in ch.encode_utf16(&mut buf) {
            out.push_str(&format!("{unit:04X}"));
        }
    }
    out
}

/// The `ToUnicode` CMap: code point *is* the UTF-16 code unit.
///
/// Written as 256 ranges rather than one, because a `bfrange` whose ends differ
/// in more than the low byte is outside what the specification promises and
/// some extractors quietly ignore it. Sections are kept to 100 entries, which
/// the specification requires.
pub(crate) fn to_unicode_cmap() -> String {
    let mut out = String::with_capacity(12 * 1024);
    out.push_str(
        "/CIDInit /ProcSet findresource begin\n\
         12 dict begin\n\
         begincmap\n\
         /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n\
         /CMapName /OCRust-Identity-UCS def\n\
         /CMapType 2 def\n\
         1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n",
    );
    for chunk in (0..=0xFFu32).collect::<Vec<_>>().chunks(100) {
        out.push_str(&format!("{} beginbfrange\n", chunk.len()));
        for high in chunk {
            let lo = high << 8;
            out.push_str(&format!("<{lo:04X}> <{:04X}> <{lo:04X}>\n", lo + 0xFF));
        }
        out.push_str("endbfrange\n");
    }
    out.push_str(
        "endcmap\n\
         CMapName currentdict /CMap defineresource pop\n\
         end\nend\n",
    );
    out
}

/// Font descriptor entries shared by both writers.
///
/// A CIDFontType2 needs a descriptor even when no font file follows it; these
/// are the metrics of a generic sans-serif face.
pub(crate) const DESCRIPTOR_FLAGS: i64 = 4; // symbolic
pub(crate) const DESCRIPTOR_BBOX: [i64; 4] = [-200, -250, 1200, 900];
pub(crate) const DESCRIPTOR_ASCENT: i64 = 900;
pub(crate) const DESCRIPTOR_DESCENT: i64 = -250;
pub(crate) const DESCRIPTOR_CAP_HEIGHT: i64 = 700;
pub(crate) const DESCRIPTOR_STEM_V: i64 = 80;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn winansi_text_does_not_need_the_unicode_font() {
        assert!(!needs_unicode("Rechnung über 1.299,90 EUR — Grüße"));
        assert!(!needs_unicode("Ça va, déjà payé?"));
    }

    #[test]
    fn other_scripts_do() {
        assert!(needs_unicode("請求書"));
        assert!(needs_unicode("Счёт"));
        assert!(needs_unicode("Τιμολόγιο"));
        assert!(needs_unicode("Zażółć gęślą jaźń"));
    }

    #[test]
    fn hex_is_utf16_big_endian() {
        assert_eq!(utf16_hex("AB"), "00410042");
        assert_eq!(utf16_hex("ü"), "00FC");
        assert_eq!(utf16_hex("日"), "65E5");
        // Outside the basic plane: a surrogate pair, high unit first.
        assert_eq!(utf16_hex("𝄞"), "D834DD1E");
    }

    #[test]
    fn the_cmap_covers_every_code_and_stays_within_section_limits() {
        let cmap = to_unicode_cmap();
        assert!(cmap.contains("<0000> <00FF> <0000>"));
        assert!(cmap.contains("<FF00> <FFFF> <FF00>"));
        assert_eq!(cmap.matches("beginbfrange").count(), 3, "256 in 100s");
        for section in cmap.split("beginbfrange").skip(1) {
            let entries = section
                .split("endbfrange")
                .next()
                .expect("section ends")
                .lines()
                .filter(|l| l.starts_with('<'))
                .count();
            assert!(entries <= 100, "{entries} entries in one section");
        }
        assert!(cmap.ends_with("end\nend\n"));
    }
}
