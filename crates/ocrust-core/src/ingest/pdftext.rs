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

use std::collections::{HashMap, HashSet};

use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::hayro_cmap::{BfString, CMap};
use hayro::hayro_interpret::hayro_syntax::content::ops::TypedInstruction;
use hayro::hayro_interpret::hayro_syntax::content::TypedIter;
use hayro::hayro_interpret::hayro_syntax::object::{
    Array, Dict, Name, Object, Stream, String as PdfString,
};
use hayro::hayro_interpret::hayro_syntax::page::{Page, Resources};
use hayro::hayro_interpret::{
    BlendMode, CacheKey, ClipPath, Context, Device, GlyphDrawMode, Image, InterpreterCache,
    InterpreterSettings, Paint, PathDrawMode, SoftMask, TransformExt,
};
use kurbo::{Affine, BezPath, PathEl, Point as KPoint, Vec2};
use unicode_normalization::char::{canonical_combining_class, is_combining_mark};
use unicode_normalization::UnicodeNormalization;

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
    /// Use the text layer and nothing else: a page without one comes back
    /// empty. Needs no recognition models.
    Only,
}

impl PdfText {
    /// `"never"`, `"auto"` or `"always"`.
    pub fn parse(name: &str) -> Option<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "never" | "off" | "no" => Some(Self::Never),
            "auto" => Some(Self::Auto),
            "always" | "on" | "yes" => Some(Self::Always),
            "only" | "text" => Some(Self::Only),
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
    /// Whether `text` is what the page says the glyph is (`/ActualText`, see
    /// [`Collector::read_actual_text`]) rather than the character drawn: it
    /// is in the order it is read, and a bracket in it faces the way it
    /// reads, where the one drawn in text read right to left is mirrored.
    actual: bool,
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
    /// Straight lines across or down the page, drawn as strokes or as thin
    /// filled bars: a table's borders, the rules under a header, a
    /// signature line. A rule between two lines of a table says a row ends
    /// there; its absence says a cell's text wrapped.
    rules: Vec<Rect>,
    /// Page area covered by raster images, in square pixels.
    image_area: f64,
    width: f64,
    height: f64,
}

impl TextLayer {
    /// Turns the layer so that the text most of the page is set in reads left
    /// to right: a table set sideways, a landscape page in a portrait
    /// document, a page rotated over upright content. The page is read as it
    /// is meant to be read; lines set otherwise stay at their angle.
    fn turn_upright(&mut self) {
        let mut quarters = [0usize; 4];
        for glyph in self.glyphs.iter().filter(|g| !g.text.trim().is_empty()) {
            let quarter = ((glyph.angle() / 90.0).round() as i64).rem_euclid(4) as usize;
            quarters[quarter] += 1;
        }
        let total: usize = quarters.iter().sum();
        let (quarter, count) = quarters
            .iter()
            .copied()
            .enumerate()
            .max_by_key(|&(_, n)| n)
            .unwrap_or((0, 0));
        if quarter == 0 || total == 0 || count * 10 < total * 6 {
            return;
        }
        let (w, h) = (self.width, self.height);
        // Each point turned back by the text's angle, into the turned page.
        let point = |p: KPoint| match quarter {
            1 => KPoint::new(p.y, w - p.x),
            2 => KPoint::new(w - p.x, h - p.y),
            _ => KPoint::new(h - p.y, p.x),
        };
        let vector = |v: Vec2| match quarter {
            1 => Vec2::new(v.y, -v.x),
            2 => Vec2::new(-v.x, -v.y),
            _ => Vec2::new(-v.y, v.x),
        };
        for glyph in &mut self.glyphs {
            glyph.origin = point(glyph.origin);
            glyph.end = point(glyph.end);
            glyph.up = vector(glyph.up);
        }
        for mark in self.marks.iter_mut().chain(self.rules.iter_mut()) {
            let a = point(KPoint::new(f64::from(mark.x0), f64::from(mark.y0)));
            let b = point(KPoint::new(f64::from(mark.x1), f64::from(mark.y1)));
            *mark = Rect::new(
                a.x.min(b.x) as f32,
                a.y.min(b.y) as f32,
                a.x.max(b.x) as f32,
                a.y.max(b.y) as f32,
            );
        }
        if quarter != 2 {
            std::mem::swap(&mut self.width, &mut self.height);
        }
    }

    /// Turns text set in columns — Japanese and Chinese set vertically,
    /// upright glyphs one below the other, the columns right to left — into
    /// text set on its side: each glyph of a column becomes a glyph of a line
    /// running down the page, the way a line runs on a page turned a quarter
    /// to the right. Such lines are then read like any other turned text, and
    /// a page set so is turned upright ([`TextLayer::turn_upright`]) with its
    /// columns in order, the right one first. Read as it is drawn, each glyph
    /// is a line of its own, and the page is read across its columns.
    ///
    /// A column is three or more glyphs drawn one after the other, each
    /// about an em below the one before, mostly of those scripts; a table's
    /// column of short cells is drawn row by row, and its rows are further
    /// apart. And a column stands beside another, as columns of text do, one
    /// of the two six glyphs long at least: a paragraph set so narrow that
    /// each of its lines holds one character is a column too, but a single
    /// one, the next paragraph under it and not beside it.
    fn stand_vertical_text(&mut self) {
        /// How far above its baseline an ideograph's em box reaches, in ems.
        const IDEOGRAPH_TOP_EM: f64 = 0.88;
        // How far `b` starts below `a`, where it is the next glyph of a column.
        let below = |a: &TextGlyph, b: &TextGlyph| {
            let size = a.size();
            let down = a.up * (-1.0 / size.max(f64::EPSILON));
            let v = b.origin - a.origin;
            let step = v.dot(down);
            let same = a.visible == b.visible && (b.size() - size).abs() <= 0.1 * size;
            let upright = a.angle().abs() <= 3.0 && b.angle().abs() <= 3.0;
            (same && upright && v.dot(a.baseline()).abs() <= 0.2 * size)
                .then_some(step)
                .filter(|step| (0.6 * size..=1.35 * size).contains(step))
        };
        // Each column: its first glyph, and how far each next one is below.
        let mut columns: Vec<(usize, Vec<f64>)> = Vec::new();
        let mut i = 0;
        while i < self.glyphs.len() {
            let mut steps: Vec<f64> = Vec::new();
            while let Some(step) = self
                .glyphs
                .get(i + steps.len() + 1)
                .and_then(|next| below(&self.glyphs[i + steps.len()], next))
            {
                steps.push(step);
            }
            let column = &self.glyphs[i..=i + steps.len()];
            let cjk = column
                .iter()
                .filter(|g| g.text.chars().any(ideographic))
                .count();
            if steps.len() >= 2 && cjk * 3 >= column.len() * 2 {
                columns.push((i, steps.clone()));
            }
            i += steps.len() + 1;
        }
        // Where each column stands: its middle across, its top and bottom
        // (the glyphs are upright), its size, and how many glyphs it holds.
        let places: Vec<(f64, f64, f64, f64, usize)> = columns
            .iter()
            .map(|(first, steps)| {
                let (a, b) = (&self.glyphs[*first], &self.glyphs[first + steps.len()]);
                let across = (a.origin.x + a.end.x) / 2.0;
                (across, a.origin.y, b.origin.y, a.size(), steps.len() + 1)
            })
            .collect();
        let beside = |k: usize| {
            let (x, top, bottom, size, count) = places[k];
            places
                .iter()
                .enumerate()
                .any(|(j, &(x2, top2, bottom2, size2, count2))| {
                    let apart = (x - x2).abs();
                    j != k
                        && apart >= 0.8 * size.min(size2)
                        && apart <= 4.0 * size.max(size2)
                        && bottom.min(bottom2) - top.max(top2) >= size.min(size2)
                        && count.max(count2) >= 6
                })
        };
        let standing: Vec<bool> = (0..columns.len()).map(beside).collect();
        for ((first, steps), stand) in columns.iter().zip(standing) {
            if !stand {
                continue;
            }
            for (k, glyph) in self.glyphs[*first..=first + steps.len()]
                .iter_mut()
                .enumerate()
            {
                let size = glyph.size();
                // Up turns to point right, the way the column reads on.
                let up = Vec2::new(-glyph.up.y, glyph.up.x);
                let middle = glyph.origin + (glyph.end - glyph.origin) * 0.5;
                // The top of the glyph's em box, a little left of its middle:
                // the box the line gets is centred on the column.
                let origin =
                    middle + glyph.up * IDEOGRAPH_TOP_EM - up * (0.5 * (ASCENT_EM - DESCENT_EM));
                let down = glyph.up * (-1.0 / size.max(f64::EPSILON));
                let step = steps.get(k).copied().unwrap_or(size);
                glyph.origin = origin;
                glyph.end = origin + down * step;
                glyph.up = up;
            }
        }
    }

    /// The straight lines drawn across or down the page (see
    /// [`TextLayer::rules`]), in the coordinates of its lines.
    pub fn rules(&self) -> Vec<Rect> {
        self.rules.clone()
    }

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
            PdfText::Only => true,
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
                self.mapped_enough()
            }
        }
    }

    /// Whether a page read without recognizing anything holds text rather
    /// than boxes: nineteen in twenty of its characters, shown and hidden
    /// together, have their Unicode. A stamp in a symbol font over a scan's
    /// hidden text is no reason to leave the page empty; a font that maps
    /// only some of its letters is.
    pub fn readable_enough(&self) -> bool {
        let printed: Vec<&TextGlyph> = self
            .glyphs
            .iter()
            .filter(|g| !g.text.trim().is_empty())
            .collect();
        let mapped = printed.iter().filter(|g| g.mapped).count();
        mapped * 20 >= printed.len() * 19
    }

    /// Whether the text a page shows has its characters: a font without a
    /// Unicode mapping reads as boxes, and one wrong character in twenty is
    /// worse than recognizing the page.
    pub fn mapped_enough(&self) -> bool {
        let printed = self.glyphs.iter().filter(|g| !g.text.trim().is_empty());
        let visible: Vec<&TextGlyph> = printed.clone().filter(|g| g.visible).collect();
        let shown: Vec<&TextGlyph> = if visible.is_empty() {
            printed.collect()
        } else {
            visible
        };
        let mapped = shown.iter().filter(|g| g.mapped).count();
        mapped * 20 >= shown.len() * 19
    }

    /// The layer's text as lines, the way the text detector delivers them:
    /// one line per run of glyphs on one baseline, split where a gap is wide
    /// enough to be a column or a table cell rather than a space.
    pub fn lines(&self, include_invisible: bool) -> Vec<Line> {
        let mut lines = self.lines_of(true);
        if include_invisible {
            // An invisible layer is somebody's OCR. Over a scan it is all the
            // text there is; over text the page shows, it is the same words a
            // second time, and only what no shown line covers is kept.
            // Only a shown line running the same way can cover a hidden one: the
            // box of a watermark set across the page covers most of it.
            let shown: Vec<(Rect, f32)> = lines.iter().map(|l| (l.bbox, l.angle)).collect();
            lines.extend(self.lines_of(false).into_iter().filter(|line| {
                let area = (line.bbox.width() * line.bbox.height()).max(1.0);
                !shown.iter().any(|(b, angle)| {
                    (angle - line.angle).abs() <= 5.0
                        && b.horizontal_overlap(&line.bbox) * b.vertical_overlap(&line.bbox)
                            >= 0.5 * area
                })
            }));
        }
        self.restore_bullets(&mut lines);
        attach_glyph_bullets(&mut lines);
        lines
    }

    /// The lines of the glyphs drawn visibly, or of those drawn invisibly.
    fn lines_of(&self, visible: bool) -> Vec<Line> {
        let (width, height) = (self.width, self.height);
        let on_page = |g: &&TextGlyph| {
            let c = g.origin + g.up * 0.3;
            c.x >= -2.0 && c.y >= -2.0 && c.x <= width + 2.0 && c.y <= height + 2.0
        };
        let glyphs: Vec<&TextGlyph> = self
            .glyphs
            .iter()
            .filter(|g| g.visible == visible && g.size() >= 1.0)
            .filter(on_page)
            .collect();
        let glyphs = cluster_marks(drop_twins(glyphs));

        // Runs in drawing order: a PDF draws a line's glyphs one after the
        // other, far more often than not.
        let mut runs: Vec<Run> = Vec::new();
        for glyph in &glyphs {
            let joined = runs.last_mut().is_some_and(|run| run.take(glyph));
            if !joined {
                runs.push(Run::new(glyph));
            }
        }
        // Each run's blanks are word gaps or its letter spacing, and on text
        // the page shows, its raised and lowered glyphs scripts. Not so on an
        // invisible layer: that is somebody's OCR, its sizes and baselines
        // fitted word by word, and a word set a little smaller and lower
        // there is no subscript.
        for run in &mut runs {
            run.settle(visible);
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
                    Piece::Char(..) | Piece::Blank(_) => (s, g),
                });
        let gaps_are_cells = spaces >= SPACES_SET || spaces * 3 >= gaps.max(1);
        let runs = merge_runs(runs);
        let runs = join_word_gaps(runs, gaps_are_cells);
        let direction = page_direction(&runs);
        runs.into_iter()
            .filter_map(|run| run.into_line(gaps_are_cells, direction))
            .collect()
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
            // A line read right to left has its bullet at its right.
            let rtl = {
                let rights = line.text.chars().filter(|&c| right_to_left(c)).count();
                let lefts = line
                    .text
                    .chars()
                    .filter(|&c| c.is_alphabetic() && !right_to_left(c))
                    .count();
                rights > lefts
            };
            let found = self.marks.iter().enumerate().find(|(i, mark)| {
                let size = mark.width().max(mark.height());
                let centre = (mark.y0 + mark.y1) / 2.0;
                let beside = if rtl {
                    mark.x0 >= line.bbox.x1 - 1.0 && mark.x0 - line.bbox.x1 <= 2.5 * height
                } else {
                    mark.x1 <= line.bbox.x0 + 1.0 && line.bbox.x0 - mark.x1 <= 2.5 * height
                };
                !used[*i]
                    && size <= 0.6 * height
                    && beside
                    && (centre - middle).abs() <= 0.3 * height
            });
            if let Some((i, mark)) = found {
                used[i] = true;
                line.text = format!("• {}", line.text);
                let bullet = Word {
                    text: "•".to_string(),
                    bbox: *mark,
                    confidence: 1.0,
                };
                // Words stay in page order, left to right.
                if rtl {
                    line.words.push(bullet);
                } else {
                    line.words.insert(0, bullet);
                }
                line.bbox = line.bbox.union(mark);
                let opening = if rtl {
                    line.segments.last_mut()
                } else {
                    line.segments.first_mut()
                };
                if let Some(first) = opening {
                    first.text = format!("• {}", first.text);
                    first.bbox = first.bbox.union(mark);
                }
            }
        }
    }
}

/// Characters a list item is drawn with as a glyph of their own.
const GLYPH_BULLETS: &[&str] = &["•", "▪", "●", "■", "◦", "‣", "➢", "∙"];

/// Puts a bullet drawn as a glyph of its own, an em or two left of its item —
/// a symbol font's bullet, set at a tab stop — back in front of the item: a
/// list, not a table of bullets and items.
fn attach_glyph_bullets(lines: &mut Vec<Line>) {
    let is_bullet = |text: &str| GLYPH_BULLETS.contains(&text.trim());
    // The bullet a cell of its own within the item's line.
    for line in lines.iter_mut() {
        let height = line.bbox.height();
        if line.segments.len() >= 2
            && is_bullet(&line.segments[0].text)
            && line.segments[1].bbox.x0 - line.segments[0].bbox.x1 <= 2.5 * height
        {
            let bullet = line.segments.remove(0);
            let first = &mut line.segments[0];
            first.text = format!("{} {}", bullet.text.trim(), first.text);
            first.bbox = first.bbox.union(&bullet.bbox);
        }
    }
    // The bullet a line of its own beside the item's.
    let mut index = 0;
    while index < lines.len() {
        let bullet = &lines[index];
        let height = bullet.bbox.height();
        if !is_bullet(&bullet.text) || bullet.angle.abs() > 3.0 {
            index += 1;
            continue;
        }
        let item = (0..lines.len())
            .filter(|&other| other != index)
            .filter(|&other| {
                let line = &lines[other];
                let overlap = line.bbox.vertical_overlap(&bullet.bbox);
                overlap >= 0.5 * height.min(line.bbox.height())
                    && line.bbox.x0 >= bullet.bbox.x1 - 1.0
                    && line.bbox.x0 - bullet.bbox.x1 <= 2.5 * height.max(line.bbox.height())
            })
            .min_by(|&a, &b| lines[a].bbox.x0.total_cmp(&lines[b].bbox.x0));
        let Some(item) = item else {
            index += 1;
            continue;
        };
        let bullet = lines.remove(index);
        let item = &mut lines[if item > index { item - 1 } else { item }];
        let mark = bullet.text.trim().to_string();
        item.text = format!("{mark} {}", item.text);
        item.words.insert(
            0,
            Word {
                text: mark.clone(),
                bbox: bullet.bbox,
                confidence: 1.0,
            },
        );
        if let Some(first) = item.segments.first_mut() {
            first.text = format!("{mark} {}", first.text);
            first.bbox = first.bbox.union(&bullet.bbox);
        }
        item.bbox = item.bbox.union(&bullet.bbox);
    }
}

