//! Turning a [`Document`] into the formats downstream tools expect.

#[cfg(feature = "pdf")]
pub(crate) mod cidfont;
pub mod overlay;
pub mod pdf;
pub mod tiff;

use std::fmt::Write as _;

use crate::doc::{BlockKind, Document, Page};
use crate::error::Result;
use crate::layout::strip_bullet;

/// Output formats.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// Plain UTF-8 text with form feeds between pages.
    Text,
    /// Markdown with headings and list items.
    Markdown,
    /// Full result including geometry and confidences.
    Json,
    /// hOCR, the HTML microformat every OCR tool speaks.
    Hocr,
    /// ALTO XML, used by libraries and archives.
    Alto,
    /// One CSV row per line, easy to load into a spreadsheet.
    Csv,
}

impl Format {
    /// Parses a format name as used on the command line.
    pub fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "text" | "txt" | "plain" => Some(Self::Text),
            "md" | "markdown" => Some(Self::Markdown),
            "json" => Some(Self::Json),
            "hocr" => Some(Self::Hocr),
            "alto" | "xml" => Some(Self::Alto),
            "csv" => Some(Self::Csv),
            _ => None,
        }
    }

    /// Conventional file extension.
    pub fn extension(&self) -> &'static str {
        match self {
            Self::Text => "txt",
            Self::Markdown => "md",
            Self::Json => "json",
            Self::Hocr => "hocr.html",
            Self::Alto => "alto.xml",
            Self::Csv => "csv",
        }
    }
}

/// Renders `doc` in `format`.
pub fn render(doc: &Document, format: Format) -> Result<String> {
    Ok(match format {
        Format::Text => doc.text(),
        Format::Markdown => to_markdown(doc),
        Format::Json => serde_json::to_string_pretty(doc)?,
        Format::Hocr => to_hocr(doc),
        Format::Alto => to_alto(doc),
        Format::Csv => to_csv(doc),
    })
}

/// Markdown with headings, list items and paragraphs.
pub fn to_markdown(doc: &Document) -> String {
    let mut out = String::new();
    for (i, page) in doc.pages.iter().enumerate() {
        if i > 0 {
            out.push_str("\n---\n\n");
        }
        for block in &page.blocks {
            let body = block.text();
            match block.kind {
                BlockKind::Heading => {
                    let _ = writeln!(out, "## {}\n", body.replace('\n', " "));
                }
                BlockKind::ListItem => {
                    for line in body.lines() {
                        let cleaned = strip_bullet(line);
                        let _ = writeln!(out, "- {cleaned}");
                    }
                    out.push('\n');
                }
                BlockKind::Paragraph => {
                    let _ = writeln!(out, "{body}\n");
                }
            }
        }
    }
    out.trim_end().to_string() + "\n"
}

/// hOCR 1.2, compatible with tesseract's output consumers.
pub fn to_hocr(doc: &Document) -> String {
    let mut out = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Transitional//EN\" \
         \"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd\">\n\
         <html xmlns=\"http://www.w3.org/1999/xhtml\">\n<head>\n\
         <meta http-equiv=\"Content-Type\" content=\"text/html;charset=utf-8\" />\n\
         <meta name=\"ocr-system\" content=\"ocrust\" />\n\
         <meta name=\"ocr-capabilities\" content=\"ocr_page ocr_carea ocr_par ocr_line ocrx_word\" />\n\
         </head>\n<body>\n",
    );
    let mut line_id = 0usize;
    let mut word_id = 0usize;
    for page in &doc.pages {
        let _ = writeln!(
            out,
            "  <div class='ocr_page' id='page_{}' title='image \"{}\"; bbox 0 0 {} {}; ppageno {}'>",
            page.index + 1,
            escape_xml(&doc.source),
            page.width,
            page.height,
            page.index
        );
        for (b, block) in page.blocks.iter().enumerate() {
            let bb = block.bbox;
            let _ = writeln!(
                out,
                "   <div class='ocr_carea' id='block_{}_{}' title='bbox {} {} {} {}'>\n    \
                 <p class='ocr_par' dir='ltr'>",
                page.index + 1,
                b,
                bb.x0 as i64,
                bb.y0 as i64,
                bb.x1 as i64,
                bb.y1 as i64
            );
            for line in &block.lines {
                line_id += 1;
                let lb = line.bbox;
                let _ = writeln!(
                    out,
                    "     <span class='ocr_line' id='line_{line_id}' \
                     title='bbox {} {} {} {}; x_wconf {}'>",
                    lb.x0 as i64,
                    lb.y0 as i64,
                    lb.x1 as i64,
                    lb.y1 as i64,
                    (line.confidence * 100.0).round() as i32
                );
                if !words_cover_line(line) {
                    word_id += 1;
                    let _ = writeln!(
                        out,
                        "      <span class='ocrx_word' id='word_{word_id}' \
                         title='bbox {} {} {} {}; x_wconf {}'>{}</span>",
                        lb.x0 as i64,
                        lb.y0 as i64,
                        lb.x1 as i64,
                        lb.y1 as i64,
                        (line.confidence * 100.0).round() as i32,
                        escape_xml(&line.text)
                    );
                } else {
                    for word in &line.words {
                        word_id += 1;
                        let wb = word.bbox;
                        let _ = writeln!(
                            out,
                            "      <span class='ocrx_word' id='word_{word_id}' \
                             title='bbox {} {} {} {}; x_wconf {}'>{}</span>",
                            wb.x0 as i64,
                            wb.y0 as i64,
                            wb.x1 as i64,
                            wb.y1 as i64,
                            (word.confidence * 100.0).round() as i32,
                            escape_xml(&word.text)
                        );
                    }
                }
                out.push_str("     </span>\n");
            }
            out.push_str("    </p>\n   </div>\n");
        }
        out.push_str("  </div>\n");
    }
    out.push_str("</body>\n</html>\n");
    out
}

