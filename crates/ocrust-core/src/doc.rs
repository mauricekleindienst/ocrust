//! The document model returned by a scan: pages, blocks, lines and words.

use std::sync::Arc;

use image::RgbImage;
use serde::{Deserialize, Serialize};

use crate::geom::{Quad, Rect};

/// What a block of text looks like structurally.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockKind {
    #[default]
    Paragraph,
    /// A short, unusually large line — rendered as a Markdown heading.
    Heading,
    /// A line starting with a bullet or an enumerator.
    ListItem,
    /// Rows of cells that line up into columns. See [`Block::table`].
    Table,
    /// Coloured ink the page's own reading missed — a stamp across the text —
    /// read on its own. Only with
    /// [`EngineConfig::read_stamps`](crate::EngineConfig::read_stamps).
    Stamp,
    /// A tick box on a form, as one line reading `☒` (ticked) or `☐` (empty).
    /// Only with [`EngineConfig::tick_boxes`](crate::EngineConfig::tick_boxes).
    TickBox,
}

/// Where a page's pixels came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PageOrigin {
    /// A standalone image file, byte buffer or in-memory frame.
    Image,
    /// A page rasterized from a PDF.
    PdfPage,
    /// One frame of a multi-page TIFF.
    TiffFrame,
    /// A PDF page read from its own text layer, not recognized.
    PdfText,
}

/// A single word with its own box.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Word {
    pub text: String,
    pub bbox: Rect,
    pub confidence: f32,
}

/// One of the detector's boxes, before it was merged into a line.
///
/// The detector returns boxes, not lines, and a table row arrives as one box per
/// cell. Those boxes become a single [`Line`] so the row reads left to right —
/// and the boxes are kept here, because where they were is what makes the row's
/// columns recoverable afterwards.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Segment {
    pub text: String,
    pub bbox: Rect,
    pub confidence: f32,
}

/// One recognized text line.
///
/// `Default` gives an empty line at the origin, which is what a caller building a
/// document by hand wants to start from: `Line { text: "x".into(), bbox, ..Default::default() }`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Line {
    pub text: String,
    /// Mean character confidence in `0..=1`.
    pub confidence: f32,
    /// Detection polygon, oriented `[tl, tr, br, bl]`.
    pub quad: Quad,
    pub bbox: Rect,
    /// Rotation of the line in degrees, positive clockwise.
    pub angle: f32,
    /// Text-detector score for the box, in `0..=1`.
    pub det_score: f32,
    /// Mean distance between the chosen character and the runner-up.
    ///
    /// A saturated softmax says `0.99` for a character it read and `0.98` for
    /// one it guessed; how far ahead the winner was still tells them apart, and
    /// it is part of what [`Page::quality`] is built from.
    #[serde(default)]
    pub margin: f32,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub words: Vec<Word>,
    /// The boxes this line was merged from, left to right.
    ///
    /// Empty when the line came from a single box, which is the ordinary case
    /// for running text.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub segments: Vec<Segment>,
}

impl Line {
    /// The boxes this line is made of, left to right — itself, when it was never
    /// merged.
    pub fn parts(&self) -> Vec<Segment> {
        if self.segments.is_empty() {
            vec![Segment {
                text: self.text.clone(),
                bbox: self.bbox,
                confidence: self.confidence,
            }]
        } else {
            self.segments.clone()
        }
    }
}

/// One cell of a [`Table`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Cell {
    /// Zero-based row, counting from the top of the table.
    pub row: usize,
    /// Zero-based column, counting from the left.
    pub column: usize,
    /// Columns this cell covers; `1` for an ordinary cell.
    pub column_span: usize,
    pub text: String,
    pub bbox: Rect,
    /// Mean character confidence of the text in this cell.
    pub confidence: f32,
}

/// Rows and columns recovered from where the cells sit on the page.
///
/// There is no table model involved and no ruling lines are read: the columns
/// are the bands of the page that every row leaves a gap between. That finds the
/// tables people actually scan — invoices, receipts, statements, price lists —
/// whether or not they are ruled, and it says nothing about merged header cells
/// stacked two deep, which it cannot see.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Table {
    pub rows: usize,
    pub columns: usize,
    /// Cells in reading order: row by row, left to right. A row with nothing in
    /// a column simply has no cell for it.
    pub cells: Vec<Cell>,
}

impl Table {
    /// The cells of one row, left to right.
    pub fn row(&self, index: usize) -> impl Iterator<Item = &Cell> {
        self.cells.iter().filter(move |c| c.row == index)
    }

    /// Row `index` as one string per column, with empty strings for the gaps.
    pub fn row_text(&self, index: usize) -> Vec<String> {
        let mut out = vec![String::new(); self.columns];
        for cell in self.row(index) {
            if let Some(slot) = out.get_mut(cell.column) {
                if !slot.is_empty() {
                    slot.push(' ');
                }
                slot.push_str(&cell.text);
            }
        }
        out
    }
}

/// A group of lines that belong together.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Block {
    pub kind: BlockKind,
    pub bbox: Rect,
    pub lines: Vec<Line>,
    /// The grid, when `kind` is [`BlockKind::Table`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub table: Option<Table>,
}

