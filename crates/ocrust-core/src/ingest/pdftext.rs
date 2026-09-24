//! The text a PDF page already carries, read instead of recognized.
//!
//! A PDF written by a word processor holds its text as text: every glyph is
//! drawn from a font that says, through its `ToUnicode` map or its encoding,
//! which character it is. Reading that is exact where OCR is a very good
//! guess, and it takes milliseconds where OCR takes a second. The interpreter
//! that rasterizes pages is run a second time with a device that draws
//! nothing and writes down, for every glyph, its characters and where they
//! sit; those become lines of the same shape the text detector produces, so
//! the layout that follows — reading order, paragraphs, headings, tables —
//! is the one every scanned page gets.
//!
//! A page is read this way only when its text layer can be trusted to be the
//! page: a scan with an invisible OCR layer, a page that is mostly a picture,
//! or a font whose characters cannot be mapped to Unicode is recognized
//! instead (see [`PdfText::Auto`]).

use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::hayro_cmap::BfString;
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Context, Device, GlyphDrawMode, Image, InterpreterCache,
    InterpreterSettings, Paint, PathDrawMode, SoftMask, TransformExt,
};
use kurbo::{Affine, BezPath, Point as KPoint, Vec2};

use crate::doc::{Line, Segment, Word};
use crate::geom::{Point, Quad, Rect};

/// Glyph-space units per em, as the interpreter hands glyphs over.
const UNITS_PER_EM: f64 = 1000.0;
/// How far above the baseline a line's box reaches, in ems.
///
/// As far as the text detector's boxes do — measured at 0.8 to 1.0 em — and
/// not to the em box's 0.8: the layout that follows measures its gaps in line
/// heights, and a table set at one and a half lines' pitch comes apart row
/// from row when its boxes are a fifth shorter than the ones it was tuned on.
const ASCENT_EM: f64 = 0.9;
/// How far below the baseline, in ems: the detector's 0.15 to 0.4.
const DESCENT_EM: f64 = 0.3;

/// Whether, and when, a PDF page's own text is used instead of OCR.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PdfText {
    /// Always recognize the rendered page (the default, and what every
    /// release before 0.3 did).
    #[default]
    Never,
    /// Use the text layer where it can be trusted to be the page — visible
    /// text, mapped to Unicode, on a page that is not mostly a picture — and
    /// recognize every other page.
    Auto,
    /// Use any text layer with readable characters, an invisible OCR layer
    /// included; recognize only pages without one.
    Always,
}

impl PdfText {
    /// `"never"`, `"auto"` or `"always"`.
    pub fn parse(name: &str) -> Option<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "never" | "off" | "no" => Some(Self::Never),
            "auto" => Some(Self::Auto),
            "always" | "on" | "yes" => Some(Self::Always),
            _ => None,
        }
    }
}

/// One glyph as the page draws it, in the pixel space of the rendered page.
#[derive(Debug, Clone)]
struct TextGlyph {
    text: String,
    /// Where the glyph starts on its baseline.
    origin: KPoint,
    /// Where the next glyph would start without extra spacing.
    end: KPoint,
    /// One em, from the baseline upwards, in page pixels.
    up: Vec2,
    visible: bool,
    mapped: bool,
}

impl TextGlyph {
    fn size(&self) -> f64 {
        self.up.hypot()
    }

    /// The baseline direction: the em rotated a quarter turn clockwise.
    fn baseline(&self) -> Vec2 {
        let size = self.size().max(f64::EPSILON);
        Vec2::new(-self.up.y / size, self.up.x / size)
    }

    fn angle(&self) -> f64 {
        let b = self.baseline();
        b.y.atan2(b.x).to_degrees()
    }

    /// The glyph's box: from [`DESCENT_EM`] below the baseline to
    /// [`ASCENT_EM`] above it, origin to advance.
    fn bbox(&self) -> Rect {
        let top = self.up * ASCENT_EM;
        let bottom = self.up * -DESCENT_EM;
        let corners = [
            self.origin + top,
            self.origin + bottom,
            self.end + top,
            self.end + bottom,
        ];
        let xs = corners.iter().map(|p| p.x as f32);
        let ys = corners.iter().map(|p| p.y as f32);
        Rect::new(
            xs.clone().fold(f32::INFINITY, f32::min),
            ys.clone().fold(f32::INFINITY, f32::min),
            xs.fold(f32::NEG_INFINITY, f32::max),
            ys.fold(f32::NEG_INFINITY, f32::max),
        )
    }
}

/// What a page's text layer holds.
#[derive(Debug, Default)]
pub struct TextLayer {
    glyphs: Vec<TextGlyph>,
    /// Small filled shapes: the bullets of a list drawn as circles or squares
    /// rather than set as a character, which is how browsers print them.
    marks: Vec<Rect>,
    /// Page area covered by raster images, in square pixels.
    image_area: f64,
    width: f64,
    height: f64,
}