/// One glyph of a run: its characters, its box, and where it sits.
struct Letter {
    text: String,
    bbox: Rect,
    /// Where the glyph starts on its baseline.
    origin: KPoint,
    size: f64,
    /// Whether `text` is as it is read (see [`TextGlyph::actual`]).
    actual: bool,
}

/// What a run is made of, left to right.
enum Piece {
    Char(Letter),
    /// A space the document set, or one the lines around it vouch for.
    Space,
    /// White space nothing fills: a word space on a page that never sets its
    /// own, the edge of a table cell on one that does.
    Gap,
    /// The white between two letters, in ems of the smaller: a word gap, or
    /// only the letter spacing the run is set with. [`Run::settle`] decides,
    /// once the whole run is known.
    Blank(f64),
}

/// Glyphs on one baseline, close enough together to be one piece of text.
struct Run {
    pieces: Vec<Piece>,
    start: KPoint,
    /// A point on the run's baseline: where its first glyph starts, or the
    /// first glyph of the text a leading superscript opens — a footnote's
    /// number is set small and raised, and the line is the footnote's.
    base: KPoint,
    last_origin: KPoint,
    last_end: KPoint,
    up: Vec2,
    angle: f64,
    size: f64,
    /// Whether the last glyph was a space the document set itself.
    after_space: bool,
    /// Whether the first glyph was: the run has no piece for it, and the
    /// text drawn before it is a word apart however near it ends — a
    /// list's number, drawn with the space after it and apart from its
    /// item, is set right of the item in a line read right to left.
    opens_with_space: bool,
    /// The letter spacing the run is set with, in ems (see [`Run::settle`]).
    tracking: f64,
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
/// A gap wider than this many ems, past the run's own letter spacing, is a
/// space between two words.
const SPACE_GAP_EM: f64 = 0.15;
/// The widest letter spacing, in ems, of a run without space glyphs. White
/// that wide between every two letters is no tracking but the gaps of a row
/// of one-letter cells.
const MAX_TRACKING_EM: f64 = 0.5;
/// The widest a space glyph is taken to be stretched, in ems, where the
/// lines around it have letters in the gap.
const STRETCHED_SPACE_EM: f64 = 12.0;
/// How far a run drawn later may start after the end of another to carry on
/// its line, in ems.
const MERGE_GAP_EM: f64 = 0.6;
/// How many times larger than the text beside it a glyph standing on its own
/// is a drop cap: the first letter of a paragraph, set three lines tall on
/// the baseline of the paragraph's third line, and no letter of that line.
const DROP_CAP_FACTOR: f64 = 1.6;
/// Space glyphs enough to say a page sets its own spaces whatever else it
/// has: a table of one-word cells has more gaps than spaces all the same.
const SPACES_SET: usize = 8;

impl Run {
    fn new(glyph: &TextGlyph) -> Self {
        let mut run = Run {
            pieces: Vec::new(),
            start: glyph.origin,
            base: glyph.origin,
            last_origin: glyph.origin,
            last_end: glyph.end,
            up: glyph.up,
            angle: glyph.angle(),
            size: glyph.size(),
            after_space: false,
            opens_with_space: glyph.text.trim().is_empty(),
            tracking: 0.0,
        };
        run.push(glyph);
        run
    }

    fn push(&mut self, glyph: &TextGlyph) {
        self.after_space = glyph.text.trim().is_empty();
        if self.after_space {
            self.space(Piece::Space);
        } else {
            self.pieces.push(Piece::Char(Letter {
                text: glyph.text.clone(),
                bbox: glyph.bbox(),
                origin: glyph.origin,
                size: glyph.size(),
                actual: glyph.actual,
            }));
        }
        self.last_origin = glyph.origin;
        self.last_end = glyph.end;
        self.size = self.size.max(glyph.size());
    }

    /// Ends the word so far with `piece`, a [`Piece::Space`] or a
    /// [`Piece::Gap`]. A space glyph drawn into a gap makes it a space.
    fn space(&mut self, piece: Piece) {
        match self.pieces.last_mut() {
            Some(Piece::Char(..)) => self.pieces.push(piece),
            Some(last @ (Piece::Gap | Piece::Blank(_))) if matches!(piece, Piece::Space) => {
                *last = piece
            }
            _ => {}
        }
    }

    /// How many glyphs with characters the run holds.
    fn letters(&self) -> usize {
        self.pieces
            .iter()
            .filter(|p| matches!(p, Piece::Char(..)))
            .count()
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
        // Off the baseline by more than a third of an em: another line —
        // unless it is a superscript or a subscript, or the text after one.
        if (glyph.origin - self.last_origin).dot(n).abs() > 0.35 * size && !self.script(glyph) {
            return false;
        }
        // Measured in the smaller of the two sizes: a heading's last word
        // and the first cell of the table beside it are an em of the heading
        // apart, and far more than one of the table's.
        let small = self.size.min(glyph.size());
        let letter = !glyph.text.trim().is_empty();
        // A space the next letter is drawn over took no room: a soft hyphen,
        // which browsers print as a space glyph of no width inside its word
        // (and in front of the hyphen they draw where a line breaks at one).
        // It is no word break.
        if letter
            && matches!(self.pieces.last(), Some(Piece::Space))
            && (glyph.origin - self.last_origin).dot(b).abs() < 0.1 * small
        {
            self.pieces.pop();
            self.push(glyph);
            return true;
        }
        let gap = (glyph.origin - self.last_end).dot(b);
        let widest = if self.after_space {
            COLUMN_GAP_AFTER_SPACE_EM
        } else {
            COLUMN_GAP_EM
        };
        if gap < -0.5 * size || gap > widest * small {
            return false;
        }
        if letter && matches!(self.pieces.last(), Some(Piece::Char(..))) {
            self.pieces
                .push(Piece::Blank(gap / small.max(f64::EPSILON)));
        }
        self.push(glyph);
        true
    }

    /// Whether `glyph` sits on this run's line although it is off the last
    /// glyph's baseline: a superscript or a subscript — set smaller, raised
    /// up to 0.6 em or lowered up to a third of one, the way `mc²`, `r³` and a
    /// footnote mark are — or the text right after one, back on the line.
    /// Or the text a few such marks open, set larger and lower: then the
    /// line's baseline is the text's (see [`Run::base`]).
    fn script(&mut self, glyph: &TextGlyph) -> bool {
        let up = self.up / self.up.hypot().max(f64::EPSILON);
        let raised = (glyph.origin - self.base).dot(up);
        let body = self.size;
        let smaller = glyph.size() <= 0.85 * body;
        if (smaller && raised >= -0.35 * body && raised <= 0.6 * body) || raised.abs() <= 0.2 * body
        {
            return true;
        }
        let larger = 0.85 * glyph.size() >= body;
        let below = -raised;
        if larger
            && self.letters() <= 3
            && below >= 0.1 * glyph.size()
            && below <= 0.6 * glyph.size()
        {
            self.base = glyph.origin;
            self.up = glyph.up;
            return true;
        }
        false
    }

    /// Settles what the run's blanks are, once the whole run is known.
    ///
    /// Text set with letter spacing — tracked capitals, a title "expanded by
    /// 5 pt", `letter-spacing` on a web page's heading — has as much white
    /// between any two of its letters as a word space would. The run's own
    /// spacing is the white most of its letters have between them at least
    /// (the lower quartile), and only a blank clearly wider than that is a
    /// word gap. The letters of ordinary text touch, and every blank past
    /// [`SPACE_GAP_EM`] is one, as it always was. A row of one-digit or
    /// one-letter cells has even gaps too: numbers are left out, and a run
    /// without space glyphs between its words is spaced [`MAX_TRACKING_EM`]
    /// at most.
    ///
    /// Then, on text the page shows (`scripts`), its superscripts and
    /// subscripts are written as such (see [`Run::mark_scripts`]).
    fn settle(&mut self, scripts: bool) {
        let mut blanks: Vec<f64> = self
            .pieces
            .iter()
            .filter_map(|p| match p {
                Piece::Blank(w) => Some(*w),
                _ => None,
            })
            .collect();
        let (letters, alphabetic) = self.pieces.iter().fold((0, 0), |(n, a), p| match p {
            Piece::Char(l) => (
                n + 1,
                a + usize::from(l.text.chars().any(char::is_alphabetic)),
            ),
            _ => (n, a),
        });
        // Where space glyphs part the words, the white between the letters
        // of a word is letter spacing however wide it is.
        let widest = if self.pieces.iter().any(|p| matches!(p, Piece::Space)) {
            COLUMN_GAP_EM
        } else {
            MAX_TRACKING_EM
        };
        self.tracking = if blanks.len() >= 3 && alphabetic * 2 >= letters {
            blanks.sort_by(f64::total_cmp);
            let quartile = blanks[blanks.len() / 4];
            if quartile <= widest {
                quartile.max(0.0)
            } else {
                0.0
            }
        } else {
            0.0
        };
        let wide = SPACE_GAP_EM + self.tracking;
        self.pieces = std::mem::take(&mut self.pieces)
            .into_iter()
            .filter_map(|p| match p {
                Piece::Blank(w) if w > wide => Some(Piece::Gap),
                Piece::Blank(_) => None,
                other => Some(other),
            })
            .collect();
        if scripts {
            self.mark_scripts();
        }
    }