impl Block {
    /// Lines joined by single newlines.
    pub fn text(&self) -> String {
        self.lines
            .iter()
            .map(|l| l.text.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// One page of a document.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Page {
    /// Zero-based page index within the source.
    pub index: usize,
    pub width: u32,
    pub height: u32,
    /// Page rotation that preprocessing corrected, in degrees.
    pub rotation: f32,
    pub origin: PageOrigin,
    pub blocks: Vec<Block>,
    /// Wall-clock time spent on this page, in milliseconds.
    pub elapsed_ms: f64,
    /// Estimated share of this page's characters that are right, in `0..=1`.
    ///
    /// `None` for a page with too little text to judge — under five lines is
    /// outside everything the estimate was fitted on. Unlike [`Page::confidence`],
    /// which is the recognizer's certainty about the characters it emitted, this
    /// accounts for what recognition cannot see — text that was missed, a layout
    /// that broke into fragments — and so ranks pages far better. It is an
    /// estimate fitted against a ground-truth corpus, not a guarantee.
    #[serde(default)]
    pub quality: Option<f32>,
    /// The preprocessed page image, kept only when
    /// `EngineConfig::keep_page_images` is set (needed to write searchable
    /// PDFs). Never serialized.
    #[serde(skip)]
    pub image: Option<Arc<RgbImage>>,
}

impl Page {
    /// Blocks joined by blank lines.
    pub fn text(&self) -> String {
        self.blocks
            .iter()
            .map(|b| b.text())
            .collect::<Vec<_>>()
            .join("\n\n")
    }

    /// All lines in reading order.
    pub fn lines(&self) -> impl Iterator<Item = &Line> {
        self.blocks.iter().flat_map(|b| b.lines.iter())
    }

    /// Mean line confidence, or `None` for an empty page.
    ///
    /// This is what the recognizer was sure of, character by character. For how
    /// much of the page is likely to be *right*, see [`Page::quality`].
    pub fn confidence(&self) -> Option<f32> {
        let mut sum = 0.0;
        let mut n = 0u32;
        for line in self.lines() {
            sum += line.confidence;
            n += 1;
        }
        (n > 0).then(|| sum / n as f32)
    }
}

/// A scanned document.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Document {
    /// Path or label the pages came from.
    pub source: String,
    pub pages: Vec<Page>,
    /// Total wall-clock time for the document, in milliseconds.
    pub elapsed_ms: f64,
}

impl Document {
    pub fn new(source: impl Into<String>) -> Self {
        Self {
            source: source.into(),
            pages: Vec::new(),
            elapsed_ms: 0.0,
        }
    }

    /// Pages joined by form feeds, the convention OCR tools use for page breaks.
    pub fn text(&self) -> String {
        self.pages
            .iter()
            .map(|p| p.text())
            .collect::<Vec<_>>()
            .join("\n\u{000c}\n")
    }

    /// Mean page quality, weighted by how much text each page carries.
    ///
    /// See [`Page::quality`] for what it estimates and what it cannot promise.
    pub fn quality(&self) -> Option<f32> {
        let mut weighted = 0.0f64;
        let mut lines = 0usize;
        for page in &self.pages {
            if let Some(quality) = page.quality {
                let count = page.lines().count().max(1);
                weighted += f64::from(quality) * count as f64;
                lines += count;
            }
        }
        (lines > 0).then(|| (weighted / lines as f64) as f32)
    }

    /// Mean line confidence across all pages.
    pub fn confidence(&self) -> Option<f32> {
        let mut sum = 0.0;
        let mut n = 0u32;
        for line in self.pages.iter().flat_map(|p| p.lines()) {
            sum += line.confidence;
            n += 1;
        }
        (n > 0).then(|| sum / n as f32)
    }

    /// Number of recognized lines.
    pub fn line_count(&self) -> usize {
        self.pages.iter().map(|p| p.lines().count()).sum()
    }

    /// Number of recognized words (falls back to whitespace splitting).
    pub fn word_count(&self) -> usize {
        self.pages
            .iter()
            .flat_map(|p| p.lines())
            .map(|l| {
                if l.words.is_empty() {
                    l.text.split_whitespace().count()
                } else {
                    l.words.len()
                }
            })
            .sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(text: &str, conf: f32) -> Line {
        let r = Rect::new(0.0, 0.0, 10.0, 5.0);
        Line {
            text: text.into(),
            confidence: conf,
            quad: Quad::from_rect(r),
            bbox: r,
            angle: 0.0,
            det_score: 1.0,
            margin: 0.0,
            words: Vec::new(),
            segments: Vec::new(),
        }
    }

    fn page(lines: Vec<Line>) -> Page {
        Page {
            index: 0,
            width: 100,
            height: 100,
            rotation: 0.0,
            origin: PageOrigin::Image,
            blocks: vec![Block {
                kind: BlockKind::Paragraph,
                bbox: Rect::new(0.0, 0.0, 10.0, 5.0),
                lines,
                table: None,
            }],
            elapsed_ms: 1.0,
            quality: None,
            image: None,
        }
    }

    #[test]
    fn text_joins_lines_blocks_and_pages() {
        let mut doc = Document::new("x");
        doc.pages.push(page(vec![line("a", 1.0), line("b", 1.0)]));
        doc.pages.push(page(vec![line("c", 1.0)]));
        assert_eq!(doc.text(), "a\nb\n\u{000c}\nc");
    }

    #[test]
    fn confidence_averages_over_lines() {
        let mut doc = Document::new("x");
        doc.pages.push(page(vec![line("a", 1.0), line("b", 0.5)]));
        assert!((doc.confidence().unwrap() - 0.75).abs() < 1e-6);
        assert_eq!(Document::new("empty").confidence(), None);
    }

    #[test]
    fn word_count_falls_back_to_whitespace() {
        let mut doc = Document::new("x");
        doc.pages.push(page(vec![line("hello brave world", 1.0)]));
        assert_eq!(doc.word_count(), 3);
        assert_eq!(doc.line_count(), 1);
    }

    #[test]
    fn document_round_trips_through_json() {
        let mut doc = Document::new("x");
        doc.pages.push(page(vec![line("a", 0.9)]));
        let json = serde_json::to_string(&doc).unwrap();
        let back: Document = serde_json::from_str(&json).unwrap();
        assert_eq!(back.text(), "a");
    }
}