impl TextLayer {
    /// The page's size in pixels, as the renderer would make it.
    pub fn size(&self) -> (u32, u32) {
        (self.width.max(1.0) as u32, self.height.max(1.0) as u32)
    }

    /// Glyphs with a character, as drawn on the page.
    pub fn glyph_count(&self) -> usize {
        self.glyphs
            .iter()
            .filter(|g| !g.text.trim().is_empty())
            .count()
    }

    /// Whether the page should be read from this layer rather than recognized.
    pub fn usable(&self, mode: PdfText) -> bool {
        let printed: Vec<&TextGlyph> = self
            .glyphs
            .iter()
            .filter(|g| !g.text.trim().is_empty())
            .collect();
        match mode {
            PdfText::Never => false,
            PdfText::Always => printed.iter().any(|g| g.mapped),
            PdfText::Auto => {
                let visible: Vec<&&TextGlyph> = printed.iter().filter(|g| g.visible).collect();
                if visible.is_empty() {
                    return false;
                }
                // An invisible layer over a picture is somebody's OCR; the
                // picture is read again, the same way as every other scan.
                if visible.len() * 2 < printed.len() {
                    return false;
                }
                // Mostly a picture: a scan, a photo, a slide exported as an
                // image — its words may be in the pixels, not the text layer.
                let page_area = (self.width * self.height).max(1.0);
                if self.image_area.min(page_area) > 0.5 * page_area {
                    return false;
                }
                // A font without a Unicode mapping reads as boxes; one wrong
                // character in twenty is worse than recognizing the page.
                let mapped = visible.iter().filter(|g| g.mapped).count();
                mapped * 20 >= visible.len() * 19
            }
        }
    }

    /// The layer's text as lines, the way the text detector delivers them:
    /// one line per run of glyphs on one baseline, split where a gap is wide
    /// enough to be a column or a table cell rather than a space.
    pub fn lines(&self, include_invisible: bool) -> Vec<Line> {
        let (width, height) = (self.width, self.height);
        let on_page = |g: &&TextGlyph| {
            let c = g.origin + g.up * 0.3;
            c.x >= -2.0 && c.y >= -2.0 && c.x <= width + 2.0 && c.y <= height + 2.0
        };
        let glyphs: Vec<&TextGlyph> = self
            .glyphs
            .iter()
            .filter(|g| (g.visible || include_invisible) && g.size() >= 1.0)
            .filter(on_page)
            .collect();

        // Runs in drawing order: a PDF draws a line's glyphs one after the
        // other, far more often than not.
        let mut runs: Vec<Run> = Vec::new();
        for glyph in glyphs {
            let joined = runs.last_mut().is_some_and(|run| run.take(glyph));
            if !joined {
                runs.push(Run::new(glyph));
            }
        }
        // Runs drawn out of order that still belong together: a word set in
        // another font drawn later, a number right-aligned in a second pass.
        // Whether the page sets its own spaces. Most producers draw a space
        // glyph between two words; then a gap that nothing fills is a tab stop
        // or the edge of a table cell, set apart by position alone. A page
        // set without space glyphs has only gaps, and those are its words.
        let (spaces, gaps) =
            runs.iter()
                .flat_map(|r| &r.pieces)
                .fold((0, 0), |(s, g), p| match p {
                    Piece::Space => (s + 1, g),
                    Piece::Gap => (s, g + 1),
                    Piece::Char(..) => (s, g),
                });
        let gaps_are_cells = spaces >= SPACES_SET || spaces * 3 >= gaps.max(1);
        let runs = merge_runs(runs);
        let runs = join_word_gaps(runs, gaps_are_cells);
        let mut lines: Vec<Line> = runs
            .into_iter()
            .filter_map(|run| run.into_line(gaps_are_cells))
            .collect();
        self.restore_bullets(&mut lines);
        lines
    }

    /// Puts back the bullet a list item was drawn with: a small filled shape
    /// just left of where a line starts, centred on its lowercase letters.
    fn restore_bullets(&self, lines: &mut [Line]) {
        let mut used = vec![false; self.marks.len()];
        for line in lines.iter_mut() {
            if line.angle.abs() > 3.0 {
                continue;
            }
            let height = line.bbox.height();
            let middle = line.bbox.y0 + 0.55 * height;
            let found = self.marks.iter().enumerate().find(|(i, mark)| {
                let size = mark.width().max(mark.height());
                let centre = (mark.y0 + mark.y1) / 2.0;
                !used[*i]
                    && size <= 0.6 * height
                    && mark.x1 <= line.bbox.x0 + 1.0
                    && line.bbox.x0 - mark.x1 <= 2.5 * height
                    && (centre - middle).abs() <= 0.3 * height
            });
            if let Some((i, mark)) = found {
                used[i] = true;
                line.text = format!("• {}", line.text);
                line.words.insert(
                    0,
                    Word {
                        text: "•".to_string(),
                        bbox: *mark,
                        confidence: 1.0,
                    },
                );
                line.bbox = line.bbox.union(mark);
                if let Some(first) = line.segments.first_mut() {
                    first.text = format!("• {}", first.text);
                    first.bbox = first.bbox.union(mark);
                }
            }
        }
    }
}