    /// Writes the run's superscripts and subscripts the way a reader sees
    /// them and the other readers write them: `10⁶`, `mc²`, `H₂O`, a
    /// footnote's `¹`. Read as they are drawn, `3·10⁶` is `3·106`, a
    /// different number. A script is a glyph set smaller than the text on
    /// the run's baseline, raised or lowered off it, and right after a letter
    /// of the line or opening the run — as a footnote's number opens the
    /// footnote. It is written in the characters Unicode has for it where it
    /// has one for every character, and as it is otherwise.
    fn mark_scripts(&mut self) {
        let up = self.up / self.up.hypot().max(f64::EPSILON);
        let base = self.base;
        let offset = |l: &Letter| (l.origin - base).dot(up);
        let body = self
            .pieces
            .iter()
            .filter_map(|p| match p {
                Piece::Char(l) if offset(l).abs() <= 0.1 * l.size => Some(l.size),
                _ => None,
            })
            .fold(0.0, f64::max);
        if body <= 0.0 {
            return;
        }
        // 1 raised, -1 lowered, 0 on the line.
        let shift = |p: &Piece| match p {
            Piece::Char(l) if l.size <= 0.85 * body => {
                let off = offset(l) / body;
                if (0.15..=0.7).contains(&off) {
                    1
                } else if (-0.45..=-0.08).contains(&off) {
                    -1
                } else {
                    0
                }
            }
            _ => 0,
        };
        let mut i = 0;
        while i < self.pieces.len() {
            let s = shift(&self.pieces[i]);
            if s == 0 {
                i += 1;
                continue;
            }
            let mut end = i;
            while end < self.pieces.len() && shift(&self.pieces[end]) == s {
                end += 1;
            }
            let attached = i == 0 || matches!(self.pieces[i - 1], Piece::Char(_));
            let form = if s > 0 { superscript } else { subscript };
            let written: Option<Vec<String>> = self.pieces[i..end]
                .iter()
                .map(|p| match p {
                    Piece::Char(l) => l.text.chars().map(form).collect(),
                    _ => None,
                })
                .collect();
            if let (true, Some(written)) = (attached, written) {
                for (piece, text) in self.pieces[i..end].iter_mut().zip(written) {
                    if let Piece::Char(l) = piece {
                        l.text = text;
                    }
                }
            }
            i = end;
        }
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
                Piece::Char(l) => Some(&l.bbox),
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
    ///
    /// The words stay in the order they stand on the page, which is what
    /// their boxes are measured by; their text is in the order it is read
    /// (see [`reading_order`]), the way the page's lines read (`page`,
    /// right to left where `true`) where the line alone does not say.
    fn into_line(self, gaps_are_cells: bool, page: Option<bool>) -> Option<Line> {
        // Where each piece is read: its place in reading order, and whether
        // it stands in text read right to left.
        let mut rank: Vec<(usize, bool)> = (0..self.pieces.len()).map(|i| (i, false)).collect();
        if let Some(order) = reading_order(&self.pieces, page) {
            for (place, &(piece, backwards)) in order.iter().enumerate() {
                rank[piece] = (place, backwards);
            }
        }
        // The text of the pieces `range` spans, in reading order.
        let read = |range: std::ops::Range<usize>| {
            let mut ids: Vec<usize> = range.collect();
            ids.sort_by_key(|&i| rank[i].0);
            let mut text = String::new();
            for i in ids {
                match &self.pieces[i] {
                    Piece::Char(l) if rank[i].1 && !l.actual => {
                        text.extend(l.text.chars().map(mirrored))
                    }
                    Piece::Char(l) => text.push_str(&l.text),
                    _ if !text.is_empty() && !text.ends_with(' ') => text.push(' '),
                    _ => {}
                }
            }
            text.trim_end().to_string()
        };
        // Each word's pieces, its box, and whether it opens a new cell.
        let mut spans: Vec<(std::ops::Range<usize>, Rect, bool)> = Vec::new();
        let mut current: Option<(usize, Rect, bool)> = None;
        let mut cut = false;
        for (i, piece) in self.pieces.iter().enumerate() {
            match piece {
                Piece::Char(l) => {
                    current = Some(match current {
                        Some((first, bbox, open)) => (first, bbox.union(&l.bbox), open),
                        None => {
                            let open = cut && !spans.is_empty();
                            cut = false;
                            (i, l.bbox, open)
                        }
                    });
                }
                Piece::Space | Piece::Gap | Piece::Blank(_) => {
                    if let Some((first, bbox, open)) = current.take() {
                        spans.push((first..i, bbox, open));
                    }
                    cut |= gaps_are_cells && matches!(piece, Piece::Gap);
                }
            }
        }
        if let Some((first, bbox, open)) = current.take() {
            spans.push((first..self.pieces.len(), bbox, open));
        }
        let text = read(0..self.pieces.len());
        if text.trim().is_empty() {
            return None;
        }
        let opens: Vec<bool> = spans.iter().map(|s| s.2).collect();
        let mut words: Vec<Word> = spans
            .iter()
            .map(|(range, bbox, _)| Word {
                text: read(range.clone()),
                bbox: *bbox,
                confidence: 1.0,
            })
            .collect();
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
            // Each cell: the pieces from its first word to its last.
            let mut cells: Vec<(std::ops::Range<usize>, Rect)> = Vec::new();
            for ((range, _, open), word) in spans.iter().zip(&words) {
                match cells.last_mut() {
                    Some((cell, bbox)) if !open => {
                        cell.end = range.end;
                        *bbox = bbox.union(&word.bbox);
                    }
                    _ => cells.push((range.clone(), word.bbox)),
                }
            }
            segments = cells
                .into_iter()
                .map(|(range, bbox)| Segment {
                    text: read(range),
                    bbox,
                    confidence: 1.0,
                })
                .collect();
        }
        let b = self.baseline();
        let top = self.up * ASCENT_EM;
        let bottom = self.up * -DESCENT_EM;
        // The run's ends on its baseline: a superscript at either end is not
        // where the line's box starts or stops being level.
        let n = Vec2::new(-b.y, b.x);
        let level = |p: KPoint| p - n * (p - self.base).dot(n);
        let (s, e) = (level(self.start), level(self.last_end));
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
                    let tracking = run.tracking.max(next.tracking);
                    let gap = (next.start - run.last_end).dot(run.baseline());
                    if next.opens_with_space {
                        run.space(Piece::Space);
                    } else if gap > (SPACE_GAP_EM + tracking) * size {
                        run.space(Piece::Gap);
                    }
                    run.pieces.extend(next.pieces);
                    run.last_origin = next.last_origin;
                    run.last_end = next.last_end;
                    run.after_space = next.after_space;
                    run.size = size;
                    run.tracking = tracking;
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
        // A space the document set may be stretched across half a column —
        // a justified heading of two words; the lines around it decide.
        let widest = if spaces_set && run.after_space {
            STRETCHED_SPACE_EM
        } else {
            COLUMN_GAP_AFTER_SPACE_EM
        };
        if gap > widest * size {
            continue;
        }
        let middle = (x1 + spans[b].1) / 2.0;
        let (reach_left, reach_right) = (spans[a].1, spans[b].2);
        // The nearest baseline above and below, a line's distance away, of
        // the lines under or over the two pieces: a line of the next column
        // over is no neighbour of this one, and a table's rows under a gap
        // between two cells are, although they are white just there.
        let neighbour = |from: f64, above: bool| {
            let mut best: Option<f64> = None;
            for (c, &(cy, cx0, cx1)) in spans.iter().enumerate() {
                if !upright[c] || c == a || c == b || cx1 < reach_left || cx0 > reach_right {
                    continue;
                }
                let d = if above { from - cy } else { cy - from };
                if d > 0.6 * size && d < 2.5 * size && best.is_none_or(|bd| d < bd) {
                    best = Some(d);
                }
            }
            best.map(|d| if above { from - d } else { from + d })
        };
        let covered = |line_y: f64| {
            spans.iter().enumerate().any(|(c, &(cy, x0, x1))| {
                upright[c] && (cy - line_y).abs() <= 0.35 * size && x0 <= middle && middle <= x1
            })
        };
        // A corridor is white above and below; a word gap has letters on at
        // least one side (both, as often as not — but the neighbouring line
        // may have a word gap of its own just there). White two lines deep on
        // one side is a corridor after all: the gutter of a table under its
        // lead-in line, not a word gap the next line happens to share. A line
        // on its own keeps the split its gap earned.
        let sides: Vec<(bool, bool)> = [true, false]
            .into_iter()
            .filter_map(|above| {
                let near = neighbour(y, above)?;
                let far = neighbour(near, above).is_some_and(|far| !covered(far));
                Some((covered(near), far))
            })
            .collect();
        let word_gap = sides.iter().any(|(near, _)| *near)
            && !sides.iter().any(|(near, far_white)| !near && *far_white);
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
            run.after_space = piece.after_space;
            run.size = run.size.max(piece.size);
            run.tracking = run.tracking.max(piece.tracking);
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
    if (next.base - run.base).dot(n).abs() > 0.35 * size {
        return false;
    }
    // A glyph standing on its own, far larger than the text beside it, is a
    // drop cap on the baseline of its paragraph's third line: the first
    // letter of the paragraph, not of that line. A superscript after a word
    // is the smaller run, and the word it follows is no single glyph as a
    // rule; it still joins its line.
    let (larger, smaller) = if run.size >= next.size {
        (run, next)
    } else {
        (next, run)
    };
    if larger.letters() == 1 && larger.size > DROP_CAP_FACTOR * smaller.size {
        return false;
    }
    let gap = (next.start - run.last_end).dot(b);
    // A word drawn later carries on a line a word space away; the next
    // column, a table's next cell, can be as near as an em.
    (-0.3 * size..=MERGE_GAP_EM * run.size.min(next.size)).contains(&gap)
}

/// Drops what a page draws twice in one place: fake bold, the same line
/// drawn again a hair to the right; a text shadow, a copy of the line drawn
/// a little off before the line itself — browsers print CSS `text-shadow`
/// that way, a whole line at a time; text both filled and stroked. Each is
/// one character, not two. A glyph is a copy of an earlier one with the same
/// characters and size within a tenth of an em along its baseline — or a
/// third of one where it is off that baseline too, as a shadow is: a letter
/// that repeats in a word ("ll") is a whole advance along the line and not
/// off it at all.
fn drop_twins(glyphs: Vec<&TextGlyph>) -> Vec<&TextGlyph> {
    // The glyphs kept so far, by the square of the page they start in.
    const CELL: f64 = 16.0;
    let cell = |p: KPoint| ((p.x / CELL).floor() as i64, (p.y / CELL).floor() as i64);
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut kept: Vec<&TextGlyph> = Vec::with_capacity(glyphs.len());
    for glyph in glyphs {
        let size = glyph.size();
        let b = glyph.baseline();
        let n = Vec2::new(-b.y, b.x);
        let copy = |other: &TextGlyph| {
            let v = glyph.origin - other.origin;
            let (along, off) = (v.dot(b).abs(), v.dot(n).abs());
            let reach = if off < 0.02 * size { 0.1 } else { 0.3 };
            other.text == glyph.text
                && (other.size() - size).abs() <= 0.05 * size
                && off <= 0.3 * size
                && along <= reach * size
        };
        let (cx, cy) = cell(glyph.origin);
        let reach = ((0.3 * size / CELL).ceil() as i64).min(8);
        let twin = (cx - reach..=cx + reach).any(|x| {
            (cy - reach..=cy + reach).any(|y| {
                grid.get(&(x, y))
                    .is_some_and(|ids| ids.iter().any(|&i| copy(kept[i])))
            })
        });
        if !twin {
            grid.entry((cx, cy)).or_default().push(kept.len());
            kept.push(glyph);
        }
    }
    kept
}

/// How far, in ems, a mark's middle may fall past the advance of the letter
/// it goes with.
const MARK_REACH_EM: f64 = 0.3;

/// Puts each mark with the letter it is drawn on, as one glyph: Arabic's
/// vowel signs, Hebrew's points, the vowel signs and viramas of the scripts
/// of India, Thai's tone marks — where a page draws them as glyphs of their
/// own, without saying what the cluster is (see
/// [`Collector::read_actual_text`]) — and the accents TeX draws over a
/// letter in fonts without accented letters (OT1: `¨`, a kern back, `u`).
/// Read as drawn, a mark moved up or down onto its letter starts a line of
/// its own, one drawn before its letter is read before it in text written
/// right to left, and `M¨unchen` is not `München`.
///
/// A mark goes with the letter before or after it in drawing order that it
/// stands over, [`MARK_REACH_EM`] away at most: the one after it only where
/// its middle falls across that one's advance and not the other's. Its
/// text follows the letter's,
/// composed where Unicode composes the two (NFC). A spacing accent (see
/// [`spacing_accent`]) goes only with a letter it is drawn over: beside
/// one, it is a character of its own. A vowel sign written before the
/// consonant it follows in the text (see [`pre_base`]) goes after the
/// consonant, and after the consonants joined to it by a virama.
fn cluster_marks(glyphs: Vec<&TextGlyph>) -> Vec<TextGlyph> {
    let mut out: Vec<TextGlyph> = Vec::with_capacity(glyphs.len());
    // Marks drawn before the letter they go with, and their text.
    let mut before: Vec<&TextGlyph> = Vec::new();
    let mut pending = String::new();
    let mut i = 0;
    while i < glyphs.len() {
        let glyph = glyphs[i];
        if let Some(last) = pre_base_cluster(&glyphs, i) {
            let mut cluster = glyphs[i + 1].clone();
            for &other in &glyphs[i + 2..=last] {
                absorb(&mut cluster, other, true);
                cluster.text.push_str(&other.text);
            }
            absorb(&mut cluster, glyph, true);
            cluster.text.push_str(&glyph.text);
            cluster.mapped &= glyphs[i..=last].iter().all(|g| g.mapped);
            out.push(cluster);
            i = last + 1;
            continue;
        }
        if let Some((text, spacing)) = mark_of(glyph) {
            let reach = if spacing { 0.0 } else { MARK_REACH_EM };
            let carries = |letter: &TextGlyph| {
                let mut chars = letter.text.chars();
                let fits = match (chars.next(), chars.next()) {
                    (Some(c), None) if spacing => c.is_alphabetic(),
                    _ => !spacing && !letter.text.trim().is_empty() && !combining(&letter.text),
                };
                fits.then(|| off_letter(letter, glyph))
                    .flatten()
                    .filter(|&d| d <= reach)
            };
            let on_last = out.last().and_then(carries);
            let on_next = glyphs[i + 1..]
                .iter()
                .take(4)
                .find(|g| mark_of(g).is_none())
                .and_then(|g| carries(g));
            // The letter after it where the mark stands over that one and
            // not over the one before; a mark between the two, or where
            // one ends and the other starts, follows its letter as the
            // text does.
            let next = match (on_last, on_next) {
                (Some(last), Some(next)) => next == 0.0 && last > 0.02,
                (last, next) => last.is_none() && next.is_some(),
            };
            if next {
                before.push(glyph);
                pending.push_str(&text);
                i += 1;
                continue;
            }
            if let (Some(_), Some(letter)) = (on_last, out.last_mut()) {
                absorb(letter, glyph, false);
                letter.text = compose(&letter.text, &text);
                letter.mapped &= glyph.mapped;
                i += 1;
                continue;
            }
        }
        let mut glyph = glyph.clone();
        if !pending.is_empty() && mark_of(&glyph).is_none() {
            for mark in before.drain(..) {
                absorb(&mut glyph, mark, false);
                glyph.mapped &= mark.mapped;
            }
            glyph.text = compose(&glyph.text, &pending);
            pending.clear();
        }
        out.push(glyph);
        i += 1;
    }
    // Marks whose letter never came, as they were.
    out.extend(before.into_iter().cloned());
    out
}

/// Whether `text` is nothing but combining marks: a vowel sign, a point, an
/// accent, a tone mark drawn as a glyph of its own.
fn combining(text: &str) -> bool {
    !text.is_empty() && text.chars().all(is_combining_mark)
}

/// A glyph's text as the marks it sets on a letter, if it is a mark, and
/// whether it is a spacing accent.
fn mark_of(glyph: &TextGlyph) -> Option<(String, bool)> {
    if combining(&glyph.text) {
        return Some((glyph.text.clone(), false));
    }
    let mut chars = glyph.text.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => spacing_accent(c).map(|mark| (mark.to_string(), true)),
        _ => None,
    }
}

/// The combining mark a spacing accent stands for where it is drawn over a
/// letter: TeX's fonts without accented letters (OT1) draw `ü` as `¨` set
/// over a `u`, `é` as `´` over an `e`.
fn spacing_accent(c: char) -> Option<char> {
    Some(match c {
        '`' | 'ˋ' => '\u{300}',
        '´' | 'ˊ' => '\u{301}',
        '^' | 'ˆ' => '\u{302}',
        '~' | '˜' => '\u{303}',
        '¯' | 'ˉ' => '\u{304}',
        '˘' => '\u{306}',
        '˙' => '\u{307}',
        '¨' => '\u{308}',
        '˚' => '\u{30A}',
        '˝' => '\u{30B}',
        'ˇ' => '\u{30C}',
        '¸' => '\u{327}',
        '˛' => '\u{328}',
        _ => return None,
    })
}

/// `letter` with `marks` set on it, composed where Unicode composes them
/// (NFC): `u` and a diaeresis are `ü`, a letter with no accented form keeps
/// the mark after it. A dotless `ı` under an accent above is the `i` TeX
/// draws that way.
fn compose(letter: &str, marks: &str) -> String {
    let above = marks.chars().any(|c| canonical_combining_class(c) == 230);
    let letter = match letter {
        "ı" if above => "i",
        "ȷ" if above => "j",
        other => other,
    };
    letter.chars().chain(marks.chars()).nfc().collect()
}

/// How far, in ems, the middle of `mark` is from the advance of `letter`
/// along its baseline — 0 where it falls across it — where the two stand on
/// one line.
fn off_letter(letter: &TextGlyph, mark: &TextGlyph) -> Option<f64> {
    let size = letter.size();
    let turn = (mark.angle() - letter.angle() + 540.0).rem_euclid(360.0) - 180.0;
    if turn.abs() > 3.0 || mark.size() > 2.0 * size || mark.size() < 0.4 * size {
        return None;
    }
    let b = letter.baseline();
    let n = Vec2::new(-b.y, b.x);
    let middle = mark.origin + (mark.end - mark.origin) * 0.5;
    if (middle - letter.origin).dot(n).abs() > size {
        return None;
    }
    let along = (middle - letter.origin).dot(b);
    let advance = (letter.end - letter.origin).dot(b);
    Some((-along).max(along - advance).max(0.0) / size.max(f64::EPSILON))
}

/// Widens `glyph` along its baseline over where `other`, a glyph it takes
/// in, starts, and over its advance where it is `whole` or `other` starts
/// where `glyph` ends: a spacing vowel sign after its consonant. A mark set
/// over a letter is as wide as the letter.
fn absorb(glyph: &mut TextGlyph, other: &TextGlyph, whole: bool) {
    let b = glyph.baseline();
    let from = glyph.origin;
    let advance = (glyph.end - from).dot(b);
    let start = (other.origin - from).dot(b);
    let (mut lo, mut hi) = (0.0f64.min(start), advance.max(start));
    if whole || start >= advance - 0.1 * glyph.size() {
        let end = (other.end - from).dot(b);
        lo = lo.min(end);
        hi = hi.max(end);
    }
    glyph.origin = from + b * lo;
    glyph.end = from + b * hi;
}

/// Whether `c` is a vowel sign written after its consonant and drawn before
/// it: Hindi's `ि`, its likes in Bengali, Gurmukhi and Gujarati, and the
/// `e` and `ai` signs of Bengali, Oriya, Tamil and Malayalam.
fn pre_base(c: char) -> bool {
    matches!(
        c as u32,
        0x093F
            | 0x09BF
            | 0x09C7
            | 0x09C8
            | 0x0A3F
            | 0x0ABF
            | 0x0B47
            | 0x0B48
            | 0x0BC6..=0x0BC8
            | 0x0D46..=0x0D48
    )
}

/// Where the vowel sign at `i`, drawn before its consonant (see
/// [`pre_base`]), has its cluster: the glyphs right after it, up to the
/// consonant it follows in the text — the last of those that a virama
/// joins. `None` where no consonant of its script follows it.
fn pre_base_cluster(glyphs: &[&TextGlyph], i: usize) -> Option<usize> {
    let sign = glyphs[i];
    let mut chars = sign.text.chars();
    let (Some(c), None) = (chars.next(), chars.next()) else {
        return None;
    };
    if !pre_base(c) {
        return None;
    }
    // Drawn back over the letter before it, the sign was drawn in the
    // text's order, after its consonant: a mark like any other.
    if let Some(prev) = i.checked_sub(1).map(|j| glyphs[j]) {
        let b = prev.baseline();
        let n = Vec2::new(-b.y, b.x);
        let along = (sign.origin - prev.origin).dot(b);
        let advance = (prev.end - prev.origin).dot(b);
        if (sign.origin - prev.origin).dot(n).abs() <= 0.3 * prev.size()
            && (-0.1 * prev.size()..advance - 0.1 * prev.size()).contains(&along)
        {
            return None;
        }
    }
    // Each of these scripts has its virama at the same place in its block.
    let block = c as u32 >> 7;
    let virama = char::from_u32((block << 7) | 0x4D)?;
    let mut last = i;
    while let Some(&next) = glyphs.get(last + 1) {
        let prev = glyphs[last];
        let size = prev.size().max(next.size());
        let b = prev.baseline();
        let n = Vec2::new(-b.y, b.x);
        let Some(first) = next.text.chars().next() else {
            break;
        };
        let script = next.text.chars().all(|c| c as u32 >> 7 == block);
        let letter = first.is_alphabetic() && !is_combining_mark(first);
        let joined = last > i && combining(&next.text);
        if !script
            || !(letter || joined)
            || (next.origin - prev.end).dot(b).abs() > 0.4 * size
            || (next.origin - prev.origin).dot(n).abs() > 0.3 * size
        {
            break;
        }
        last += 1;
        if !next.text.ends_with(virama) {
            break;
        }
    }
    (last > i).then_some(last)
}

/// A character as Unicode has it raised, where it does: the digits, the
/// signs and `n` that the other readers write raised too.
fn superscript(c: char) -> Option<char> {
    Some(match c {
        '0' => '⁰',
        '1' => '¹',
        '2' => '²',
        '3' => '³',
        '4' => '⁴',
        '5' => '⁵',
        '6' => '⁶',
        '7' => '⁷',
        '8' => '⁸',
        '9' => '⁹',
        '+' => '⁺',
        '-' | '−' => '⁻',
        '=' => '⁼',
        '(' => '⁽',
        ')' => '⁾',
        'n' => 'ⁿ',
        _ => return None,
    })
}

/// A character as Unicode has it lowered, where it does.
fn subscript(c: char) -> Option<char> {
    Some(match c {
        '0' => '₀',
        '1' => '₁',
        '2' => '₂',
        '3' => '₃',
        '4' => '₄',
        '5' => '₅',
        '6' => '₆',
        '7' => '₇',
        '8' => '₈',
        '9' => '₉',
        '+' => '₊',
        '-' | '−' => '₋',
        '=' => '₌',
        '(' => '₍',
        ')' => '₎',
        _ => return None,
    })
}

/// Whether `c` is a letter of a script written right to left: Hebrew,
/// Arabic, Syriac, Thaana and their neighbours, their presentation forms.
fn right_to_left(c: char) -> bool {
    matches!(c as u32,
        0x0590..=0x08FF | 0xFB1D..=0xFDFF | 0xFE70..=0xFEFF | 0x10800..=0x10FFF | 0x1E800..=0x1EFFF)
        && c.is_alphabetic()
}

/// Whether `c` is Chinese, Japanese or Korean, or their punctuation and
/// full-width forms: the scripts set in columns.
fn ideographic(c: char) -> bool {
    matches!(c as u32,
        0x1100..=0x11FF | 0x2E80..=0x2FDF | 0x3000..=0x9FFF | 0xAC00..=0xD7AF
        | 0xF900..=0xFAFF | 0xFE30..=0xFE4F | 0xFF00..=0xFFEF | 0x20000..=0x3FFFF)
}

/// How a piece of a run takes part in its direction.
#[derive(Clone, Copy, PartialEq)]
enum Direction {
    /// A letter of a script written right to left.
    Right,
    /// A letter of any other script.
    Left,
    /// A number, read left to right in either kind of text.
    Number,
    /// A number in Arabic text, which takes no unit sign in with it.
    ArabicNumber,
    /// White space and punctuation, which go with what is around them.
    Neutral,
}

/// Whether `c` is a letter of the Arabic script.
fn arabic(c: char) -> bool {
    matches!(c as u32,
        0x0600..=0x06FF | 0x0750..=0x077F | 0x08A0..=0x08FF | 0xFB50..=0xFDFF | 0xFE70..=0xFEFF)
        && c.is_alphabetic()
}

/// A bracket as the one facing the other way: text written right to left
/// draws `(` as `)` (rule L4), and is read with its brackets turned back.
fn mirrored(c: char) -> char {
    match c {
        '(' => ')',
        ')' => '(',
        '[' => ']',
        ']' => '[',
        '{' => '}',
        '}' => '{',
        '<' => '>',
        '>' => '<',
        '«' => '»',
        '»' => '«',
        '‹' => '›',
        '›' => '‹',
        other => other,
    }
}

/// How the characters of a piece take part in its line's direction.
fn direction(text: &str) -> Direction {
    if text.chars().any(right_to_left) {
        Direction::Right
    } else if text.chars().any(char::is_alphabetic) {
        Direction::Left
    } else if text.chars().any(char::is_numeric) {
        Direction::Number
    } else {
        Direction::Neutral
    }
}

/// The way a line reads where the letters at its two ends agree on it —
/// right to left where `true` — and `None` where they do not, or it has
/// no letters. A line with a single letter reads that letter's way.
fn ends(class: &[Direction]) -> Option<bool> {
    let strong = |c: &&Direction| matches!(c, Direction::Right | Direction::Left);
    match (class.iter().find(strong), class.iter().rev().find(strong)) {
        (Some(Direction::Right), Some(Direction::Right)) => Some(true),
        (Some(Direction::Left), Some(Direction::Left)) => Some(false),
        _ => None,
    }
}

/// The way a page's lines read, where they clearly do: right to left where
/// `true`. Of the lines whose letters at both ends agree on a direction,
/// at least two, and four in five, read that way; a German letter with a
/// Hebrew line in it reads left to right, an Arabic one quoting an English
/// title right to left. A bilingual page does not say.
fn page_direction(runs: &[Run]) -> Option<bool> {
    let (mut rtl, mut ltr) = (0usize, 0usize);
    for run in runs {
        let class: Vec<Direction> = run
            .pieces
            .iter()
            .map(|p| match p {
                Piece::Char(l) => direction(&l.text),
                _ => Direction::Neutral,
            })
            .collect();
        match ends(&class) {
            Some(true) => rtl += 1,
            Some(false) => ltr += 1,
            None => {}
        }
    }
    if ltr >= 2 && ltr >= 4 * rtl {
        Some(false)
    } else if rtl >= 2 && rtl >= 4 * ltr {
        Some(true)
    } else {
        None
    }
}

/// The order a run's pieces are read in, where it holds text written right
/// to left: each piece's index, and whether it stands in text read right to
/// left, where its brackets are drawn mirrored. `None` where the run holds
/// no such text and is read as it is drawn.
///
/// Hebrew and Arabic are drawn the way they stand on the page, left to
/// right, and read the other way — except for the numbers in them and any
/// words of a script written left to right, which read left to right where
/// they stand. This is a simplified form of Unicode's bidirectional
/// algorithm for one line without embeddings: the line takes its direction
/// from the letters at its ends; each piece takes a level from what it is
/// and what stands around it; and every stretch at a level or higher is
/// turned around, the highest level first, as rule L2 does. Turned around,
/// the order the page shows is the order the text is read in.
///
/// `page` is the way the page's lines read where they clearly do (see
/// [`page_direction`]), right to left where `true`.
fn reading_order(pieces: &[Piece], page: Option<bool>) -> Option<Vec<(usize, bool)>> {
    use Direction::{ArabicNumber, Left, Neutral, Number, Right};
    let text = |i: usize| match &pieces[i] {
        Piece::Char(l) => l.text.as_str(),
        _ => "",
    };
    let mut class: Vec<Direction> = (0..pieces.len()).map(|i| direction(text(i))).collect();
    let rights = class.iter().filter(|&&c| c == Right).count();
    if rights == 0 {
        return None;
    }
    // The line's direction: a paragraph starts with a letter of its own
    // direction, which stands at the right end of a line read right to left
    // and at the left end of one read left to right. Where the letters at
    // both ends agree, so does the line. Where not, the line alone is
    // ambiguous — `Kontakt: Frau` and a Hebrew name after it, or a Hebrew
    // sentence ending in a German word — and the page's other lines say
    // which way its text runs. On a page that does not say, most of the
    // line's words do — words, not letters: a web address at the end of a
    // Hebrew sentence has more letters than the sentence's words — and on
    // a tie, its leftmost letter, as a line read left to right starts at its
    // left end.
    let rtl = match (ends(&class), page) {
        (Some(rtl), _) | (None, Some(rtl)) => rtl,
        (None, None) => {
            let (mut right_words, mut left_words) = (0usize, 0usize);
            let mut word: Option<Direction> = None;
            for (i, piece) in pieces.iter().enumerate() {
                if !matches!(piece, Piece::Char(_)) {
                    match word.take() {
                        Some(Right) => right_words += 1,
                        Some(Left) => left_words += 1,
                        _ => {}
                    }
                } else if word.is_none() && matches!(class[i], Right | Left) {
                    word = Some(class[i]);
                }
            }
            match word {
                Some(Right) => right_words += 1,
                Some(Left) => left_words += 1,
                _ => {}
            }
            match right_words.cmp(&left_words) {
                std::cmp::Ordering::Equal => {
                    class.iter().find(|&&c| matches!(c, Right | Left)) == Some(&Right)
                }
                more => more.is_gt(),
            }
        }
    };
    // The nearest letter from `i` towards `step`, if any.
    let letter = |class: &[Direction], i: usize, step: isize| {
        let mut j = i as isize + step;
        while j >= 0 && (j as usize) < class.len() {
            if matches!(class[j as usize], Right | Left) {
                return Some(j as usize);
            }
            j += step;
        }
        None
    };
    // A number after an Arabic letter is an Arabic number (W2); the letter
    // before it in reading order stands to its right in a line read right
    // to left.
    let back = if rtl { 1 } else { -1 };
    for i in 0..class.len() {
        if class[i] == Number
            && letter(&class, i, back).is_some_and(|j| text(j).chars().any(arabic))
        {
            class[i] = ArabicNumber;
        }
    }
    // A separator between two numbers (`1,000`, `3.5`, `12:30`) belongs to
    // them (W4), and so does a unit sign next to a number that is not
    // Arabic (`12%`, `5°`, `€20`; W5).
    for i in 0..class.len() {
        let mut chars = text(i).chars();
        let (Some(sign), None) = (chars.next(), chars.next()) else {
            continue;
        };
        if class[i] != Neutral {
            continue;
        }
        let before = i.checked_sub(1).map(|j| class[j]);
        let after = class.get(i + 1).copied();
        let numbers = matches!(before, Some(Number | ArabicNumber)) && before == after;
        if ",.:/·".contains(sign) && numbers
            || "+-−".contains(sign) && numbers && before == Some(Number)
        {
            class[i] = before.unwrap_or(Neutral);
        } else if "%‰°$€£¥#".contains(sign) && (before == Some(Number) || after == Some(Number))
        {
            class[i] = Number;
        }
    }
    // The nearest piece that is not neutral, from `i` towards `step`; the
    // line's own direction past its end. Numbers count as right to left
    // for what stands between them (N1).
    let paragraph = if rtl { Right } else { Left };
    let beside = |i: usize, step: isize| {
        let mut j = i as isize + step;
        while j >= 0 && (j as usize) < class.len() {
            match class[j as usize] {
                Neutral => j += step,
                Left => return Left,
                _ => return Right,
            }
        }
        paragraph
    };
    let left_level = if rtl { 2 } else { 0 };
    let levels: Vec<u8> = (0..class.len())
        .map(|i| match class[i] {
            Right => 1,
            Left => left_level,
            Number | ArabicNumber if rtl => 2,
            // A number inside words written right to left reads left to
            // right inside them; one among the line's own words is one of
            // them.
            Number | ArabicNumber => {
                let side = |step| letter(&class, i, step).map(|j| class[j]);
                match (side(-1), side(1)) {
                    (Some(Left), _) | (_, Some(Left)) => 0,
                    (None, None) => 0,
                    _ => 2,
                }
            }
            Neutral => match (beside(i, -1), beside(i, 1)) {
                (Right, Right) => 1,
                (Left, Left) => left_level,
                _ => u8::from(rtl),
            },
        })
        .collect();
    let mut order: Vec<usize> = (0..pieces.len()).collect();
    let top = levels.iter().copied().max().unwrap_or(0);
    for level in (1..=top).rev() {
        let mut i = 0;
        while i < order.len() {
            if levels[order[i]] < level {
                i += 1;
                continue;
            }
            let mut end = i;
            while end < order.len() && levels[order[end]] >= level {
                end += 1;
            }
            order[i..end].reverse();
            i = end;
        }
    }
    Some(order.into_iter().map(|i| (i, levels[i] % 2 == 1)).collect())
}

/// The device that draws nothing and writes down every glyph.
struct Collector {
    layer: TextLayer,
    /// What each font's private-use characters stand for, by font.
    fonts: HashMap<u128, SymbolFont>,
    /// Of fonts whose name is not known: how many glyphs each drew, and which
    /// of them were private-use characters left as they are.
    unnamed: HashMap<u128, (usize, Vec<(usize, char)>)>,
    /// What the page's fonts do not say with each glyph they draw.
    facts: FontFacts,
    /// The tag of every marked-content sequence begun, in the order the
    /// interpreter began them (see [`marked_content`]).
    marked: Vec<Vec<u8>>,
    /// The sequences open, the innermost last, by their place in `marked`.
    open: Vec<usize>,
    /// Of each glyph drawn, in order: the sequences open around it, the
    /// outermost first, and whether its font gave it an advance.
    drawn: Vec<(Vec<usize>, bool)>,
}

/// Rules a page keeps at most: a map or a chart draws thousands of strokes,
/// and a table needs a few dozen.
const MAX_RULES: usize = 20_000;

impl Collector {
    fn push_rule(&mut self, rule: Rect) {
        if self.layer.rules.len() < MAX_RULES {
            self.layer.rules.push(rule);
        }
    }