/// ALTO 4.2 XML.
pub fn to_alto(doc: &Document) -> String {
    let mut out = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <alto xmlns=\"http://www.loc.gov/standards/alto/ns-v4#\">\n\
         \x20 <Description>\n    <MeasurementUnit>pixel</MeasurementUnit>\n\
         \x20   <OCRProcessing ID=\"OCR_1\">\n      <ocrProcessingStep>\n\
         \x20       <processingSoftware>\n          <softwareName>ocrust</softwareName>\n\
         \x20       </processingSoftware>\n      </ocrProcessingStep>\n    </OCRProcessing>\n\
         \x20 </Description>\n  <Layout>\n",
    );
    for page in &doc.pages {
        let _ = writeln!(
            out,
            "    <Page ID=\"page_{}\" PHYSICAL_IMG_NR=\"{}\" WIDTH=\"{}\" HEIGHT=\"{}\">\n\
             \x20     <PrintSpace HPOS=\"0\" VPOS=\"0\" WIDTH=\"{}\" HEIGHT=\"{}\">",
            page.index + 1,
            page.index + 1,
            page.width,
            page.height,
            page.width,
            page.height
        );
        for (b, block) in page.blocks.iter().enumerate() {
            let bb = block.bbox;
            let _ = writeln!(
                out,
                "        <TextBlock ID=\"block_{}_{}\" HPOS=\"{}\" VPOS=\"{}\" WIDTH=\"{}\" HEIGHT=\"{}\">",
                page.index + 1,
                b,
                bb.x0 as i64,
                bb.y0 as i64,
                bb.width() as i64,
                bb.height() as i64
            );
            for (l, line) in block.lines.iter().enumerate() {
                let lb = line.bbox;
                let _ = writeln!(
                    out,
                    "          <TextLine ID=\"line_{}_{}_{}\" HPOS=\"{}\" VPOS=\"{}\" WIDTH=\"{}\" HEIGHT=\"{}\">",
                    page.index + 1,
                    b,
                    l,
                    lb.x0 as i64,
                    lb.y0 as i64,
                    lb.width() as i64,
                    lb.height() as i64
                );
                let words: Vec<(String, crate::geom::Rect, f32)> = if !words_cover_line(line) {
                    vec![(line.text.clone(), lb, line.confidence)]
                } else {
                    line.words
                        .iter()
                        .map(|w| (w.text.clone(), w.bbox, w.confidence))
                        .collect()
                };
                for (text, bbox, conf) in words {
                    let _ = writeln!(
                        out,
                        "            <String CONTENT=\"{}\" HPOS=\"{}\" VPOS=\"{}\" WIDTH=\"{}\" HEIGHT=\"{}\" WC=\"{:.2}\"/>",
                        escape_xml(&text),
                        bbox.x0 as i64,
                        bbox.y0 as i64,
                        bbox.width() as i64,
                        bbox.height() as i64,
                        conf
                    );
                }
                out.push_str("          </TextLine>\n");
            }
            out.push_str("        </TextBlock>\n");
        }
        out.push_str("      </PrintSpace>\n    </Page>\n");
    }
    out.push_str("  </Layout>\n</alto>\n");
    out
}