/// What a run is made of, left to right.
enum Piece {
    Char(String, Rect),
    /// A space the document set, or one the lines around it vouch for.
    Space,
    /// White space nothing fills: a word space on a page that never sets its
    /// own, the edge of a table cell on one that does.
    Gap,
}

/// Glyphs on one baseline, close enough together to be one piece of text.
struct Run {
    pieces: Vec<Piece>,
    start: KPoint,
    last_origin: KPoint,
    last_end: KPoint,
    last_text: String,
    up: Vec2,
    angle: f64,
    size: f64,
    /// Whether the last glyph was a space the document set itself.
    after_space: bool,
}

/// After a space the document set itself, a gap has to be this many ems to
/// split the line: justified text in a narrow column stretches its word
/// spaces far past an em, and those spaces are glyphs. A table's cells and
/// a page's columns are separated by position alone.
const COLUMN_GAP_AFTER_SPACE_EM: f64 = 4.0;

/// A gap wider than this many ems splits a line into two boxes: the next
/// column, a table cell, a tab stop — not the space between two words, which
/// is a quarter to half an em, and in justified text rarely approaches one.
const COLUMN_GAP_EM: f64 = 1.0;
/// A gap wider than this many ems is a space between two words.
const SPACE_GAP_EM: f64 = 0.15;
/// Space glyphs enough to say a page sets its own spaces whatever else it
/// has: a table of one-word cells has more gaps than spaces all the same.
const SPACES_SET: usize = 8;

impl Run {
    fn new(glyph: &TextGlyph) -> Self {
        let mut run = Run {
            pieces: Vec::new(),
            start: glyph.origin,
            last_origin: glyph.origin,
            last_end: glyph.end,
            last_text: String::new(),
            up: glyph.up,
            angle: glyph.angle(),
            size: glyph.size(),
            after_space: false,
        };
        run.push(glyph);
        run
    }

    fn push(&mut self, glyph: &TextGlyph) {
        self.after_space = glyph.text.trim().is_empty();
        if self.after_space {
            self.space(Piece::Space);
        } else {
            self.pieces
                .push(Piece::Char(glyph.text.clone(), glyph.bbox()));
        }
        self.last_origin = glyph.origin;
        self.last_end = glyph.end;
        self.last_text = glyph.text.clone();
        self.size = self.size.max(glyph.size());
    }

    /// Ends the word so far with `piece`, a [`Piece::Space`] or a
    /// [`Piece::Gap`]. A space glyph drawn into a gap makes it a space.
    fn space(&mut self, piece: Piece) {
        match self.pieces.last_mut() {
            Some(Piece::Char(..)) => self.pieces.push(piece),
            Some(last @ Piece::Gap) if matches!(piece, Piece::Space) => *last = piece,
            _ => {}
        }
    }

    /// Adds `glyph` if it goes on this run; says whether it did.
    fn take(&mut self, glyph: &TextGlyph) -> bool {
        let angle = glyph.angle();
        let turn = (angle - self.angle + 540.0).rem_euclid(360.0) - 180.0;
        if turn.abs() > 3.0 {
            return false;
        }
        let size = self.size.max(glyph.size());
        let b = glyph.baseline();
        let n = Vec2::new(-b.y, b.x);
        // Off the baseline by more than a third of an em: another line.
        if (glyph.origin - self.last_origin).dot(n).abs() > 0.35 * size {
            return false;
        }
        // The same glyph drawn again a hair away: "fake bold", or a text
        // shadow. It is one character, not two.
        if glyph.text == self.last_text && (glyph.origin - self.last_origin).hypot() < 0.1 * size {
            return true;
        }
        let gap = (glyph.origin - self.last_end).dot(b);
        let widest = if self.after_space {
            COLUMN_GAP_AFTER_SPACE_EM
        } else {
            COLUMN_GAP_EM
        };
        if gap < -0.5 * size || gap > widest * size {
            return false;
        }
        if gap > SPACE_GAP_EM * size {
            self.space(Piece::Gap);
        }
        self.push(glyph);
        true
    }

    fn baseline(&self) -> Vec2 {
        let size = self.up.hypot().max(f64::EPSILON);
        Vec2::new(-self.up.y / size, self.up.x / size)
    }

    /// Leftmost and rightmost pixel the run's characters cover.
    fn x_range(&self) -> (f64, f64) {
        let (lo, hi) = self
            .pieces
            .iter()
            .filter_map(|p| match p {
                Piece::Char(_, b) => Some(b),
                _ => None,
            })
            .fold((f64::INFINITY, f64::NEG_INFINITY), |(lo, hi), b| {
                (lo.min(f64::from(b.x0)), hi.max(f64::from(b.x1)))
            });
        if lo.is_finite() {
            (lo, hi)
        } else {
            (self.start.x, self.last_end.x)
        }
    }