    /// A font whose name is not known and that drew nothing but private-use
    /// characters from Wingdings' bullets, each at the start of its line — a
    /// bullet font, set a glyph at a time before each item: those glyphs are
    /// bullets. Symbol's θ in a sentence has text before it.
    fn settle_unnamed_fonts(&mut self) {
        let glyphs = &self.layer.glyphs;
        let opens_a_line = |index: usize| {
            let glyph = &glyphs[index];
            let size = glyph.size().max(1.0);
            let b = glyph.baseline();
            let n = Vec2::new(-b.y, b.x);
            !glyphs.iter().enumerate().any(|(other, g)| {
                let rel = g.end - glyph.origin;
                other != index
                    && !g.text.trim().is_empty()
                    && rel.dot(n).abs() < 0.3 * size
                    && (-0.6 * size..=0.1 * size).contains(&rel.dot(b))
            })
        };
        let mut bullets_at: Vec<usize> = Vec::new();
        for (total, private) in self.unnamed.values() {
            let bullets = private.iter().all(|&(index, c)| {
                private_use(c, SymbolFont::Dingbats) == Some('•') && opens_a_line(index)
            });
            if *total == 0 || private.len() != *total || !bullets {
                continue;
            }
            bullets_at.extend(private.iter().map(|&(index, _)| index));
        }
        for index in bullets_at {
            let glyph = &mut self.layer.glyphs[index];
            glyph.text = "•".into();
            glyph.mapped = true;
        }
    }