/// One row per line: `page,block,line,x0,y0,x1,y1,confidence,text`.
pub fn to_csv(doc: &Document) -> String {
    let mut out = String::from("page,block,line,x0,y0,x1,y1,confidence,text\n");
    for page in &doc.pages {
        for (b, block) in page.blocks.iter().enumerate() {
            for (l, line) in block.lines.iter().enumerate() {
                let r = line.bbox;
                let _ = writeln!(
                    out,
                    "{},{},{},{:.1},{:.1},{:.1},{:.1},{:.4},{}",
                    page.index,
                    b,
                    l,
                    r.x0,
                    r.y0,
                    r.x1,
                    r.y1,
                    line.confidence,
                    csv_field(&line.text)
                );
            }
        }
    }
    out
}

/// A compact human-readable summary, handy for CLI output.
pub fn summary(doc: &Document) -> String {
    let conf = doc
        .confidence()
        .map(|c| format!("{:.1}%", c * 100.0))
        .unwrap_or_else(|| "n/a".into());
    format!(
        "{}: {} page(s), {} line(s), {} word(s), mean confidence {conf}, {:.0} ms",
        doc.source,
        doc.pages.len(),
        doc.line_count(),
        doc.word_count(),
        doc.elapsed_ms
    )
}

/// Per-page statistics.
pub fn page_summary(page: &Page) -> String {
    format!(
        "page {}: {}x{}, {} line(s), {:.0} ms",
        page.index + 1,
        page.width,
        page.height,
        page.lines().count(),
        page.elapsed_ms
    )
}

/// True when the word boxes spell out the whole line.
///
/// Word boxes come from the recognizer's character positions and normally cover
/// the line exactly, but a caller may have filtered or rebuilt them. Exporters
/// fall back to the full line so that text is never silently dropped.
fn words_cover_line(line: &crate::doc::Line) -> bool {
    if line.words.is_empty() {
        return false;
    }
    let joined: String = line
        .words
        .iter()
        .flat_map(|w| w.text.chars())
        .filter(|c| !c.is_whitespace())
        .collect();
    let expected: String = line.text.chars().filter(|c| !c.is_whitespace()).collect();
    joined == expected
}

fn escape_xml(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            // XML 1.0 forbids most control characters.
            c if (c as u32) < 0x20 && c != '\t' && c != '\n' && c != '\r' => out.push(' '),
            c => out.push(c),
        }
    }
    out
}

fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doc::{Block, Line, Page, PageOrigin, Word};
    use crate::geom::{Quad, Rect};

    fn line(text: &str, r: Rect, words: Vec<Word>) -> Line {
        Line {
            text: text.into(),
            confidence: 0.91,
            quad: Quad::from_rect(r),
            bbox: r,
            angle: 0.0,
            det_score: 0.8,
            words,
        }
    }

    fn doc() -> Document {
        let mut d = Document::new("scan & test.png");
        d.pages.push(Page {
            index: 0,
            width: 640,
            height: 480,
            rotation: 0.0,
            origin: PageOrigin::Image,
            blocks: vec![
                Block {
                    kind: BlockKind::Heading,
                    bbox: Rect::new(10.0, 10.0, 300.0, 40.0),
                    lines: vec![line(
                        "Rechnung 2026",
                        Rect::new(10.0, 10.0, 300.0, 40.0),
                        vec![],
                    )],
                },
                Block {
                    kind: BlockKind::Paragraph,
                    bbox: Rect::new(10.0, 60.0, 400.0, 100.0),
                    lines: vec![line(
                        "Betrag: 19,90 <EUR>",
                        Rect::new(10.0, 60.0, 400.0, 80.0),
                        vec![Word {
                            text: "Betrag:".into(),
                            bbox: Rect::new(10.0, 60.0, 80.0, 80.0),
                            confidence: 0.95,
                        }],
                    )],
                },
                Block {
                    kind: BlockKind::ListItem,
                    bbox: Rect::new(10.0, 110.0, 400.0, 130.0),
                    lines: vec![line(
                        "• Position eins",
                        Rect::new(10.0, 110.0, 400.0, 130.0),
                        vec![],
                    )],
                },
            ],
            elapsed_ms: 12.0,
            image: None,
        });
        d.elapsed_ms = 20.0;
        d
    }

    #[test]
    fn format_names_and_extensions() {
        assert_eq!(Format::parse("MD"), Some(Format::Markdown));
        assert_eq!(Format::parse("hocr"), Some(Format::Hocr));
        assert_eq!(Format::parse("nope"), None);
        assert_eq!(Format::Json.extension(), "json");
    }

    #[test]
    fn markdown_uses_headings_and_bullets() {
        let md = to_markdown(&doc());
        assert!(md.contains("## Rechnung 2026"), "{md}");
        assert!(md.contains("- Position eins"), "{md}");
        assert!(md.contains("Betrag: 19,90 <EUR>"), "{md}");
    }

    #[test]
    fn a_list_line_keeps_a_negative_amount() {
        // The bullet goes, the minus sign stays: a Markdown exporter that trims
        // every leading dash turns a refund into a charge.
        let mut d = Document::new("kontoauszug.png");
        d.pages.push(Page {
            index: 0,
            width: 200,
            height: 100,
            rotation: 0.0,
            origin: PageOrigin::Image,
            blocks: vec![Block {
                kind: BlockKind::ListItem,
                bbox: Rect::new(0.0, 0.0, 200.0, 60.0),
                lines: vec![
                    line(
                        "\u{2022} Gutschrift",
                        Rect::new(0.0, 0.0, 200.0, 20.0),
                        vec![],
                    ),
                    line("-19,90 EUR", Rect::new(0.0, 30.0, 200.0, 50.0), vec![]),
                ],
            }],
            elapsed_ms: 1.0,
            image: None,
        });
        let md = to_markdown(&d);
        assert!(md.contains("- Gutschrift"), "{md}");
        assert!(md.contains("- -19,90 EUR"), "{md}");
    }

    #[test]
    fn hocr_falls_back_to_the_line_when_words_are_incomplete() {
        // The fixture has one word box for a two-word line, so the exporter
        // must emit the line text rather than lose "19,90 <EUR>".
        let hocr = to_hocr(&doc());
        assert!(hocr.contains("19,90 &lt;EUR&gt;"), "{hocr}");
        assert_eq!(hocr.matches("class='ocrx_word'").count(), 3);
    }

    #[test]
    fn hocr_uses_word_boxes_when_they_cover_the_line() {
        let mut d = doc();
        let line = &mut d.pages[0].blocks[1].lines[0];
        line.text = "Betrag 1990".into();
        line.words = vec![
            Word {
                text: "Betrag".into(),
                bbox: Rect::new(10.0, 60.0, 80.0, 80.0),
                confidence: 0.95,
            },
            Word {
                text: "1990".into(),
                bbox: Rect::new(90.0, 60.0, 140.0, 80.0),
                confidence: 0.9,
            },
        ];
        let hocr = to_hocr(&d);
        assert!(hocr.contains(">Betrag</span>"), "{hocr}");
        assert!(hocr.contains(">1990</span>"), "{hocr}");
        assert_eq!(hocr.matches("class='ocrx_word'").count(), 4);
    }

    #[test]
    fn hocr_escapes_and_carries_geometry() {
        let hocr = to_hocr(&doc());
        assert!(hocr.contains("class='ocr_page'"));
        assert!(hocr.contains("bbox 10 60 400 80"), "{hocr}");
        assert!(hocr.contains("scan &amp; test.png"), "escaped source");
        assert!(hocr.contains("x_wconf 91"));
    }

    #[test]
    fn alto_is_wellformed_enough_to_parse_tags() {
        let alto = to_alto(&doc());
        assert!(alto.starts_with("<?xml"));
        assert_eq!(alto.matches("<TextLine").count(), 3);
        assert_eq!(alto.matches("</Page>").count(), 1);
        // The fixture's word boxes do not cover the whole line, so ALTO carries
        // the full line text instead of a partial word.
        assert!(
            alto.contains("CONTENT=\"Betrag: 19,90 &lt;EUR&gt;\""),
            "{alto}"
        );
        // Every opened tag is closed.
        for tag in ["alto", "Layout", "Page", "PrintSpace", "TextBlock"] {
            assert_eq!(
                alto.matches(&format!("<{tag}")).count(),
                alto.matches(&format!("</{tag}>")).count(),
                "unbalanced {tag}"
            );
        }
    }

    #[test]
    fn csv_quotes_fields_with_commas() {
        let csv = to_csv(&doc());
        let line = csv.lines().find(|l| l.contains("Betrag")).unwrap();
        assert!(line.contains("\"Betrag: 19,90 <EUR>\""), "{line}");
        assert_eq!(csv.lines().count(), 4, "header + 3 lines");
    }

    #[test]
    fn json_round_trips() {
        let json = render(&doc(), Format::Json).unwrap();
        let back: Document = serde_json::from_str(&json).unwrap();
        assert_eq!(back.pages[0].blocks.len(), 3);
    }

    #[test]
    fn summary_mentions_counts() {
        let s = summary(&doc());
        assert!(s.contains("1 page(s)"), "{s}");
        assert!(s.contains("3 line(s)"), "{s}");
    }

    #[test]
    fn control_characters_are_stripped_from_xml() {
        let mut d = doc();
        d.pages[0].blocks[0].lines[0].text = "bad\u{0007}char".into();
        let hocr = to_hocr(&d);
        assert!(!hocr.contains('\u{0007}'));
        assert!(hocr.contains("bad char"));
    }
}