    /// The run as a line of words. Where `gaps_are_cells`, a [`Piece::Gap`]
    /// ends a cell: the line comes back in [`Line::segments`], one per cell,
    /// the way the layout keeps apart the boxes of a row it merged.
    fn into_line(self, gaps_are_cells: bool) -> Option<Line> {
        let mut words: Vec<Word> = Vec::new();
        // Whether each word opens a new cell.
        let mut opens: Vec<bool> = Vec::new();
        let mut word_text = String::new();
        let mut word_box: Option<Rect> = None;
        let mut cut = false;
        for piece in &self.pieces {
            match piece {
                Piece::Char(s, bbox) => {
                    if word_box.is_none() {
                        opens.push(cut && !words.is_empty());
                        cut = false;
                    }
                    word_text.push_str(s);
                    word_box = Some(word_box.map_or(*bbox, |b| b.union(bbox)));
                }
                Piece::Space | Piece::Gap => {
                    if let Some(bbox) = word_box.take() {
                        words.push(Word {
                            text: std::mem::take(&mut word_text),
                            bbox,
                            confidence: 1.0,
                        });
                    }
                    cut |= gaps_are_cells && matches!(piece, Piece::Gap);
                }
            }
        }
        if let Some(bbox) = word_box.take() {
            words.push(Word {
                text: word_text,
                bbox,
                confidence: 1.0,
            });
        }
        let text = words
            .iter()
            .map(|w| w.text.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        if text.trim().is_empty() {
            return None;
        }
        // A gap between two words of one cell was judged a word space: a
        // column or a cell would have split the line. Words meet in the
        // middle of it, so that a justified line's stretched spaces do not
        // look like the gutters of a table to the layout that follows.
        if self.angle.abs() <= 3.0 {
            for i in 1..words.len() {
                let (left, right) = (words[i - 1].bbox.x1, words[i].bbox.x0);
                if right > left && !opens[i] {
                    let middle = (left + right) / 2.0;
                    words[i - 1].bbox.x1 = middle;
                    words[i].bbox.x0 = middle;
                }
            }
        }
        let mut segments: Vec<Segment> = Vec::new();
        if opens.iter().any(|&o| o) {
            for (word, &open) in words.iter().zip(&opens) {
                match segments.last_mut() {
                    Some(cell) if !open => {
                        cell.text.push(' ');
                        cell.text.push_str(&word.text);
                        cell.bbox = cell.bbox.union(&word.bbox);
                    }
                    _ => segments.push(Segment {
                        text: word.text.clone(),
                        bbox: word.bbox,
                        confidence: 1.0,
                    }),
                }
            }
        }
        let b = self.baseline();
        let top = self.up * ASCENT_EM;
        let bottom = self.up * -DESCENT_EM;
        let (s, e) = (self.start, self.last_end);
        // Make sure the quad spans the whole run even if the last glyph was
        // drawn left of the first one (a run merged from two).
        let (s, e) = if (e - s).dot(b) >= 0.0 {
            (s, e)
        } else {
            (e, s)
        };
        let point = |p: KPoint| Point::new(p.x as f32, p.y as f32);
        let quad = Quad::new([
            point(s + top),
            point(e + top),
            point(e + bottom),
            point(s + bottom),
        ]);
        let mut bbox = quad.bounds();
        for word in &words {
            bbox = bbox.union(&word.bbox);
        }
        Some(Line {
            text,
            confidence: 1.0,
            quad,
            bbox,
            angle: self.angle as f32,
            det_score: 1.0,
            margin: 1.0,
            words,
            segments,
        })
    }
}

/// Joins runs that sit on one baseline next to each other, in reading order.
fn merge_runs(mut runs: Vec<Run>) -> Vec<Run> {
    let mut merged = true;
    while merged {
        merged = false;
        'outer: for i in 0..runs.len() {
            for j in 0..runs.len() {
                if i == j {
                    continue;
                }
                if follows(&runs[i], &runs[j]) {
                    let next = runs.remove(j);
                    let i = if j < i { i - 1 } else { i };
                    let run = &mut runs[i];
                    let size = run.size.max(next.size);
                    let gap = (next.start - run.last_end).dot(run.baseline());
                    if gap > SPACE_GAP_EM * size {
                        run.space(Piece::Gap);
                    }
                    run.pieces.extend(next.pieces);
                    run.last_origin = next.last_origin;
                    run.last_end = next.last_end;
                    run.last_text = next.last_text;
                    run.after_space = next.after_space;
                    run.size = size;
                    merged = true;
                    break 'outer;
                }
            }
        }
    }
    runs
}