    /// Puts the text a marked-content sequence says it is (`/ActualText`)
    /// in place of the glyphs drawn in it. A browser shapes Arabic, Hindi
    /// and Thai into glyphs that are not one letter each — a conjunct, a
    /// letter and its vowel signs moved up or down onto it, a vowel sign
    /// drawn before the consonant it follows in the text — and says with
    /// each such cluster what text it is. Read glyph by glyph, letters are
    /// lost, vowel signs stand before their consonants, and marks start
    /// lines of their own. The outermost sequence with a text of its own
    /// decides; one that says nothing (an empty text) leaves its glyphs be.
    ///
    /// `sequences` are the page's sequences as [`marked_content`] reads
    /// them, which the interpreter began in the same order with the same
    /// tags: where the two do not agree — a form the interpreter left
    /// hidden — no text can be told from another's, and none is used.
    fn read_actual_text(&mut self, sequences: &[Marked]) {
        let agree = sequences.len() == self.marked.len()
            && sequences
                .iter()
                .zip(&self.marked)
                .all(|(s, tag)| s.tag == *tag);
        if !agree {
            return;
        }
        let said = |k: &usize| sequences[*k].text.as_deref().is_some_and(|t| !t.is_empty());
        let spans: Vec<Option<usize>> = self
            .drawn
            .iter()
            .map(|(open, _)| open.iter().copied().find(said))
            .collect();
        if spans.iter().all(Option::is_none) {
            return;
        }
        let glyphs = std::mem::take(&mut self.layer.glyphs);
        let mut out = Vec::with_capacity(glyphs.len());
        let mut i = 0;
        while i < glyphs.len() {
            let Some(k) = spans.get(i).copied().flatten() else {
                out.push(glyphs[i].clone());
                i += 1;
                continue;
            };
            let mut end = i + 1;
            while spans.get(end).copied().flatten() == Some(k) {
                end += 1;
            }
            let advanced: Vec<bool> = self.drawn[i..end].iter().map(|d| d.1).collect();
            let text = sequences[k].text.as_deref().unwrap_or_default();
            match stand_in(&glyphs[i..end], &advanced, text) {
                Some(glyph) => out.push(glyph),
                None => out.extend_from_slice(&glyphs[i..end]),
            }
            i = end;
        }
        self.layer.glyphs = out;
    }
}

/// The glyph that stands for `glyphs`, drawn in one marked-content sequence
/// that says it is `text` (see [`Collector::read_actual_text`]): on the
/// baseline of the widest of them that is no mark — marks are moved up and
/// down onto their letter — and across all of them. A glyph its font gave
/// no advance (`advanced`) covers only where it starts. `None` where they
/// are no piece of one line: a sequence around a word broken over two
/// lines says what the word is, and neither line holds it all. And `None`
/// where the glyphs say `text` already, none of them a mark: they keep
/// their places. A mark is kept with its letter here, as it is known to
/// go with it.
fn stand_in(glyphs: &[TextGlyph], advanced: &[bool], text: &str) -> Option<TextGlyph> {
    if glyphs.iter().all(|g| mark_of(g).is_none())
        && glyphs.iter().map(|g| g.text.as_str()).collect::<String>() == text
    {
        return None;
    }
    let width = |i: usize| {
        let g = &glyphs[i];
        if advanced[i] {
            (g.end - g.origin).hypot()
        } else {
            0.0
        }
    };
    let wider = |&a: &usize, &b: &usize| width(a).total_cmp(&width(b));
    let widest = (0..glyphs.len())
        .filter(|&i| !combining(&glyphs[i].text))
        .max_by(wider)
        .or_else(|| (0..glyphs.len()).max_by(wider))?;
    let base = &glyphs[widest];
    let size = base.size();
    let b = base.baseline();
    let n = Vec2::new(-b.y, b.x);
    let (mut lo, mut hi) = (0.0f64, width(widest));
    for (g, &advanced) in glyphs.iter().zip(advanced) {
        let turn = (g.angle() - base.angle() + 540.0).rem_euclid(360.0) - 180.0;
        if turn.abs() > 3.0 || (g.origin - base.origin).dot(n).abs() > size {
            return None;
        }
        let from = (g.origin - base.origin).dot(b);
        let to = if advanced {
            (g.end - base.origin).dot(b)
        } else {
            from
        };
        lo = lo.min(from.min(to));
        hi = hi.max(from.max(to));
    }
    if hi - lo > (glyphs.len() as f64 + 1.0) * size {
        return None;
    }
    // Nothing but marks, none of them wide: as wide as a glyph of unknown
    // width is taken to be.
    if hi - lo < 0.05 * size {
        hi = lo + 0.5 * size;
    }
    let text = arabic_letters(unligature(text.to_string()));
    Some(TextGlyph {
        mapped: text.chars().all(readable),
        visible: glyphs.iter().any(|g| g.visible),
        origin: base.origin + b * lo,
        end: base.origin + b * hi,
        up: base.up,
        text,
        actual: true,
    })
}

/// A marked-content sequence as a content stream begins it: its tag, and
/// the text it says it is (`/ActualText`), where it says.
struct Marked {
    tag: Vec<u8>,
    text: Option<String>,
}

/// How deeply forms drawn in forms are followed, as the interpreter does.
const MAX_FORM_DEPTH: u32 = 50;

/// The marked-content sequences of `page`, in the order the interpreter
/// begins them: those of its content, of each form it draws where and as
/// often as it draws it, and of its annotations' appearances. hayro tells a
/// device where a sequence begins and ends and what its tag is, but not
/// what its properties say; the streams are read again for that. `None`
/// once there are more than `limit`: the two readings do not agree.
fn marked_content(page: &Page<'_>, limit: usize) -> Option<Vec<Marked>> {
    let mut out = Vec::new();
    let resources = page.resources();
    marked_in(page.typed_operations(), resources, 0, limit, &mut out)?;
    for annot in page
        .raw()
        .get::<Array<'_>>(b"Annots")
        .iter()
        .flat_map(|annots| annots.iter::<Dict<'_>>())
    {
        // As the interpreter draws them: shown, placed, a single look.
        let hidden = annot.get::<u32>(b"F").unwrap_or(0) & 2 != 0;
        let look = annot
            .get::<Dict<'_>>(b"AP")
            .and_then(|ap| ap.get::<Stream<'_>>(b"N"));
        if let (false, Some(_), Some(look)) = (hidden, annot.get::<[f64; 4]>(b"Rect"), look) {
            marked_in_form(&look, resources, 1, limit, &mut out)?;
        }
    }
    Some(out)
}

/// Adds the marked-content sequences `ops` begins to `out` (see
/// [`marked_content`]).
fn marked_in<'a>(
    mut ops: TypedIter<'_>,
    resources: &Resources<'a>,
    depth: u32,
    limit: usize,
    out: &mut Vec<Marked>,
) -> Option<()> {
    while let Some(op) = ops.next() {
        match op {
            TypedInstruction::BeginMarkedContent(bmc) => out.push(Marked {
                tag: bmc.0.as_ref().to_vec(),
                text: None,
            }),
            TypedInstruction::BeginMarkedContentWithProperties(bdc) => {
                let properties = match bdc.1 {
                    Object::Dict(dict) => Some(dict.clone()),
                    Object::Name(name) => resources.properties.get::<Dict<'_>>(name.as_ref()),
                    _ => None,
                };
                let text = properties
                    .and_then(|p| p.get::<PdfString<'_>>(b"ActualText"))
                    .map(|s| text_string(s.as_bytes()));
                out.push(Marked {
                    tag: bdc.0.as_ref().to_vec(),
                    text,
                });
            }
            TypedInstruction::XObject(x) => {
                let form = resources.get_x_object(x.0).filter(|s| {
                    s.dict()
                        .get::<Name<'_>>(b"Subtype")
                        .is_some_and(|t| t.as_str() == "Form")
                });
                if let Some(form) = form {
                    marked_in_form(&form, resources, depth + 1, limit, out)?;
                }
            }
            _ => {}
        }
        if out.len() > limit {
            return None;
        }
    }
    Some(())
}

/// Adds the marked-content sequences of the form `form` to `out`, where the
/// interpreter draws it: it has a box, and is not nested too deeply.
fn marked_in_form<'a>(
    form: &Stream<'a>,
    resources: &Resources<'a>,
    depth: u32,
    limit: usize,
    out: &mut Vec<Marked>,
) -> Option<()> {
    let dict = form.dict();
    if depth > MAX_FORM_DEPTH || dict.get::<[f32; 4]>(b"BBox").is_none() {
        return Some(());
    }
    let Ok(content) = form.decoded() else {
        return Some(());
    };
    let own = dict.get::<Dict<'_>>(b"Resources").unwrap_or_default();
    let resources = Resources::from_parent(own, resources.clone());
    marked_in(TypedIter::new(&content), &resources, depth, limit, out)
}

/// A PDF text string as text: UTF-16 after its byte order mark, UTF-8
/// after its own (PDF 2.0), PDFDocEncoding without one.
fn text_string(bytes: &[u8]) -> String {
    let utf16 = |rest: &[u8], big: bool| -> String {
        let units = rest.chunks_exact(2).map(|p| {
            if big {
                u16::from_be_bytes([p[0], p[1]])
            } else {
                u16::from_le_bytes([p[0], p[1]])
            }
        });
        char::decode_utf16(units)
            .map(|c| c.unwrap_or('\u{FFFD}'))
            .collect()
    };
    match bytes {
        [0xFE, 0xFF, rest @ ..] => utf16(rest, true),
        [0xFF, 0xFE, rest @ ..] => utf16(rest, false),
        [0xEF, 0xBB, 0xBF, rest @ ..] => String::from_utf8_lossy(rest).into_owned(),
        _ => bytes.iter().map(|&b| pdf_doc_char(b)).collect(),
    }
}

/// A byte of PDFDocEncoding: Latin-1, but for the accents it has below the
/// space and the punctuation and letters it has from 0x80 on.
fn pdf_doc_char(byte: u8) -> char {
    const ACCENTS: &str = "˘ˇˆ˙˝˛˚˜";
    const HIGH: &str = "•†‡…—–ƒ⁄‹›−‰„“”‘’‚™ﬁﬂŁŒŠŸŽıłœšž\u{FFFD}€";
    match byte {
        0x18..=0x1F => ACCENTS.chars().nth(usize::from(byte - 0x18)),
        0x80..=0xA0 => HIGH.chars().nth(usize::from(byte - 0x80)),
        _ => Some(char::from(byte)),
    }
    .unwrap_or('\u{FFFD}')
}

impl<'a> Device<'a> for Collector {
    fn set_soft_mask(&mut self, _: Option<SoftMask<'a>>) {}
    fn set_blend_mode(&mut self, _: BlendMode) {}
    fn draw_path(&mut self, path: &BezPath, transform: Affine, _: &Paint<'a>, mode: &PathDrawMode) {
        let bounds = transform.transform_rect_bbox(kurbo::Shape::bounding_box(path));
        let (w, h) = (bounds.width(), bounds.height());
        let rect = |b: kurbo::Rect| Rect::new(b.x0 as f32, b.y0 as f32, b.x1 as f32, b.y1 as f32);
        // Small, roughly square: the size of a bullet.
        let bullet =
            w >= 1.0 && h >= 1.0 && w <= 60.0 && h <= 60.0 && (0.6..=1.6).contains(&(w / h));
        if !matches!(mode, PathDrawMode::Fill(_)) {
            // A small closed curve, stroked: the hollow bullet of a nested
            // list, which browsers draw as the outline of a circle. (A small
            // square drawn so is a tick box, not a bullet.)
            let elements = path.elements();
            let round = elements
                .iter()
                .any(|e| matches!(e, PathEl::CurveTo(..) | PathEl::QuadTo(..)));
            if bullet && round && elements.iter().any(|e| matches!(e, PathEl::ClosePath)) {
                self.layer.marks.push(rect(bounds));
                return;
            }
            // A stroke: each of its straight pieces that runs across or down
            // the page is a rule — a border drawn line by line, or a cell's
            // box drawn whole.
            let mut start = None;
            let mut at = None;
            for element in elements {
                let (from, to) = match *element {
                    PathEl::MoveTo(p) => {
                        (start, at) = (Some(p), Some(p));
                        continue;
                    }
                    PathEl::LineTo(p) => (at.replace(p), p),
                    PathEl::ClosePath => match start {
                        Some(p) => (at.replace(p), p),
                        None => continue,
                    },
                    PathEl::QuadTo(_, p) | PathEl::CurveTo(_, _, p) => {
                        at = Some(p);
                        continue;
                    }
                };
                let Some(from) = from else { continue };
                let (a, b) = (transform * from, transform * to);
                if (a.x - b.x).abs() < 1.0 || (a.y - b.y).abs() < 1.0 {
                    self.push_rule(rect(kurbo::Rect::from_points(a, b)));
                }
            }
            return;
        }
        if bullet {
            self.layer.marks.push(rect(bounds));
        } else if w.min(h) <= 6.0 && w.max(h) >= 4.0 * w.min(h).max(1.0) {
            // A thin bar: a rule, the way browsers draw a table's borders.
            self.push_rule(rect(bounds));
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
        let unicode = glyph.as_unicode();
        let width = match glyph {
            Glyph::Outline(g) => g.advance_width().map(f64::from),
            Glyph::Type3(_) => unicode
                .as_ref()
                .and_then(|u| self.facts.type3_advance(&bf_text(u))),
        };
        let advanced = width.is_some_and(|w| w.is_finite() && w > 0.0);
        let (font, key) = match glyph {
            Glyph::Outline(g) => {
                let key = g.font_cache_key();
                let font = *self.fonts.entry(key).or_insert_with(|| {
                    symbol_font(g.font_data().and_then(|d| d.postscript_name).as_deref())
                });
                (font, Some(key))
            }
            Glyph::Type3(_) => (SymbolFont::Other, None),
        };
        // A ZapfDingbats glyph has no Unicode; the code it was drawn for
        // says which mark it is.
        let dingbat = match glyph {
            Glyph::Outline(g) if unicode.is_none() => self
                .facts
                .dingbats
                .get(&g.font_cache_key())
                .and_then(|codes| codes.get(&u32::from(g.glyph_id())))
                .and_then(|&code| zapf_dingbat(code)),
            _ => None,
        };
        let (text, mapped) = match (dingbat, unicode) {
            (Some(c), _) => (c.to_string(), true),
            (None, Some(BfString::Char(c))) => {
                let c = private_use(c, font).unwrap_or(c);
                (arabic_letters(unligature(c.to_string())), readable(c))
            }
            (None, Some(BfString::String(s))) => {
                let s: String = s
                    .chars()
                    .map(|c| private_use(c, font).unwrap_or(c))
                    .collect();
                let ok = !s.is_empty() && s.chars().all(readable);
                (arabic_letters(unligature(s)), ok)
            }
            (None, None) => ("\u{FFFD}".to_string(), false),
        };
        // A glyph of unknown width is taken to be half an em wide; a mark
        // whose font gives it no width has none: it is set over its letter.
        let advance = match width {
            Some(w) if advanced => w,
            _ if combining(&text) => 0.0,
            _ => 0.5 * UNITS_PER_EM,
        };
        let end = full * KPoint::new(advance, 0.0);
        // A mark is where it is inked, on its baseline: where the pen stood
        // says little, as a font draws a mark left or right of it onto the
        // letter it goes with, and the advance it gives the mark is one the
        // shaper took back. (Not a vowel sign drawn before its consonant: its
        // stroke reaches over the consonant, and it goes by its pen.)
        let (origin, end) = match glyph {
            Glyph::Outline(g) if combining(&text) && !text.chars().any(pre_base) => {
                let ink = kurbo::Shape::bounding_box(&g.outline());
                if ink.width() > 0.0 && ink.x0.is_finite() && ink.x1.is_finite() {
                    (
                        full * KPoint::new(ink.x0, 0.0),
                        full * KPoint::new(ink.x1, 0.0),
                    )
                } else {
                    (origin, end)
                }
            }
            _ => (origin, end),
        };
        if let (SymbolFont::Other, Some(key)) = (font, key) {
            let entry = self.unnamed.entry(key).or_default();
            entry.0 += 1;
            let mut chars = text.chars();
            if let (Some(c), None) = (chars.next(), chars.next()) {
                if (0xF020..=0xF0FF).contains(&(c as u32)) {
                    entry.1.push((self.layer.glyphs.len(), c));
                }
            }
        }
        self.drawn.push((self.open.clone(), advanced));
        self.layer.glyphs.push(TextGlyph {
            text,
            origin,
            end,
            up,
            visible: !matches!(draw_mode, GlyphDrawMode::Invisible),
            mapped,
            actual: false,
        });
    }

    fn begin_marked_content(&mut self, tag: &[u8], _: Option<i32>) {
        self.open.push(self.marked.len());
        self.marked.push(tag.to_vec());
    }

    fn end_marked_content(&mut self) {
        self.open.pop();
    }

    fn draw_image(&mut self, _: Image<'a, '_>, transform: Affine) {
        // An image is the unit square under its transform.
        let area = transform.determinant().abs();
        if area.is_finite() {
            self.layer.image_area += area;
        }
    }
}

/// The characters a glyph maps to, as one string.
fn bf_text(unicode: &BfString) -> String {
    match unicode {
        BfString::Char(c) => c.to_string(),
        BfString::String(s) => s.clone(),
    }
}

/// What a page's fonts do not say with each glyph hayro hands over.
#[derive(Default)]
struct FontFacts {
    /// The advances of the page's Type3 glyphs, in glyph-space units, by
    /// the characters each maps to. hayro says what a Type3 glyph is, not
    /// how wide: the font's `Widths` do.
    type3: HashMap<String, Vec<f64>>,
    /// Of each ZapfDingbats font, by its cache key: the code each of its
    /// glyphs is drawn for, by glyph id (see [`dingbat_codes`]).
    dingbats: HashMap<u128, HashMap<u32, u8>>,
}

impl FontFacts {
    /// How far a Type3 glyph mapping to `text` advances. Where fonts of the
    /// page disagree — a bold and a regular face — the average is within a
    /// few hundredths of an em of either.
    fn type3_advance(&self, text: &str) -> Option<f64> {
        let widths = self.type3.get(text)?;
        Some(widths.iter().sum::<f64>() / widths.len().max(1) as f64)
    }
}

/// Reads [`FontFacts`] from the fonts `page` draws with: those of its
/// resources, of the forms it draws, and of its annotations' appearances.
fn font_facts<'a>(page: &Page<'a>, cache: &InterpreterCache<'a>) -> FontFacts {
    let mut facts = FontFacts::default();
    let mut seen: HashSet<u128> = HashSet::new();
    let mut stack: Vec<(Resources<'a>, u8)> = vec![(page.resources().clone(), 0)];
    for annot in page
        .raw()
        .get::<Array<'_>>(b"Annots")
        .iter()
        .flat_map(|annots| annots.iter::<Dict<'_>>())
    {
        let Some(normal) = annot.get::<Dict<'_>>(b"AP").and_then(|ap| {
            ap.get::<Stream<'_>>(b"N").map(|s| vec![s]).or_else(|| {
                let states = ap.get::<Dict<'_>>(b"N")?;
                Some(
                    states
                        .keys()
                        .filter_map(|k| states.get::<Stream<'_>>(k.as_ref()))
                        .collect(),
                )
            })
        }) else {
            continue;
        };
        for look in normal {
            if let Some(own) = look.dict().get::<Dict<'_>>(b"Resources") {
                stack.push((Resources::from_parent(own, page.resources().clone()), 1));
            }
        }
    }
    while let Some((resources, depth)) = stack.pop() {
        for name in resources.fonts.keys() {
            let Some(font) = resources.fonts.get::<Dict<'_>>(name.as_ref()) else {
                continue;
            };
            if !seen.insert(font.cache_key()) {
                continue;
            }
            let subtype = font.get::<Name<'_>>(b"Subtype");
            if subtype.as_ref().is_some_and(|s| s.as_str() == "Type3") {
                type3_widths(&font, &mut facts.type3);
            } else if dingbats(&font) {
                let codes = dingbat_codes(page, cache, &resources, name.as_str());
                facts.dingbats.insert(font.cache_key(), codes);
            }
        }
        if depth >= 4 {
            continue;
        }
        for name in resources.x_objects.keys() {
            let Some(form) = resources.x_objects.get::<Stream<'_>>(name.as_ref()) else {
                continue;
            };
            if !seen.insert(form.cache_key()) {
                continue;
            }
            if let Some(own) = form.dict().get::<Dict<'_>>(b"Resources") {
                stack.push((Resources::from_parent(own, resources.clone()), depth + 1));
            }
        }
    }
    facts
}

/// Adds the advance of each glyph of the Type3 `font` to `into`, by the
/// characters its `ToUnicode` map gives it: its width times the font's
/// matrix, as hayro places the glyph after it.
fn type3_widths(font: &Dict<'_>, into: &mut HashMap<String, Vec<f64>>) {
    let Some(data) = font
        .get::<Stream<'_>>(b"ToUnicode")
        .and_then(|s| s.decoded().ok())
    else {
        return;
    };
    let Some(map) = CMap::parse(&data, |_| None) else {
        return;
    };
    let first = font.get::<u32>(b"FirstChar").unwrap_or(0);
    let scale = font.get::<[f64; 6]>(b"FontMatrix").map_or(0.001, |m| m[0]);
    let widths = font.get::<Vec<f64>>(b"Widths").unwrap_or_default();
    for (code, width) in (first..=255).zip(widths) {
        let advance = width * scale * UNITS_PER_EM;
        let Some(text) = map.lookup_bf_string(code) else {
            continue;
        };
        if advance.is_finite() && advance > 0.0 {
            let known = into.entry(bf_text(&text)).or_default();
            if !known.contains(&advance) {
                known.push(advance);
            }
        }
    }
}

/// Whether `font` is ZapfDingbats — the standard font or one embedded under
/// its name — without a `ToUnicode` map to say what its glyphs are.
fn dingbats(font: &Dict<'_>) -> bool {
    let name = font
        .get::<Name<'_>>(b"BaseFont")
        .map(|n| n.as_str().to_ascii_lowercase())
        .unwrap_or_default();
    (name.contains("zapfdingbats") || name.ends_with("dingbats"))
        && !font.contains_key(b"ToUnicode")
}

/// The code each glyph of the ZapfDingbats font `name` in `resources` is
/// drawn for, by glyph id. hayro draws the font's glyphs by the names its
/// encoding gives each code — from a font of its own where the page embeds
/// none — and says neither the code a glyph was drawn for nor a Unicode for
/// the name. Every code, drawn once with the page's own font, says which
/// glyph is which.
fn dingbat_codes<'a>(
    page: &Page<'a>,
    cache: &InterpreterCache<'a>,
    resources: &Resources<'a>,
    name: &str,
) -> HashMap<u32, u8> {
    /// A device that notes the glyph id of every glyph drawn.
    struct GlyphIds(Vec<u32>);
    impl<'a> Device<'a> for GlyphIds {
        fn set_soft_mask(&mut self, _: Option<SoftMask<'a>>) {}
        fn set_blend_mode(&mut self, _: BlendMode) {}
        fn draw_path(&mut self, _: &BezPath, _: Affine, _: &Paint<'a>, _: &PathDrawMode) {}
        fn push_clip_path(&mut self, _: &ClipPath) {}
        fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}
        fn draw_glyph(
            &mut self,
            glyph: &Glyph<'a>,
            _: Affine,
            _: Affine,
            _: &Paint<'a>,
            _: &GlyphDrawMode,
        ) {
            self.0.push(match glyph {
                Glyph::Outline(g) => u32::from(g.glyph_id()),
                Glyph::Type3(_) => 0,
            });
        }
        fn draw_image(&mut self, _: Image<'a, '_>, _: Affine) {}
        fn pop_clip_path(&mut self) {}
        fn pop_transparency_group(&mut self) {}
    }
    // The resource's name as a content stream writes it.
    let token: String = name
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-_.".contains(&b) {
                char::from(b).to_string()
            } else {
                format!("#{b:02X}")
            }
        })
        .collect();
    let codes: Vec<u8> = (0x21..=0xFE).collect();
    let hex: String = codes.iter().map(|c| format!("{c:02X}")).collect();
    let content = format!("BT /{token} 10 Tf <{hex}> Tj ET");
    let mut context = Context::new(
        Affine::IDENTITY,
        kurbo::Rect::new(0.0, 0.0, 1.0, 1.0),
        cache,
        page.xref(),
        InterpreterSettings::default(),
    );
    let mut ids = GlyphIds(Vec::new());
    hayro::hayro_interpret::interpret(
        TypedIter::new(content.as_bytes()),
        resources,
        &mut context,
        &mut ids,
    );
    if ids.0.len() != codes.len() {
        return HashMap::new();
    }
    ids.0
        .into_iter()
        .zip(codes)
        .filter(|&(id, _)| id != 0)
        .collect()
}

/// A mark of the ZapfDingbats font by its code, as Adobe's glyph list for
/// the font has it: Unicode's Dingbats block follows the font's own order,
/// and where Unicode had a mark already — the telephone, the pointing hands,
/// the star, the black shapes, the card suits, the circled numbers, the
/// arrows — the font's glyph is that one.
fn zapf_dingbat(code: u8) -> Option<char> {
    let point = match code {
        0x20 => 0x20,
        0x25 => 0x260E,
        0x2A => 0x261B,
        0x2B => 0x261E,
        0x48 => 0x2605,
        0x6C => 0x25CF,
        0x6E => 0x25A0,
        0x73 => 0x25B2,
        0x74 => 0x25BC,
        0x75 => 0x25C6,
        0x77 => 0x25D7,
        0x21..=0x7E => 0x2700 + u32::from(code - 0x20),
        0x80..=0x8D => 0x2768 + u32::from(code - 0x80),
        0xA8 => 0x2663,
        0xA9 => 0x2666,
        0xAA => 0x2665,
        0xAB => 0x2660,
        0xAC..=0xB5 => 0x2460 + u32::from(code - 0xAC),
        0xD5 => 0x2192,
        0xD6 => 0x2194,
        0xD7 => 0x2195,
        0xA1..=0xEF | 0xF1..=0xFE => 0x2700 + u32::from(code - 0x40),
        _ => return None,
    };
    char::from_u32(point)
}

/// The check boxes and radio buttons of a filled-in form, as `☒` where one
/// is ticked and `☐` where it is not, the way the other readers write them.
/// A box keeps a look for each of its states, chosen by the state it is in
/// (`/AS`), and hayro draws only a look that is a single stream: a ticked
/// box would read as nothing at all, and the form's answers would be lost.
fn form_boxes(page: &Page<'_>, initial: Affine) -> Vec<TextGlyph> {
    let Some(annots) = page.raw().get::<Array<'_>>(b"Annots") else {
        return Vec::new();
    };
    let mut boxes = Vec::new();
    for annot in annots.iter::<Dict<'_>>() {
        let widget = annot
            .get::<Name<'_>>(b"Subtype")
            .is_some_and(|s| s.as_str() == "Widget");
        // Hidden, or not to be shown on screen.
        let hidden = annot.get::<u32>(b"F").unwrap_or(0) & (2 | 32) != 0;
        let Some(ap) = annot.get::<Dict<'_>>(b"AP") else {
            continue;
        };
        if !widget || hidden || ap.get::<Stream<'_>>(b"N").is_some() {
            continue;
        }
        let Some(states) = ap.get::<Dict<'_>>(b"N") else {
            continue;
        };
        let button =
            inherited(&annot, |d| d.get::<Name<'_>>(b"FT")).is_some_and(|t| t.as_str() == "Btn");
        // A push button has no state to read.
        let push = inherited(&annot, |d| d.get::<u32>(b"Ff")).unwrap_or(0) & (1 << 16) != 0;
        let Some([ax, ay, bx, by]) = annot.get::<[f64; 4]>(b"Rect") else {
            continue;
        };
        if !button || push {
            continue;
        }
        let ticked = match annot.get::<Name<'_>>(b"AS") {
            Some(state) => state.as_str() != "Off",
            None => inherited(&annot, |d| d.get::<Name<'_>>(b"V"))
                .is_some_and(|v| v.as_str() != "Off" && states.contains_key(v.as_ref())),
        };
        let (x0, x1) = (ax.min(bx), ax.max(bx));
        let (y0, y1) = (ay.min(by), ay.max(by));
        // An em four fifths of the box tall, on a baseline a fifth of the way
        // up it: where a glyph of the text beside it would sit.
        let baseline = y0 + 0.2 * (y1 - y0);
        let origin = initial * KPoint::new(x0, baseline);
        boxes.push(TextGlyph {
            text: if ticked { "☒" } else { "☐" }.to_string(),
            origin,
            end: initial * KPoint::new(x1, baseline),
            up: initial * KPoint::new(x0, y1) - origin,
            visible: true,
            mapped: true,
            actual: false,
        });
    }
    boxes
}

/// What `get` reads from a form field, or from the nearest of the fields it
/// belongs to: a radio button's type and value are its group's.
fn inherited<'a, T>(field: &Dict<'a>, get: impl Fn(&Dict<'a>) -> Option<T>) -> Option<T> {
    let mut node = field.clone();
    for _ in 0..16 {
        if let Some(value) = get(&node) {
            return Some(value);
        }
        node = node.get::<Dict<'_>>(b"Parent")?;
    }
    None
}

/// A ligature glyph as the letters it joins: "Pﬂicht" is searched for as
/// "Pflicht", and a knowledge base's index does not know the two are one word.
fn unligature(text: String) -> String {
    if !text.chars().any(|c| ('\u{FB00}'..='\u{FB06}').contains(&c)) {
        return text;
    }
    text.chars()
        .map(|c| match c {
            '\u{FB00}' => "ff".to_string(),
            '\u{FB01}' => "fi".to_string(),
            '\u{FB02}' => "fl".to_string(),
            '\u{FB03}' => "ffi".to_string(),
            '\u{FB04}' => "ffl".to_string(),
            '\u{FB05}' | '\u{FB06}' => "st".to_string(),
            other => other.to_string(),
        })
        .collect()
}

/// Arabic as its letters. A font shapes each letter for its place in the
/// word, and a text layer that names the shapes — Unicode's presentation
/// forms, U+FB50 to U+FDFF and U+FE70 to U+FEFF, which Chrome writes for
/// Arabic it prints as Type3 glyphs — holds words nobody types or searches
/// for. Each shape is the letter it is, as compatibility normalization
/// (NFKC) makes it: `ﺗﻘﺮﻳﺮ` is `تقرير`, `ﻻ` is `لا`. A vowel sign set on
/// its own is the sign. Of the ligatures of Forms-A only `ﷲ` is taken
/// apart; the others hardly ever come out of a font's shaping.
fn arabic_letters(text: String) -> String {
    let forms = |c: char| matches!(c as u32, 0xFB50..=0xFDFF | 0xFE70..=0xFEFF);
    if !text.chars().any(forms) {
        return text;
    }
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        let code = c as u32;
        match code {
            0xFE70..=0xFE7F if code != 0xFE73 && code != 0xFE75 => {
                out.push(char::from_u32(0x064B + (code - 0xFE70) / 2).unwrap_or(c));
            }
            0xFEF5..=0xFEFC => {
                out.push('ل');
                out.push(['آ', 'أ', 'إ', 'ا'][((code - 0xFEF5) / 2) as usize]);
            }
            0xFDF2 => out.push_str("الله"),
            _ => out.push(
                ARABIC_FORMS
                    .iter()
                    .find(|&&(first, count, _)| (first..first + u32::from(count)).contains(&code))
                    .map_or(c, |&(_, _, letter)| letter),
            ),
        }
    }
    out
}

/// The presentation forms that are one letter each: the first form's code
/// point, how many forms follow one another (isolated, final, and for a
/// letter that joins on both sides initial and medial), and the letter.
const ARABIC_FORMS: &[(u32, u8, char)] = &[
    (0xFB50, 2, 'ٱ'),
    (0xFB52, 4, 'ٻ'),
    (0xFB56, 4, 'پ'),
    (0xFB5A, 4, 'ڀ'),
    (0xFB5E, 4, 'ٺ'),
    (0xFB62, 4, 'ٿ'),
    (0xFB66, 4, 'ٹ'),
    (0xFB6A, 4, 'ڤ'),
    (0xFB6E, 4, 'ڦ'),
    (0xFB72, 4, 'ڄ'),
    (0xFB76, 4, 'ڃ'),
    (0xFB7A, 4, 'چ'),
    (0xFB7E, 4, 'ڇ'),
    (0xFB82, 2, 'ڍ'),
    (0xFB84, 2, 'ڌ'),
    (0xFB86, 2, 'ڎ'),
    (0xFB88, 2, 'ڈ'),
    (0xFB8A, 2, 'ژ'),
    (0xFB8C, 2, 'ڑ'),
    (0xFB8E, 4, 'ک'),
    (0xFB92, 4, 'گ'),
    (0xFB96, 4, 'ڳ'),
    (0xFB9A, 4, 'ڱ'),
    (0xFB9E, 2, 'ں'),
    (0xFBA0, 4, 'ڻ'),
    (0xFBA4, 2, 'ۀ'),
    (0xFBA6, 4, 'ہ'),
    (0xFBAA, 4, 'ھ'),
    (0xFBAE, 2, 'ے'),
    (0xFBB0, 2, 'ۓ'),
    (0xFBD3, 4, 'ڭ'),
    (0xFBD7, 2, 'ۇ'),
    (0xFBD9, 2, 'ۆ'),
    (0xFBDB, 2, 'ۈ'),
    (0xFBDE, 2, 'ۋ'),
    (0xFBE0, 2, 'ۅ'),
    (0xFBE2, 2, 'ۉ'),
    (0xFBE4, 4, 'ې'),
    (0xFBE8, 2, 'ى'),
    (0xFBFC, 4, 'ی'),
    (0xFE80, 1, 'ء'),
    (0xFE81, 2, 'آ'),
    (0xFE83, 2, 'أ'),
    (0xFE85, 2, 'ؤ'),
    (0xFE87, 2, 'إ'),
    (0xFE89, 4, 'ئ'),
    (0xFE8D, 2, 'ا'),
    (0xFE8F, 4, 'ب'),
    (0xFE93, 2, 'ة'),
    (0xFE95, 4, 'ت'),
    (0xFE99, 4, 'ث'),
    (0xFE9D, 4, 'ج'),
    (0xFEA1, 4, 'ح'),
    (0xFEA5, 4, 'خ'),
    (0xFEA9, 2, 'د'),
    (0xFEAB, 2, 'ذ'),
    (0xFEAD, 2, 'ر'),
    (0xFEAF, 2, 'ز'),
    (0xFEB1, 4, 'س'),
    (0xFEB5, 4, 'ش'),
    (0xFEB9, 4, 'ص'),
    (0xFEBD, 4, 'ض'),
    (0xFEC1, 4, 'ط'),
    (0xFEC5, 4, 'ظ'),
    (0xFEC9, 4, 'ع'),
    (0xFECD, 4, 'غ'),
    (0xFED1, 4, 'ف'),
    (0xFED5, 4, 'ق'),
    (0xFED9, 4, 'ك'),
    (0xFEDD, 4, 'ل'),
    (0xFEE1, 4, 'م'),
    (0xFEE5, 4, 'ن'),
    (0xFEE9, 4, 'ه'),
    (0xFEED, 2, 'و'),
    (0xFEEF, 2, 'ى'),
    (0xFEF1, 4, 'ي'),
];

/// What a font's private-use characters stand for. Word and PowerPoint set
/// Symbol's Greek letters and Wingdings' bullets at the code points of the
/// Private Use Area, U+F020 to U+F0FF; which font drew one says which it is.
#[derive(Clone, Copy, Debug, PartialEq)]
enum SymbolFont {
    Symbol,
    Dingbats,
    /// ZapfDingbats, whose codes are not Wingdings' (see [`zapf_dingbat`]).
    Zapf,
    Other,
}

fn symbol_font(name: Option<&str>) -> SymbolFont {
    let name = name.unwrap_or("").to_ascii_lowercase();
    if name.contains("zapf") || name.contains("dingbats") {
        SymbolFont::Zapf
    } else if name.contains("wingdings") || name.contains("webdings") || name.contains("dingbat") {
        SymbolFont::Dingbats
    } else if name.contains("symbol") {
        SymbolFont::Symbol
    } else {
        SymbolFont::Other
    }
}

/// A private-use character as what it shows: Symbol's letters and signs,
/// Wingdings' bullets and ticks, ZapfDingbats' marks. Where the font is not
/// known, only the bullets no letter of either font shares.
fn private_use(c: char, font: SymbolFont) -> Option<char> {
    let code = c as u32;
    if !(0xF020..=0xF0FF).contains(&code) {
        return None;
    }
    let byte = (code - 0xF000) as u8;
    match font {
        SymbolFont::Symbol => symbol_char(byte),
        SymbolFont::Dingbats => match byte {
            0xFC => Some('✓'),
            0xFB => Some('✗'),
            0x6C | 0x6E | 0x6F | 0x71 | 0x75 | 0x76 | 0x77 | 0x9F | 0xA1 | 0xA2 | 0xA7 | 0xA8
            | 0xD8 => Some('•'),
            _ => None,
        },
        SymbolFont::Zapf => zapf_dingbat(byte),
        SymbolFont::Other => matches!(byte, 0xB7 | 0xA7 | 0xD8 | 0xFC).then_some('•'),
    }
}