/// Rejoins pieces of a line that were split at a gap which only a word space
/// fills: justified text in a narrow column stretches its spaces past an em,
/// and a browser sets each word on its own, so the gap is all there is to go
/// by. A gap between two columns, or two cells of a table, is a corridor: the
/// lines above and below it are white there too. A stretched word space is
/// not — the neighbouring lines have letters where it is.
///
/// Where the page sets its own spaces (`spaces_set`), only a piece that ends
/// on one is looked at: a justified space is still a space glyph, however
/// far it is stretched, and a gap without one is a cell's edge even where a
/// longer cell below reaches across it.
fn join_word_gaps(runs: Vec<Run>, spaces_set: bool) -> Vec<Run> {
    let upright: Vec<bool> = runs.iter().map(|r| r.angle.abs() <= 3.0).collect();
    let spans: Vec<(f64, f64, f64)> = runs
        .iter()
        .map(|r| {
            let (x0, x1) = r.x_range();
            (r.start.y, x0, x1)
        })
        .collect();
    // Pairs to join: (left, right), both upright, on one baseline, next to
    // each other with nothing in between.
    let mut join: Vec<(usize, usize)> = Vec::new();
    for (a, run) in runs.iter().enumerate() {
        if !upright[a] || (spaces_set && !run.after_space) {
            continue;
        }
        let (y, _, x1) = spans[a];
        let size = run.size;
        // The nearest piece to the right on this baseline.
        let right = (0..runs.len())
            .filter(|&b| b != a && upright[b])
            .filter(|&b| (spans[b].0 - y).abs() <= 0.35 * size.max(runs[b].size))
            .filter(|&b| spans[b].1 >= x1 - 0.1 * size)
            .min_by(|&p, &q| spans[p].1.total_cmp(&spans[q].1));
        let Some(b) = right else { continue };
        let gap = spans[b].1 - x1;
        if gap > COLUMN_GAP_AFTER_SPACE_EM * size {
            continue;
        }
        let middle = (x1 + spans[b].1) / 2.0;
        // The nearest baseline above and below, a line's distance away.
        let neighbour = |above: bool| {
            let mut best: Option<f64> = None;
            for (c, &(cy, _, _)) in spans.iter().enumerate() {
                if !upright[c] || c == a || c == b {
                    continue;
                }
                let d = if above { y - cy } else { cy - y };
                if d > 0.6 * size && d < 2.5 * size && best.is_none_or(|bd| d < bd) {
                    best = Some(d);
                }
            }
            best.map(|d| if above { y - d } else { y + d })
        };
        let covered = |line_y: f64| {
            spans.iter().enumerate().any(|(c, &(cy, x0, x1))| {
                upright[c] && (cy - line_y).abs() <= 0.35 * size && x0 <= middle && middle <= x1
            })
        };
        // A corridor is white above and below; a word gap has letters on at
        // least one side (both, as often as not — but the neighbouring line
        // may have a word gap of its own just there). A line on its own keeps
        // the split its gap earned.
        let word_gap = [neighbour(true), neighbour(false)]
            .into_iter()
            .flatten()
            .any(covered);
        if word_gap {
            join.push((a, b));
        }
    }
    if join.is_empty() {
        return runs;
    }
    // Follow each chain of joins from its leftmost piece.
    let mut next: Vec<Option<usize>> = vec![None; runs.len()];
    let mut has_left = vec![false; runs.len()];
    for &(a, b) in &join {
        if next[a].is_none() && !has_left[b] {
            next[a] = Some(b);
            has_left[b] = true;
        }
    }
    let mut slots: Vec<Option<Run>> = runs.into_iter().map(Some).collect();
    let mut out = Vec::new();
    for start in 0..slots.len() {
        if has_left[start] {
            continue;
        }
        let Some(mut run) = slots[start].take() else {
            continue;
        };
        let mut cursor = next[start];
        let mut guard = 0;
        while let Some(b) = cursor {
            guard += 1;
            if guard > slots.len() {
                break;
            }
            let Some(piece) = slots[b].take() else { break };
            run.space(Piece::Space);
            run.pieces.extend(piece.pieces);
            run.last_origin = piece.last_origin;
            run.last_end = piece.last_end;
            run.last_text = piece.last_text;
            run.after_space = piece.after_space;
            run.size = run.size.max(piece.size);
            cursor = next[b];
        }
        out.push(run);
    }
    // Pieces left behind by a broken chain (a cycle cannot happen on one
    // baseline read left to right, but nothing is dropped if it did).
    out.extend(slots.into_iter().flatten());
    out
}

/// Whether `next` continues `run` on its baseline, just after its end.
fn follows(run: &Run, next: &Run) -> bool {
    let turn = (next.angle - run.angle + 540.0).rem_euclid(360.0) - 180.0;
    if turn.abs() > 3.0 {
        return false;
    }
    let size = run.size.max(next.size);
    let b = run.baseline();
    let n = Vec2::new(-b.y, b.x);
    if (next.start - run.start).dot(n).abs() > 0.35 * size {
        return false;
    }
    let gap = (next.start - run.last_end).dot(b);
    (-0.3 * size..=COLUMN_GAP_EM * size).contains(&gap)
}

/// The device that draws nothing and writes down every glyph.
struct Collector {
    layer: TextLayer,
}