/// The Symbol font's encoding, from 0x20: Greek letters, operators, arrows.
fn symbol_char(byte: u8) -> Option<char> {
    const LOW: &str = " !∀#∃%&∋()∗+,−./0123456789:;<=>?≅ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_‾αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼";
    const HIGH: &str = "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…⏐⎯↵ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓◊〈®©™∑";
    match byte {
        0x20..=0x7E => LOW.chars().nth(usize::from(byte - 0x20)),
        0xA0..=0xE5 => HIGH.chars().nth(usize::from(byte - 0xA0)),
        0xF1 => Some('〉'),
        0xF2 => Some('∫'),
        _ => None,
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
pub(crate) fn read<'a>(page: &'a Page<'a>, cache: &InterpreterCache<'a>, scale: f32) -> TextLayer {
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
        fonts: HashMap::new(),
        unnamed: HashMap::new(),
        facts: font_facts(page, cache),
        marked: Vec::new(),
        open: Vec::new(),
        drawn: Vec::new(),
    };
    hayro::hayro_interpret::interpret_page(page, &mut context, &mut device);
    device.layer.glyphs.extend(form_boxes(page, initial));
    device.settle_unnamed_fonts();
    if !device.marked.is_empty() {
        if let Some(sequences) = marked_content(page, device.marked.len()) {
            device.read_actual_text(&sequences);
        }
    }
    device.layer.stand_vertical_text();
    device.layer.turn_upright();
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
            actual: false,
        }
    }

    fn layer(glyphs: Vec<TextGlyph>) -> TextLayer {
        TextLayer {
            glyphs,
            marks: Vec::new(),
            rules: Vec::new(),
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
    fn a_large_heading_and_small_text_beside_it_are_two_lines() {
        // The heading's last word, and a table cell in the next column less
        // than an em of the heading but three of the table's away.
        let mut glyphs = spaced(
            "Ein Satz mit vielen Wörtern in kleiner Schrift",
            100.0,
            60.0,
            10.0,
        );
        glyphs.extend(word("2:", 100.0, 200.0, 40.0));
        glyphs.extend(spaced("Position", 100.0 + 40.0 + 30.0, 200.0, 10.0));
        let lines = layer(glyphs).lines(false);
        let texts: Vec<&str> = lines.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(texts[1..], ["2:", "Position"]);
    }

    #[test]
    fn a_justified_heading_is_joined_across_its_stretched_space() {
        // "Bericht 2:" set justified: the space glyph is ordinary, the next
        // word far off; the heading's next line has letters under the gap.
        let mut glyphs = spaced(
            "Ein Satz mit vielen Wörtern in kleiner Schrift",
            100.0,
            20.0,
            10.0,
        );
        glyphs.extend(word("Bericht", 100.0, 100.0, 40.0));
        glyphs.push(glyph(" ", 240.0, 100.0, 40.0));
        glyphs.extend(word("2:", 560.0, 100.0, 40.0));
        glyphs.extend(word("Zutrittskontrolle", 100.0, 150.0, 40.0));
        // A table cell in the next column, on the heading's baseline.
        glyphs.extend(spaced("Menge", 700.0, 100.0, 10.0));
        let lines = layer(glyphs).lines(false);
        let texts: Vec<&str> = lines.iter().map(|l| l.text.as_str()).collect();
        assert!(texts.contains(&"Bericht 2:"), "{texts:?}");
        assert!(texts.contains(&"Menge"), "{texts:?}");
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
    fn ligatures_are_the_letters_they_join() {
        let ligature = format!("P{}ichten, e{}zient", '\u{FB02}', '\u{FB03}');
        assert_eq!(unligature(ligature), "Pflichten, effizient");
        assert_eq!(unligature("Straße".to_string()), "Straße");
    }

    #[test]
    fn only_reads_any_page_and_never_recognizes_one() {
        let empty = layer(Vec::new());
        assert!(empty.usable(PdfText::Only));
        assert!(!empty.usable(PdfText::Always));
        assert_eq!(PdfText::parse("only"), Some(PdfText::Only));
    }

    #[test]
    fn a_superscript_stays_on_its_line() {
        // `E = mc²` and on: the 2 is set smaller and raised 0.4 em, and is
        // written raised: read as drawn, it is `mc2`.
        let mut glyphs = word("mc", 100.0, 200.0, 10.0);
        glyphs.extend(word("2", 110.0, 195.9, 7.0));
        glyphs.extend(word("for", 116.0, 200.0, 10.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "mc² for");
    }

    #[test]
    fn an_invisible_layer_over_shown_text_is_not_read_twice() {
        let shown = word("Vertrag", 100.0, 200.0, 20.0);
        let mut glyphs = shown.clone();
        // The same word again, invisibly and a little off: a DMS's OCR.
        glyphs.extend(
            word("Vertrag", 101.0, 201.0, 19.0)
                .into_iter()
                .map(|mut g| {
                    g.visible = false;
                    g
                }),
        );
        // And an invisible word where nothing is shown: the OCR of a picture.
        glyphs.extend(
            word("Stempel", 100.0, 400.0, 20.0)
                .into_iter()
                .map(|mut g| {
                    g.visible = false;
                    g
                }),
        );
        let texts: Vec<String> = layer(glyphs)
            .lines(true)
            .into_iter()
            .map(|l| l.text)
            .collect();
        assert_eq!(texts, ["Vertrag", "Stempel"]);
    }

    #[test]
    fn a_page_in_a_font_without_unicode_is_not_mapped_enough() {
        let mut glyphs = word("Hallo", 100.0, 200.0, 20.0);
        assert!(layer(glyphs.clone()).mapped_enough());
        for glyph in &mut glyphs {
            glyph.mapped = false;
        }
        assert!(!layer(glyphs).mapped_enough());
    }

    #[test]
    fn a_watermark_across_the_page_does_not_hide_the_ocr_beneath() {
        let mut glyphs: Vec<TextGlyph> = word("Vertrag", 100.0, 300.0, 20.0)
            .into_iter()
            .map(|mut g| {
                g.visible = false;
                g
            })
            .collect();
        // VERTRAULICH, shown, drawn at 45 degrees across the hidden line.
        for (i, c) in "VERTRAULICH".chars().enumerate() {
            let step = 14.0 * i as f64;
            glyphs.push(TextGlyph {
                text: c.to_string(),
                origin: KPoint::new(60.0 + step, 420.0 - step),
                end: KPoint::new(70.0 + step, 410.0 - step),
                up: Vec2::new(-14.0, -14.0),
                visible: true,
                mapped: true,
                actual: false,
            });
        }
        let texts: Vec<String> = layer(glyphs)
            .lines(true)
            .into_iter()
            .map(|l| l.text)
            .collect();
        assert!(texts.iter().any(|t| t == "Vertrag"), "{texts:?}");
    }

    #[test]
    fn symbol_font_bullets_are_bullets_and_a_stamp_leaves_the_page_readable() {
        assert_eq!(private_use('\u{F0B7}', SymbolFont::Other), Some('•'));
        assert_eq!(private_use('a', SymbolFont::Other), None);
        assert_eq!(private_use('\u{F071}', SymbolFont::Other), None);
        // θ in Symbol, a square bullet in Wingdings.
        assert_eq!(private_use('\u{F071}', SymbolFont::Symbol), Some('θ'));
        assert_eq!(private_use('\u{F071}', SymbolFont::Dingbats), Some('•'));
        assert_eq!(private_use('\u{F0B7}', SymbolFont::Symbol), Some('•'));
        assert_eq!(private_use('\u{F0A3}', SymbolFont::Symbol), Some('≤'));
        assert_eq!(private_use('\u{F0FC}', SymbolFont::Dingbats), Some('✓'));
        assert_eq!(
            symbol_font(Some("ABCDEF+Wingdings-Regular")),
            SymbolFont::Dingbats
        );
        assert_eq!(symbol_font(Some("SymbolMT")), SymbolFont::Symbol);
        // A scan's hidden text, a page of it.
        let mut glyphs: Vec<TextGlyph> = (0..20)
            .flat_map(|row| word("Zeile des Vertrags", 100.0, 100.0 + 30.0 * row as f64, 20.0))
            .map(|mut g| {
                g.visible = false;
                g
            })
            .collect();
        // A stamp in a symbol font, shown: nothing of it maps to Unicode.
        glyphs.extend(word("XY", 100.0, 400.0, 20.0).into_iter().map(|mut g| {
            g.mapped = false;
            g
        }));
        assert!(layer(glyphs).readable_enough());
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

    /// A one-page PDF of `objects`: the catalog, the page tree and a US
    /// Letter page are objects 1 to 3, and the page's `resources`,
    /// `content` and `extra` page entries refer to the objects given, which
    /// are numbered from 5 on; object 4 is the content stream.
    fn pdf(resources: &str, extra: &str, content: &str, objects: &[String]) -> Vec<u8> {
        let mut all = vec![
            "<< /Type /Catalog /Pages 2 0 R >>".to_string(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_string(),
            format!(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] \
                 /Resources << {resources} >> /Contents 4 0 R {extra} >>"
            ),
            format!(
                "<< /Length {} >>\nstream\n{content}\nendstream",
                content.len()
            ),
        ];
        all.extend(objects.iter().cloned());
        let mut file = String::from("%PDF-1.4\n");
        let mut offsets = Vec::new();
        for (i, body) in all.iter().enumerate() {
            offsets.push(file.len());
            file.push_str(&format!("{} 0 obj\n{body}\nendobj\n", i + 1));
        }
        let xref = file.len();
        file.push_str(&format!("xref\n0 {}\n0000000000 65535 f \n", all.len() + 1));
        for offset in offsets {
            file.push_str(&format!("{offset:010} 00000 n \n"));
        }
        file.push_str(&format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            all.len() + 1
        ));
        file.into_bytes()
    }

    /// The text layer of the first page of `file`, a point to a pixel.
    fn read_pdf(file: Vec<u8>) -> TextLayer {
        let pdf = hayro::hayro_syntax::Pdf::new(std::sync::Arc::new(file)).expect("a PDF");
        let pages = pdf.pages();
        let cache = InterpreterCache::new();
        read(&pages[0], &cache, 1.0)
    }

    fn texts(lines: &[Line]) -> Vec<&str> {
        lines.iter().map(|l| l.text.as_str()).collect()
    }

    /// `text` set with `tracking` ems of letter spacing, spaces as glyphs.
    fn tracked(text: &str, x: f64, y: f64, size: f64, tracking: f64) -> Vec<TextGlyph> {
        text.chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    &c.to_string(),
                    x + i as f64 * (0.5 + tracking) * size,
                    y,
                    size,
                )
            })
            .collect()
    }

    #[test]
    fn letter_spaced_text_is_read_as_its_words() {
        // Tracked capitals, a fifth of an em apart, and a title expanded by
        // a sixth of an em with a space glyph between its words; the page
        // sets its spaces.
        let mut glyphs = spaced(
            "Die Stadtwerke haben ihre Ziele erreicht",
            100.0,
            100.0,
            10.0,
        );
        glyphs.extend(tracked("PRESSEMITTEILUNG", 100.0, 150.0, 10.0, 0.2));
        glyphs.extend(tracked(
            "Jahresbericht Stadtwerke",
            100.0,
            250.0,
            30.0,
            0.17,
        ));
        // Spaced out wider than any cell's padding: its space glyphs say
        // it is text.
        glyphs.extend(tracked("Neustadt Mitte", 100.0, 350.0, 20.0, 0.6));
        let lines = layer(glyphs).lines(false);
        assert_eq!(
            texts(&lines)[1..],
            [
                "PRESSEMITTEILUNG",
                "Jahresbericht Stadtwerke",
                "Neustadt Mitte"
            ]
        );
        assert!(lines[1].segments.is_empty(), "tracking is no table");
        // Letters that touch keep their word gaps as they were.
        let mut glyphs = word("Elektro", 100.0, 200.0, 20.0);
        glyphs.extend(word("montiert", 100.0 + 70.0 + 6.0, 200.0, 20.0));
        assert_eq!(layer(glyphs).lines(false)[0].text, "Elektro montiert");
    }

    #[test]
    fn a_row_of_one_digit_cells_is_no_letter_spacing() {
        // Evenly spaced, as letter-spaced text is — but numbers, in cells.
        let mut glyphs = spaced("Die Kosten des Projekts im Überblick", 100.0, 100.0, 20.0);
        glyphs.extend(tracked("1234", 100.0, 200.0, 20.0, 0.4));
        // Letters, but further apart than letter spacing goes without a
        // space glyph between the words.
        glyphs.extend(tracked("ABCD", 100.0, 300.0, 20.0, 0.6));
        let lines = layer(glyphs).lines(false);
        for line in &lines[1..] {
            let cells: Vec<&str> = line.segments.iter().map(|s| s.text.as_str()).collect();
            assert_eq!(cells.len(), 4, "{cells:?}");
        }
    }

    #[test]
    fn a_soft_hyphen_printed_as_a_space_of_no_width_does_not_split_its_word() {
        // A browser prints `Versicherungs&shy;bedingungen` with a space
        // glyph after the `s` and the `b` drawn where the space starts; where
        // the line breaks at one, the hyphen is drawn there instead.
        let mut glyphs = word("Versicherungs", 100.0, 200.0, 20.0);
        glyphs.push(glyph(" ", 230.0, 200.0, 20.0));
        glyphs.extend(word("bedingungen", 230.0, 200.0, 20.0));
        glyphs.extend(word("Donau", 100.0, 240.0, 20.0));
        glyphs.push(glyph(" ", 150.0, 240.0, 20.0));
        glyphs.push(glyph("‐", 150.0, 240.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines), ["Versicherungsbedingungen", "Donau‐"]);
    }

    #[test]
    fn text_with_a_shadow_is_read_once() {
        // CSS `text-shadow: 2px 2px`: the line in grey two pixels off, then
        // the line itself. A letter that repeats ("mm") is still two.
        let mut glyphs = word("Sommerfest", 102.0, 202.0, 40.0);
        glyphs.extend(word("Sommerfest", 100.0, 200.0, 40.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines), ["Sommerfest"]);
    }

    #[test]
    fn superscripts_and_subscripts_are_written_raised_and_lowered() {
        // `3·10⁶ Zellen`, a footnote's `¹` opening its line, `H₂O`.
        let mut glyphs = word("3·10", 100.0, 200.0, 16.0);
        glyphs.extend(word("6", 132.0, 193.7, 13.3));
        glyphs.push(glyph(" ", 138.7, 200.0, 16.0));
        glyphs.extend(word("Zellen", 142.7, 200.0, 16.0));
        glyphs.extend(word("1", 100.0, 293.7, 13.3));
        glyphs.push(glyph(" ", 106.7, 300.0, 16.0));
        glyphs.extend(word("Vorläufige", 110.7, 300.0, 16.0));
        glyphs.extend(word("H", 100.0, 400.0, 16.0));
        glyphs.extend(word("2", 108.0, 403.0, 11.0));
        glyphs.extend(word("O", 113.5, 400.0, 16.0));
        // Raised and smaller, but letters Unicode has no raised form of: as
        // they are, the way the other readers write them.
        glyphs.extend(word("21", 100.0, 500.0, 16.0));
        glyphs.extend(word("st", 116.0, 493.7, 13.3));
        let lines = layer(glyphs).lines(false);
        assert_eq!(
            texts(&lines),
            ["3·10⁶ Zellen", "¹ Vorläufige", "H₂O", "21st"]
        );
        // Hidden text is somebody's OCR, its words sized one by one.
        let hidden: Vec<TextGlyph> = word("mc", 100.0, 200.0, 10.0)
            .into_iter()
            .chain(word("2", 110.0, 195.9, 7.0))
            .map(|mut g| {
                g.visible = false;
                g
            })
            .collect();
        assert_eq!(layer(hidden).lines(true)[0].text, "mc2");
    }

    #[test]
    fn a_drop_cap_is_a_line_of_its_own() {
        // The paragraph's first letter, three lines tall, drawn before the
        // paragraph and standing on the baseline of its third line.
        let mut glyphs = word("D", 100.0, 300.0, 66.0);
        glyphs.extend(spaced("ie Stadt liegt", 140.0, 252.0, 20.0));
        glyphs.extend(spaced("am Ufer des", 140.0, 276.0, 20.0));
        glyphs.extend(spaced("Jahrhundert wuchs", 140.0, 300.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(
            texts(&lines),
            ["D", "ie Stadt liegt", "am Ufer des", "Jahrhundert wuchs"]
        );
    }

    #[test]
    fn a_type3_font_advances_by_its_own_widths() {
        // Helvetica's widths in a Type3 font: `m` is 0.833 em wide, and read
        // as half an em it leaves a word space after itself.
        let widths: Vec<(char, u32)> = vec![
            ('S', 667),
            ('u', 556),
            ('m', 833),
            ('a', 556),
            ('r', 333),
            ('y', 500),
        ];
        let first = 0x53;
        let mut table = vec![0; 0x7A - first + 1];
        for &(c, w) in &widths {
            table[c as usize - first] = w;
        }
        let bf: String = widths
            .iter()
            .map(|&(c, _)| format!("<{:02X}> <{:04X}> ", c as u32, c as u32))
            .collect();
        let cmap = format!(
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap \
             1 begincodespacerange <00> <FF> endcodespacerange \
             {} beginbfchar {bf}endbfchar endcmap end end",
            widths.len()
        );
        let widths = table
            .iter()
            .map(u32::to_string)
            .collect::<Vec<_>>()
            .join(" ");
        let file = pdf(
            "/Font << /T3 5 0 R >>",
            "",
            "BT /T3 24 Tf 72 700 Td (Summary) Tj ET",
            &[
                format!(
                    "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1000 1000] \
                     /FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << >> \
                     /FirstChar {first} /LastChar 122 /Widths [{widths}] /ToUnicode 6 0 R >>"
                ),
                format!("<< /Length {} >>\nstream\n{cmap}\nendstream", cmap.len()),
            ],
        );
        let lines = read_pdf(file).lines(false);
        assert_eq!(texts(&lines), ["Summary"]);
    }

    #[test]
    fn zapf_dingbats_marks_are_ticks_and_crosses() {
        // The standard font, not embedded, drawn by its own codes.
        let file = pdf(
            "/Font << /F1 5 0 R /Z 6 0 R >>",
            "",
            "BT /Z 11 Tf 72 700 Td (4) Tj ET BT /F1 11 Tf 90 700 Td (Licht ok) Tj ET \
             BT /Z 11 Tf 72 680 Td (8) Tj ET BT /F1 11 Tf 90 680 Td (Tuer defekt) Tj ET",
            &[
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_string(),
                "<< /Type /Font /Subtype /Type1 /BaseFont /ZapfDingbats >>".to_string(),
            ],
        );
        let page = read_pdf(file);
        assert!(page.readable_enough());
        let lines = page.lines(false);
        let all = texts(&lines).join(" / ");
        assert!(all.contains("✔") && all.contains("✘"), "{all}");
        assert!(!all.contains('\u{FFFD}'), "{all}");
        // Embedded under its name, its codes in the Private Use Area.
        assert_eq!(symbol_font(Some("ABCDEF+ZapfDingbats")), SymbolFont::Zapf);
        assert_eq!(private_use('\u{F034}', SymbolFont::Zapf), Some('✔'));
        assert_eq!(zapf_dingbat(0x33), Some('✓'));
        assert_eq!(zapf_dingbat(0x6C), Some('●'));
        assert_eq!(zapf_dingbat(0x75), Some('◆'));
        assert_eq!(zapf_dingbat(0xAC), Some('①'));
        assert_eq!(zapf_dingbat(0xD5), Some('→'));
    }

    #[test]
    fn a_hollow_bullet_is_a_bullet_and_a_small_stroked_square_is_not() {
        // A nested list's circle, stroked, before "Server"; a tick box's
        // square outline before "offen".
        let circle = "77 703 m 77 704.1 76.1 705 75 705 c 73.9 705 73 704.1 73 703 c \
                      73 701.9 73.9 701 75 701 c 76.1 701 77 701.9 77 703 c h S";
        let file = pdf(
            "/Font << /F1 5 0 R >>",
            "",
            &format!(
                "{circle} BT /F1 11 Tf 84 700 Td (Server) Tj ET \
                 72 598 6 6 re S BT /F1 11 Tf 84 600 Td (offen) Tj ET"
            ),
            &["<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_string()],
        );
        let lines = read_pdf(file).lines(false);
        assert_eq!(texts(&lines), ["• Server", "offen"]);
    }

    #[test]
    fn a_form_s_check_boxes_read_as_ticked_or_not() {
        // Two boxes whose looks are kept by state; the first is ticked.
        let boxes = |name: &str, state: &str, x: u32| {
            format!(
                "<< /Type /Annot /Subtype /Widget /FT /Btn /T ({name}) /V /{state} /AS /{state} \
                 /Rect [{x} 666 {} 680] /F 4 /AP << /N << /Yes 7 0 R /Off 7 0 R >> >> >>",
                x + 14
            )
        };
        let file = pdf(
            "/Font << /F1 5 0 R >>",
            "/Annots [6 0 R 8 0 R]",
            "BT /F1 12 Tf 200 670 Td (ja) Tj ET BT /F1 12 Tf 280 670 Td (nein) Tj ET",
            &[
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_string(),
                boxes("ja", "Yes", 182),
                // Each state's look: empty here, as the box has one.
                "<< /Type /XObject /Subtype /Form /BBox [0 0 14 14] /Length 0 >>\n\
                 stream\n\nendstream"
                    .to_string(),
                boxes("nein", "Off", 262),
            ],
        );
        let lines = read_pdf(file).lines(false);
        assert_eq!(texts(&lines).join(" "), "☒ ja ☐ nein");
    }

    #[test]
    fn right_to_left_text_is_read_in_its_own_order() {
        // Hebrew as a page draws it, left to right: the words in reverse
        // order and each spelled backwards; the number and the Latin words in
        // it the way they read.
        let visual = "םידבוע Microsoft Office 2026 תנשב";
        let lines = layer(spaced(visual, 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(lines[0].text, "בשנת 2026 Microsoft Office עובדים");
        // The words keep their places on the page, left to right.
        let x: Vec<f32> = lines[0].words.iter().map(|w| w.bbox.x0).collect();
        assert!(x.windows(2).all(|p| p[0] < p[1]), "{x:?}");
        assert_eq!(lines[0].words[0].text, "עובדים");
        // A sentence's full stop is drawn at its left end, and brackets
        // facing the other way.
        let lines = layer(spaced(".(םלוע) םולש", 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(lines[0].text, "שלום (עולם).");
        // In Arabic a number takes no percent sign in with it: it stands
        // left of the number, and is read after it.
        let lines = layer(spaced("يف %12 ةبسنب", 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(lines[0].text, "بنسبة 12% في");
        // A Hebrew name in German text.
        let lines = layer(spaced("Kontakt: ןהכ דוד (Leitung)", 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(lines[0].text, "Kontakt: דוד כהן (Leitung)");
        // A Hebrew sentence ending in a web address: its words, not the
        // address's many letters, say which way it reads.
        let visual = "www.example.co.il/reports רתאב ןימז חודה";
        let lines = layer(spaced(visual, 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(lines[0].text, "הדוח זמין באתר www.example.co.il/reports");
        // Arabic shapes are the letters they shape.
        assert_eq!(arabic_letters("ﺗﻘﺮﻳﺮ ﻻ ﷲ".to_string()), "تقرير لا الله");
        assert_eq!(arabic_letters("Bericht".to_string()), "Bericht");
    }

    #[test]
    fn a_column_of_vertical_text_is_one_line_and_columns_read_right_to_left() {
        // Two columns of upright glyphs, each an em below the one before:
        // the right one drawn first, as vertical Japanese is set.
        let column = |text: &str, x: f64| -> Vec<TextGlyph> {
            text.chars()
                .enumerate()
                .map(|(i, c)| TextGlyph {
                    end: KPoint::new(x + 20.0, 100.0 + 20.0 * i as f64),
                    ..glyph(&c.to_string(), x, 100.0 + 20.0 * i as f64, 20.0)
                })
                .collect()
        };
        let mut glyphs = column("吾輩は猫である。", 500.0);
        glyphs.extend(column("名前はまだ無い。", 470.0));
        let mut page = layer(glyphs);
        page.stand_vertical_text();
        page.turn_upright();
        let lines = page.lines(false);
        assert_eq!(texts(&lines), ["吾輩は猫である。", "名前はまだ無い。"]);
        assert!(lines.iter().all(|l| l.angle.abs() < 1.0));
        assert!(
            lines[0].bbox.y1 <= lines[1].bbox.y0,
            "the right column first"
        );
        // A paragraph so narrow it holds a character a line, the next one
        // under it: set across the page, a line at a time.
        let mut glyphs = column("吾輩は猫である。", 500.0);
        glyphs.extend(column("名前", 500.0).into_iter().map(|mut g| {
            g.origin.y += 200.0;
            g.end.y += 200.0;
            g
        }));
        let mut page = layer(glyphs);
        page.stand_vertical_text();
        assert!(page.glyphs.iter().all(|g| g.angle().abs() < 1.0));
        // A column of three cells in a table, drawn row by row, is left be.
        let mut rows = word("Anna", 100.0, 100.0, 20.0);
        rows.extend(word("Ben", 100.0, 130.0, 20.0));
        rows.extend(word("Clara", 100.0, 160.0, 20.0));
        let mut page = layer(rows);
        page.stand_vertical_text();
        assert!(page.glyphs.iter().all(|g| g.angle().abs() < 1.0));
    }

    #[test]
    fn a_cluster_is_read_as_the_text_its_marked_content_says_it_is() {
        // As Chrome prints Hindi and Arabic: each cluster in a `/Span` with
        // its `/ActualText`. Hindi's `हि` draws its vowel sign first; an
        // Arabic letter with its vowel mark draws the mark first, moved up
        // onto the letter with `Td`; `ß` stands for two glyphs, its text
        // in PDFDocEncoding. (Helvetica's glyphs stand in for the script's.)
        let file = pdf(
            "/Font << /F1 5 0 R >>",
            "",
            "BT /F1 12 Tf 72 700 Td \
             /Span <</ActualText <FEFF0939093F>>> BDC (ih) Tj EMC \
             /Span <</ActualText <FEFF0928>>> BDC (n) Tj EMC ET \
             BT /F1 12 Tf 72 680 Td \
             /Span <</ActualText <FEFF0645064E>>> BDC 3 6 Td (') Tj -3 -6 Td (m) Tj EMC ET \
             BT /F1 12 Tf 72 660 Td (Stra) Tj /Span <</ActualText (\\337)>> BDC (ss) Tj EMC (e) Tj ET",
            &["<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_string()],
        );
        let lines = read_pdf(file).lines(false);
        assert_eq!(texts(&lines), ["हिन", "مَ", "Straße"]);
        assert_eq!(text_string(b"\xFE\xFF\x00\x41\x00\xFC"), "Aü");
        assert_eq!(text_string(b"Stra\xDFe \x93"), "Straße ﬁ");

        // Hebrew's brackets are drawn mirrored, and Chrome says what each
        // is: that text is not mirrored back.
        let glyphs = spaced("(םלוע) םולש", 100.0, 200.0, 20.0)
            .into_iter()
            .map(|mut g| {
                if "()".contains(g.text.as_str()) {
                    g.text = mirrored(g.text.chars().next().unwrap_or(' ')).to_string();
                    g.actual = true;
                }
                g
            })
            .collect();
        assert_eq!(texts(&layer(glyphs).lines(false)), ["שלום (עולם)"]);

        // A sequence around a word broken over two lines says what the
        // word is; neither line holds it all, and its glyphs stay.
        let mut glyphs = word("Silben-", 100.0, 200.0, 20.0);
        glyphs.extend(word("trennung", 100.0, 230.0, 20.0));
        let advanced = vec![true; glyphs.len()];
        assert!(stand_in(&glyphs, &advanced, "Silbentrennung").is_none());
        // Where the sequences read again are not the ones the interpreter
        // began, no text is told from another's.
        let mut device = Collector {
            layer: layer(word("ih", 100.0, 200.0, 20.0)),
            fonts: HashMap::new(),
            unnamed: HashMap::new(),
            facts: FontFacts::default(),
            marked: vec![b"Span".to_vec()],
            open: Vec::new(),
            drawn: vec![(vec![0], true), (vec![0], true)],
        };
        let said = |tag: &[u8]| Marked {
            tag: tag.to_vec(),
            text: Some("हि".to_string()),
        };
        device.read_actual_text(&[said(b"P")]);
        assert_eq!(device.layer.glyphs.len(), 2);
        device.read_actual_text(&[said(b"Span")]);
        assert_eq!(texts(&device.layer.lines(false)), ["हि"]);
    }

    /// A glyph of `text` from `x` to `end` on the baseline `y`, raised by
    /// `raised` pixels.
    fn placed(text: &str, x: f64, end: f64, y: f64, raised: f64) -> TextGlyph {
        TextGlyph {
            end: KPoint::new(end, y - raised),
            ..glyph(text, x, y - raised, 20.0)
        }
    }

    #[test]
    fn a_mark_drawn_as_a_glyph_of_its_own_stays_with_its_letter() {
        // Arabic `كَتَبَ` as drawn, left to right: each letter, then its
        // fatha set over it half an em up, with no advance of its own.
        let mut glyphs = Vec::new();
        for (i, letter) in ["ب", "ت", "ك"].iter().enumerate() {
            let x = 100.0 + 10.0 * i as f64;
            glyphs.push(placed(letter, x, x + 10.0, 200.0, 0.0));
            glyphs.push(placed("\u{64E}", x + 4.0, x + 4.0, 200.0, 10.0));
        }
        // Hindi `किताब`: the vowel sign `ि` drawn before its consonant, and
        // `ा`, a spacing sign, after its own.
        glyphs.push(placed("ि", 100.0, 105.0, 250.0, 0.0));
        for (i, letter) in ["क", "त"].iter().enumerate() {
            let x = 105.0 + 10.0 * i as f64;
            glyphs.push(placed(letter, x, x + 10.0, 250.0, 0.0));
        }
        glyphs.push(placed("ा", 125.0, 130.0, 250.0, 0.0));
        glyphs.push(placed("ब", 130.0, 140.0, 250.0, 0.0));
        // Thai `เช่น`: the tone mark where its letter ends, raised, and the
        // next letter drawn from the same place.
        glyphs.push(placed("เ", 100.0, 110.0, 300.0, 0.0));
        glyphs.push(placed("ช", 110.0, 120.0, 300.0, 0.0));
        glyphs.push(placed("\u{E48}", 120.0, 120.0, 300.0, 12.0));
        glyphs.push(placed("น", 120.0, 130.0, 300.0, 0.0));
        // Hebrew `שָׁלוֹם` drawn right to left's way, each point after its
        // letter: the qamats under the shin, the shin dot over it, the
        // holam over the vav.
        glyphs.push(placed("ם", 100.0, 110.0, 350.0, 0.0));
        glyphs.push(placed("ו", 110.0, 115.0, 350.0, 0.0));
        glyphs.push(placed("\u{5B9}", 112.0, 112.0, 350.0, 16.0));
        glyphs.push(placed("ל", 115.0, 125.0, 350.0, 0.0));
        glyphs.push(placed("ש", 125.0, 137.0, 350.0, 0.0));
        glyphs.push(placed("\u{5B8}", 130.0, 130.0, 350.0, -6.0));
        glyphs.push(placed("\u{5C1}", 135.0, 135.0, 350.0, 16.0));
        // Hindi `कित` drawn in the text's order: `ि` after its consonant,
        // moved back over it; it is not the next one's.
        glyphs.push(placed("क", 100.0, 110.0, 400.0, 0.0));
        glyphs.push(placed("ि", 101.0, 106.0, 400.0, 0.0));
        glyphs.push(placed("त", 110.0, 120.0, 400.0, 0.0));
        let lines = layer(glyphs).lines(false);
        let nfc = |s: &str| s.nfc().collect::<String>();
        assert_eq!(
            texts(&lines),
            [
                nfc("كَتَبَ"),
                nfc("किताब"),
                nfc("เช่น"),
                nfc("שָׁלוֹם"),
                nfc("कित")
            ]
        );
    }

    #[test]
    fn an_accent_tex_draws_over_a_letter_is_the_accented_letter() {
        // OT1 has no `ü`: pdfTeX draws `¨`, kerns back and draws `u` under
        // it. `é` the other way round: the `e`, then `´` back over it. `í`
        // is `´` over a dotless `ı`.
        let over = |accent: &str, letter: &str, x: f64, first: bool| {
            let accent = placed(accent, x + 2.0, x + 8.0, 200.0, 0.0);
            let letter = placed(letter, x, x + 10.0, 200.0, 0.0);
            if first {
                vec![accent, letter]
            } else {
                vec![letter, accent]
            }
        };
        let mut glyphs = word("M", 100.0, 200.0, 20.0);
        glyphs.extend(over("¨", "u", 110.0, true));
        glyphs.extend(word("nchen", 120.0, 200.0, 20.0));
        glyphs.extend(word("Poincar", 100.0, 250.0, 20.0));
        glyphs.extend(over("´", "e", 170.0, false).into_iter().map(|mut g| {
            g.origin.y += 50.0;
            g.end.y += 50.0;
            g
        }));
        glyphs.extend(word("Mart", 100.0, 300.0, 20.0));
        glyphs.extend(over("´", "ı", 140.0, true).into_iter().map(|mut g| {
            g.origin.y += 100.0;
            g.end.y += 100.0;
            g
        }));
        glyphs.extend(word("n", 150.0, 300.0, 20.0));
        // An accent beside a letter, with its own advance, is a character.
        glyphs.extend(word("a^b", 100.0, 350.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines), ["München", "Poincaré", "Martín", "a^b"]);
    }

    #[test]
    fn a_right_to_left_list_keeps_the_space_after_its_number() {
        // Chrome draws an item's marker apart from the item: the space and
        // the full stop, then the number, at the item's right end.
        let mut glyphs = vec![
            glyph(" ", 250.0, 200.0, 20.0),
            glyph(".", 255.0, 200.0, 20.0),
            glyph("1", 260.0, 200.0, 20.0),
        ];
        glyphs.extend(spaced("הכרב ירבדו החיתפ", 100.0, 200.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines), ["1. פתיחה ודברי ברכה"]);
    }

    #[test]
    fn a_line_whose_ends_disagree_reads_the_way_its_page_does() {
        // A German line ending in an Arabic name, as many words each way:
        // it starts at its left end.
        let visual = "Ansprechpartner: Herr يلعلا دمحم";
        let lines = layer(spaced(visual, 100.0, 200.0, 20.0)).lines(false);
        assert_eq!(texts(&lines), ["Ansprechpartner: Herr محمد العلي"]);
        // Fewer German words than Arabic ones, on a German page.
        let mut glyphs = spaced("Die Beratung ist kostenlos.", 100.0, 100.0, 20.0);
        glyphs.extend(spaced("Übersetzung: مكب ابحرم", 100.0, 200.0, 20.0));
        glyphs.extend(spaced("Wir freuen uns auf Sie.", 100.0, 300.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines)[1], "Übersetzung: مرحبا بكم");
        // A Hebrew sentence ending in a product's name, on a Hebrew page.
        let mut glyphs = spaced("םויה בשחמב שומיש", 100.0, 100.0, 20.0);
        glyphs.extend(spaced("Microsoft Office ןימז חודה", 100.0, 200.0, 20.0));
        glyphs.extend(spaced("הבוט הדובע", 100.0, 300.0, 20.0));
        let lines = layer(glyphs).lines(false);
        assert_eq!(texts(&lines)[1], "הדוח זמין Microsoft Office");
    }
}