impl<'a> Device<'a> for Collector {
    fn set_soft_mask(&mut self, _: Option<SoftMask<'a>>) {}
    fn set_blend_mode(&mut self, _: BlendMode) {}
    fn draw_path(&mut self, path: &BezPath, transform: Affine, _: &Paint<'a>, mode: &PathDrawMode) {
        // Only small, roughly square, filled shapes: a bullet, not a rule,
        // a table border or a letter drawn as outlines.
        if !matches!(mode, PathDrawMode::Fill(_)) {
            return;
        }
        let bounds = transform.transform_rect_bbox(kurbo::Shape::bounding_box(path));
        let (w, h) = (bounds.width(), bounds.height());
        if w >= 1.0 && h >= 1.0 && w <= 60.0 && h <= 60.0 && (0.6..=1.6).contains(&(w / h)) {
            self.layer.marks.push(Rect::new(
                bounds.x0 as f32,
                bounds.y0 as f32,
                bounds.x1 as f32,
                bounds.y1 as f32,
            ));
        }
    }
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}
    fn pop_clip_path(&mut self) {}
    fn pop_transparency_group(&mut self) {}

    fn draw_glyph(
        &mut self,
        glyph: &Glyph<'a>,
        transform: Affine,
        glyph_transform: Affine,
        _: &Paint<'a>,
        draw_mode: &GlyphDrawMode,
    ) {
        let full = transform * glyph_transform;
        let origin = full * KPoint::ZERO;
        let up = full * KPoint::new(0.0, UNITS_PER_EM) - origin;
        if !(up.x.is_finite() && up.y.is_finite()) || up.hypot() < 0.25 {
            return;
        }
        let advance = match glyph {
            Glyph::Outline(g) => g.advance_width().map(f64::from),
            Glyph::Type3(_) => None,
        }
        .filter(|w: &f64| w.is_finite() && *w > 0.0)
        .unwrap_or(0.5 * UNITS_PER_EM);
        let end = full * KPoint::new(advance, 0.0);
        let (text, mapped) = match glyph.as_unicode() {
            Some(BfString::Char(c)) => (c.to_string(), readable(c)),
            Some(BfString::String(s)) => {
                let ok = !s.is_empty() && s.chars().all(readable);
                (s, ok)
            }
            None => ("\u{FFFD}".to_string(), false),
        };
        self.layer.glyphs.push(TextGlyph {
            text,
            origin,
            end,
            up,
            visible: !matches!(draw_mode, GlyphDrawMode::Invisible),
            mapped,
        });
    }

    fn draw_image(&mut self, _: Image<'a, '_>, transform: Affine) {
        // An image is the unit square under its transform.
        let area = transform.determinant().abs();
        if area.is_finite() {
            self.layer.image_area += area;
        }
    }
}

/// A character a text layer should hold: not a replacement or private-use
/// code point, not a control character.
fn readable(c: char) -> bool {
    let code = c as u32;
    !(c == '\u{FFFD}'
        || (c.is_control() && !c.is_whitespace())
        || (0xE000..=0xF8FF).contains(&code)
        || code >= 0xF0000)
}

/// Reads the text layer of `page`, placed as it would be rendered at
/// `scale` pixels per point.
pub(crate) fn read<'a>(
    page: &'a hayro::hayro_syntax::page::Page<'a>,
    cache: &InterpreterCache<'a>,
    scale: f32,
) -> TextLayer {
    let (w_pt, h_pt) = page.render_dimensions();
    let (width, height) = ((w_pt * scale).floor() as f64, (h_pt * scale).floor() as f64);
    let initial = Affine::scale(f64::from(scale)) * page.initial_transform(true).to_kurbo();
    let mut context = Context::new(
        initial,
        kurbo::Rect::new(0.0, 0.0, width, height),
        cache,
        page.xref(),
        InterpreterSettings::default(),
    );
    let mut device = Collector {
        layer: TextLayer {
            width,
            height,
            ..TextLayer::default()
        },
    };
    hayro::hayro_interpret::interpret_page(page, &mut context, &mut device);
    device.layer
}

#[cfg(test)]
mod tests {
    use super::*;

    fn glyph(text: &str, x: f64, y: f64, size: f64) -> TextGlyph {
        TextGlyph {
            text: text.to_string(),
            origin: KPoint::new(x, y),
            end: KPoint::new(x + 0.5 * size, y),
            up: Vec2::new(0.0, -size),
            visible: true,
            mapped: true,
        }
    }

    fn layer(glyphs: Vec<TextGlyph>) -> TextLayer {
        TextLayer {
            glyphs,
            marks: Vec::new(),
            image_area: 0.0,
            width: 1000.0,
            height: 1000.0,
        }
    }

    fn word(text: &str, x: f64, y: f64, size: f64) -> Vec<TextGlyph> {
        text.chars()
            .enumerate()
            .map(|(i, c)| glyph(&c.to_string(), x + i as f64 * 0.5 * size, y, size))
            .collect()
    }

    #[test]
    fn a_bullet_drawn_as_a_shape_is_put_back() {
        let mut page = layer(word("Frühwarnsystem", 100.0, 200.0, 20.0));
        // A 6 px dot 15 px left of the line, at the height of its x-height.
        page.marks.push(Rect::new(79.0, 186.0, 85.0, 192.0));
        let lines = page.lines(false);
        assert_eq!(lines[0].text, "• Frühwarnsystem");
        assert_eq!(lines[0].words[0].text, "•");
    }

    #[test]
    fn glyphs_on_a_baseline_become_a_line_with_words() {
        let mut glyphs = word("Projekt", 100.0, 200.0, 20.0);
        glyphs.extend(word("Adler", 100.0 + 7.0 * 10.0 + 6.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "Projekt Adler");
        assert_eq!(lines[0].words.len(), 2);
        assert_eq!(lines[0].confidence, 1.0);
    }

    #[test]
    fn a_wide_gap_splits_a_line_like_the_detector_does() {
        let mut glyphs = word("Name", 100.0, 200.0, 20.0);
        glyphs.extend(word("Wert", 100.0 + 40.0 + 25.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(
            lines.iter().map(|l| l.text.as_str()).collect::<Vec<_>>(),
            ["Name", "Wert"]
        );
    }

    #[test]
    fn a_justified_word_space_does_not_split_the_line() {
        // "Werkstore" then a space glyph, then "mit" two ems further on: a
        // narrow justified column, not two cells.
        let mut glyphs = word("Werkstore", 100.0, 200.0, 20.0);
        glyphs.push(glyph(" ", 190.0, 200.0, 20.0));
        glyphs.extend(word("mit", 190.0 + 10.0 + 40.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "Werkstore mit");
    }

    #[test]
    fn a_stretched_word_gap_is_joined_where_the_lines_around_it_have_letters() {
        // Three justified lines of one narrow column: every line has one wide
        // gap, at a different place, with no space glyph in it.
        let rows = [
            ("Seit", "Anfangszeit"),
            ("Werkstore", "mit"),
            ("Um", "stellungen"),
        ];
        let mut glyphs = Vec::new();
        for (i, (left, right)) in rows.iter().enumerate() {
            let y = 200.0 + i as f64 * 30.0;
            glyphs.extend(word(left, 100.0, y, 20.0));
            let end = 100.0 + left.chars().count() as f64 * 10.0;
            glyphs.extend(word(right, end + 30.0 + i as f64 * 7.0, y, 20.0));
        }
        let lines = layer(glyphs).lines(false);
        let texts: Vec<&str> = lines.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(
            texts,
            ["Seit Anfangszeit", "Werkstore mit", "Um stellungen"]
        );
    }

    /// Words with a space glyph between each two, from `x` on.
    fn spaced(text: &str, x: f64, y: f64, size: f64) -> Vec<TextGlyph> {
        let mut glyphs = Vec::new();
        let mut at = x;
        for (i, w) in text.split(' ').enumerate() {
            if i > 0 {
                glyphs.push(glyph(" ", at, y, size));
                at += 0.25 * size;
            }
            glyphs.extend(word(w, at, y, size));
            at += w.chars().count() as f64 * 0.5 * size;
        }
        glyphs
    }

    #[test]
    fn a_gap_with_no_space_in_it_ends_a_cell_where_the_page_sets_spaces() {
        // A browser's table: the words in a cell have space glyphs between
        // them, the cells only their positions — here less than an em apart.
        let mut glyphs = spaced("Die Kosten des Projekts im Überblick", 100.0, 100.0, 20.0);
        glyphs.extend(spaced("Elektro", 100.0, 200.0, 20.0));
        glyphs.extend(spaced("120.000 €", 100.0 + 70.0 + 15.0, 200.0, 20.0));
        glyphs.extend(spaced(
            "98.500 €",
            100.0 + 70.0 + 15.0 + 85.0 + 15.0,
            200.0,
            20.0,
        ));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines[1].text, "Elektro 120.000 € 98.500 €");
        let cells: Vec<&str> = lines[1].segments.iter().map(|s| s.text.as_str()).collect();
        assert_eq!(cells, ["Elektro", "120.000 €", "98.500 €"]);
        assert!(lines[0].segments.is_empty(), "prose is one box");
        // The words of one cell meet; the cells keep their gap.
        let words = &lines[1].words;
        assert_eq!(words[1].bbox.x1, words[2].bbox.x0);
        assert!(words[1].bbox.x0 - words[0].bbox.x1 >= 14.0);
    }

    #[test]
    fn a_longer_cell_below_does_not_join_a_short_one_to_its_neighbour() {
        // "Elektro" is two ems short of the next column, and "Trockenbau"
        // below fills most of that: to the corridor test it looks like a
        // stretched word space. No space glyph was set there, so it is not.
        let mut glyphs = spaced("Die Kosten des Projekts im Überblick", 100.0, 100.0, 20.0);
        for (i, (label, figure)) in [("Elektro", "120.000 €"), ("Trockenbau", "45.000 €")]
            .iter()
            .enumerate()
        {
            let y = 200.0 + i as f64 * 30.0;
            glyphs.extend(spaced(label, 100.0, y, 20.0));
            glyphs.extend(spaced(figure, 100.0 + 100.0 + 12.0, y, 20.0));
        }
        let lines = layer(glyphs).lines(false);
        let texts: Vec<&str> = lines.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(
            texts,
            [
                "Die Kosten des Projekts im Überblick",
                "Elektro",
                "120.000 €",
                "Trockenbau 45.000 €",
            ]
        );
        assert_eq!(lines[3].segments.len(), 2);
    }

    #[test]
    fn a_page_without_space_glyphs_has_words_in_its_gaps() {
        let mut glyphs = word("Elektro", 100.0, 200.0, 20.0);
        glyphs.extend(word("montiert", 100.0 + 70.0 + 6.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines[0].text, "Elektro montiert");
        assert!(lines[0].segments.is_empty());
    }

    #[test]
    fn a_bullet_goes_into_the_first_cell_too() {
        let mut glyphs = spaced("Ein Satz mit sehr vielen Wörtern darin", 100.0, 100.0, 20.0);
        glyphs.extend(spaced("Punkt", 100.0, 200.0, 20.0));
        glyphs.extend(spaced("offen", 100.0 + 50.0 + 15.0, 200.0, 20.0));
        let mut page = layer(glyphs);
        page.marks.push(Rect::new(79.0, 186.0, 85.0, 192.0));
        let lines = page.lines(false);
        assert_eq!(lines[1].text, "• Punkt offen");
        assert_eq!(lines[1].segments[0].text, "• Punkt");
    }

    #[test]
    fn a_corridor_through_the_rows_keeps_columns_apart() {
        // Two columns: the gap is white in every row.
        let mut glyphs = Vec::new();
        for i in 0..3 {
            let y = 200.0 + i as f64 * 30.0;
            glyphs.extend(word("links", 100.0, y, 20.0));
            glyphs.extend(word("rechts", 100.0 + 50.0 + 40.0, y, 20.0));
        }
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines.len(), 6);
    }

    #[test]
    fn text_drawn_out_of_order_is_put_back_in_order() {
        let mut glyphs = word("Adler", 100.0 + 7.0 * 10.0 + 6.0, 200.0, 20.0);
        glyphs.extend(word("Projekt", 100.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "Projekt Adler");
    }

    #[test]
    fn fake_bold_is_one_character_not_two() {
        let mut glyphs = Vec::new();
        for g in word("GEHEIM", 100.0, 200.0, 20.0) {
            let mut twin = g.clone();
            twin.origin.x += 0.5;
            twin.end.x += 0.5;
            glyphs.push(g);
            glyphs.push(twin);
        }
        assert_eq!(layer(glyphs).lines(false)[0].text, "GEHEIM");
    }

    #[test]
    fn two_baselines_are_two_lines() {
        let mut glyphs = word("oben", 100.0, 200.0, 20.0);
        glyphs.extend(word("unten", 100.0, 230.0, 20.0));
        assert_eq!(layer(glyphs).lines(false).len(), 2);
    }

    #[test]
    fn a_scan_with_an_invisible_ocr_layer_is_not_trusted_in_auto() {
        let mut glyphs = word("Rechnung", 100.0, 200.0, 20.0);
        for g in &mut glyphs {
            g.visible = false;
        }
        let mut page = layer(glyphs);
        page.image_area = 1000.0 * 1000.0;
        assert!(!page.usable(PdfText::Auto));
        assert!(page.usable(PdfText::Always));
        assert!(page.lines(false).is_empty());
        assert_eq!(page.lines(true)[0].text, "Rechnung");
    }

    #[test]
    fn a_picture_page_or_an_unmapped_font_is_recognized_instead() {
        let mut picture = layer(word("Foto", 100.0, 200.0, 20.0));
        picture.image_area = 0.8 * 1000.0 * 1000.0;
        assert!(!picture.usable(PdfText::Auto));

        let mut glyphs = word("Vertrag", 100.0, 200.0, 20.0);
        glyphs[2].mapped = false;
        assert!(!layer(glyphs).usable(PdfText::Auto));

        assert!(layer(word("Vertrag", 100.0, 200.0, 20.0)).usable(PdfText::Auto));
        assert!(!layer(word("Vertrag", 100.0, 200.0, 20.0)).usable(PdfText::Never));
    }

    #[test]
    fn modes_parse() {
        assert_eq!(PdfText::parse("Auto"), Some(PdfText::Auto));
        assert_eq!(PdfText::parse("never"), Some(PdfText::Never));
        assert_eq!(PdfText::parse("always"), Some(PdfText::Always));
        assert_eq!(PdfText::parse("sometimes"), None);
    }

    #[test]
    fn replacement_and_private_use_characters_are_not_readable() {
        assert!(readable('ä'));
        assert!(readable(' '));
        assert!(!readable('\u{FFFD}'));
        assert!(!readable('\u{E001}'));
        assert!(!readable('\u{0007}'));
    }
}
