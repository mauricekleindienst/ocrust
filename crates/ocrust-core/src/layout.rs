//! Layout analysis: reading order, paragraph grouping and word boxes.
//!
//! A detector returns an unordered pile of line boxes. Everything that makes
//! OCR output *readable* — column order, paragraph breaks, joined hyphenated
//! words — happens here, without any extra model.

use crate::doc::{Block, BlockKind, Line, Word};
use crate::geom::{crop_stands_up, Point, Quad, Rect};
use crate::recognize::CharSpan;

/// Tunables for layout grouping.
#[derive(Debug, Clone)]
pub struct LayoutConfig {
    /// Lines closer than `line_gap_factor * median_height` join one paragraph.
    pub line_gap_factor: f32,
    /// Minimum gutter width (in median line heights) to accept a column split.
    pub column_gap_factor: f32,
    /// Refuse a column split when this share of baselines has boxes on both
    /// sides of the gutter *and* one side is narrow (see
    /// [`Self::column_min_width_over_gap`]).
    ///
    /// Rows of a table straddle the gutter; page columns are wide blocks. Both
    /// conditions together tell an invoice's cells from a newspaper's columns.
    pub column_shared_baseline_veto: f32,
    /// A page column has to be at least this many gutter widths wide.
    ///
    /// A receipt's right-hand price column is narrower than the gap before it; a
    /// newspaper column is several times wider.
    pub column_min_width_over_gap: f32,
    /// A page column also has to cover at least this share of the region.
    ///
    /// Together with the rule above this separates a table's narrow cell column
    /// from a genuine page column, which is always a substantial slice of the
    /// page.
    pub column_min_width_fraction: f32,
    /// A corridor between two page columns has to be this many line heights
    /// wide.
    ///
    /// Columns are looked for before rows, because a page whose columns sit on
    /// one baseline grid offers a horizontal gap between every pair of lines,
    /// and cutting there first would read the lines across the gutter. Only a
    /// gap this wide gets that precedence; a table's gutters are far narrower.
    pub column_corridor_gap_factor: f32,
    /// A corridor only splits a region this wide a share of the page.
    ///
    /// Columns are a property of the page: they split all of it. A drawing's
    /// title block is two columns of its own — labels down one side, values down
    /// the other — and reading it that way is not reading it.
    pub column_corridor_page_share: f32,
    /// A page column carries at most this many boxes per baseline.
    ///
    /// A paragraph's line is one box. A table row puts a box in every cell, and
    /// that is what tells a table with wide cells from a column of text.
    pub column_max_boxes_per_band: f32,
    /// A page column has to span at least this many baselines.
    pub column_min_bands: usize,
    /// How many corridors a page may be cut at, one fewer than its columns.
    pub column_max_corridors: usize,
    /// The narrowest column, over the widest one.
    ///
    /// A page's columns are near enough the same width. A label and the value
    /// beside it are not, and neither is a table's name column next to its
    /// figures.
    pub column_width_evenness: f32,
    /// Minimum gutter width as a fraction of the content width.
    ///
    /// This is what separates a page laid out in columns from a table: a
    /// newspaper's gutter is several percent of the page, while the gaps between
    /// table cells are around one percent. Without it, every ruled invoice comes
    /// back read column by column.
    pub column_gap_min_fraction: f32,
    /// A line this much taller than the median is treated as a heading.
    pub heading_height_factor: f32,
    /// Join detected boxes that sit on the same baseline into one text line.
    ///
    /// A detector returns boxes, not lines: a receipt's `Milch 1L` and its
    /// right-aligned `1,19` are two boxes on one line, and so are the cells of a
    /// table row. Without this, they come out as separate lines in an order that
    /// depends on their x positions.
    pub merge_baselines: bool,
    /// How much of the shorter box's height must overlap for a merge.
    pub baseline_overlap: f32,
    /// Largest gap, in line heights, that is merged on a page without a
    /// repeating column structure.
    ///
    /// A table or a receipt has cells at the same x positions in row after row,
    /// and there a wide gap still belongs to one row. A drawing's labels are
    /// scattered, and joining two of them because they happen to sit at the same
    /// height would invent a line that is not there.
    pub max_merge_gap_factor: f32,
    /// Join words split across a line break by a trailing hyphen.
    pub dehyphenate: bool,
    /// Detect columns and read them one after another.
    pub detect_columns: bool,
    /// Read a block whose cells line up into columns as a table.
    pub detect_tables: bool,
}

impl Default for LayoutConfig {
    fn default() -> Self {
        Self {
            line_gap_factor: 0.9,
            column_gap_factor: 1.2,
            column_gap_min_fraction: 0.035,
            column_shared_baseline_veto: 0.6,
            column_min_width_over_gap: 2.0,
            column_min_width_fraction: 0.25,
            column_corridor_gap_factor: 2.0,
            column_corridor_page_share: 0.6,
            column_max_boxes_per_band: 1.3,
            column_min_bands: 3,
            column_max_corridors: 4,
            column_width_evenness: 0.6,
            heading_height_factor: 1.45,
            merge_baselines: true,
            baseline_overlap: 0.55,
            max_merge_gap_factor: 4.0,
            dehyphenate: true,
            detect_columns: true,
            detect_tables: true,
        }
    }
}

/// Median of the line heights, used as the page's scale reference.
/// Median height of the text itself, taken from the detection polygons.
///
/// De-hyphenation unions two lines' boxes into one, so a box is no measure of
/// how tall the text is; the quad it came from still is.
fn median_text_height(lines: &[Line]) -> f32 {
    if lines.is_empty() {
        return 1.0;
    }
    let mut hs: Vec<f32> = lines
        .iter()
        .map(|l| l.quad.edge_height().max(1.0))
        .collect();
    hs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    hs[hs.len() / 2]
}

/// Print smaller than this, in points, is small print: footnotes, the terms
/// of business, a caption.
const SMALL_PRINT_POINTS: f32 = 8.0;
/// A line's box on a page read from its own text, as a share of its font
/// size: the ascent and the descent together.
const LINE_OVER_SIZE: f32 = 1.2;

/// Share of the characters, over at least this many lines, that makes a size
/// the body's whatever else the page holds: more lines than a heading has.
const BODY_SHARE: f32 = 0.3;
const BODY_LINES: usize = EXACT_HEADING_LINES + 1;
/// Characters, at most, of the one line outside a page's tables for it to be
/// a title over the tables rather than text of their own.
const TITLE_CHARS: usize = 80;

/// The size of the body text on a page whose sizes are exact.
///
/// The lower median line's, outside the tables, so that a slide's title over
/// one line of text, or a heading over a table and a note, is not taken for
/// the body — or any larger size that sets paragraphs and a large share of
/// the characters: a page crowded with footnotes is still its text's. When
/// no more than a title stands outside the tables, their text counts too: a
/// title over a table stands out from the table.
///
/// Small print is left out when the rest is more than a few headings — a
/// quarter of the lines or a tenth of the characters: a short letter over the
/// terms of business in six points is still a letter, while terms of business
/// set in seven points with nine-point headings are body text with headings.
fn body_text_height(
    lines: &[Line],
    tables: &[crate::table::Found],
    pixels_per_point: f32,
) -> Option<f32> {
    let outside: Vec<&Line> = lines
        .iter()
        .enumerate()
        .filter(|(index, _)| !tables.iter().any(|t| (t.start..t.end).contains(index)))
        .map(|(_, line)| line)
        .filter(|line| !line.text.trim().is_empty())
        .collect();
    let outside_chars: usize = outside.iter().map(|l| l.text.chars().count()).sum();
    let mut chosen: Vec<&Line> = if outside.len() <= 1 && outside_chars <= TITLE_CHARS {
        lines.iter().filter(|l| !l.text.trim().is_empty()).collect()
    } else {
        outside
    };
    let size = |line: &Line| line.quad.edge_height().max(1.0);
    let chars = |line: &Line| line.text.chars().count();
    if pixels_per_point > 0.0 {
        let smallest = SMALL_PRINT_POINTS * LINE_OVER_SIZE * pixels_per_point;
        let larger: Vec<&&Line> = chosen.iter().filter(|l| size(l) >= smallest).collect();
        let larger_chars: usize = larger.iter().map(|l| chars(l)).sum();
        let total_chars: usize = chosen.iter().map(|l| chars(l)).sum();
        if !larger.is_empty()
            && (larger.len() * 4 >= chosen.len() || larger_chars * 10 >= total_chars)
        {
            chosen.retain(|l| size(l) >= smallest);
        }
    }
    // A listing is set in a monospaced font, as a rule smaller than the
    // text; however many lines it has, the text around it is the body. A
    // size is the listing's when most of its lines that can be told are
    // monospaced — a line of a word or two cannot, and goes with its size.
    let same_size = |a: f32, b: f32| a.max(b) <= a.min(b) * 1.05;
    let listing: Vec<f32> = chosen
        .iter()
        .filter(|l| monospace_verdict(l) == Some(true))
        .map(|l| size(l))
        .filter(|&height| {
            let verdicts: Vec<bool> = chosen
                .iter()
                .filter(|l| same_size(size(l), height))
                .filter_map(|l| monospace_verdict(l))
                .collect();
            let mono = verdicts.iter().filter(|&&m| m).count();
            mono >= 3 && mono * 4 >= verdicts.len() * 3
        })
        .collect();
    let in_listing = |l: &Line| listing.iter().any(|&height| same_size(size(l), height));
    if chosen.iter().any(|l| !in_listing(l)) {
        chosen.retain(|l| !in_listing(l));
    }
    if chosen.is_empty() {
        return None;
    }
    let mut sizes: Vec<f32> = chosen.iter().map(|l| size(l)).collect();
    sizes.sort_by(|a, b| cmp_f32(*a, *b));
    let median = sizes[(sizes.len() - 1) / 2];
    // Sizes a twentieth apart are one size.
    let same = |a: f32, b: f32| a.max(b) <= a.min(b) * 1.05;
    let total: usize = chosen.iter().map(|l| chars(l)).sum();
    let paragraph = |height: f32| {
        chosen.windows(2).any(|pair| {
            let (a, b) = (pair[0], pair[1]);
            same(size(a), height)
                && same(size(b), height)
                && b.bbox.y0 - a.bbox.y1 < a.bbox.height()
                && b.bbox.horizontal_overlap(&a.bbox) > 0.0
        })
    };
    let body = sizes
        .iter()
        .copied()
        .filter(|&height| height > median)
        .filter(|&height| {
            let set: Vec<&&Line> = chosen.iter().filter(|l| same(size(l), height)).collect();
            let share: usize = set.iter().map(|l| chars(l)).sum();
            set.len() >= BODY_LINES
                && share as f32 >= BODY_SHARE * total as f32
                && paragraph(height)
        })
        .fold(median, f32::max);
    Some(body)
}

fn median_height(lines: &[Line]) -> f32 {
    if lines.is_empty() {
        return 1.0;
    }
    let mut hs: Vec<f32> = lines.iter().map(|l| l.bbox.height().max(1.0)).collect();
    hs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    hs[hs.len() / 2]
}

/// Orders lines for reading using a recursive XY-cut.
///
/// The classic algorithm: find the widest whitespace band that cuts all boxes,
/// split there, recurse. Horizontal cuts win over vertical ones so that a
/// page-wide headline above two columns still comes first.
pub fn reading_order(lines: Vec<Line>, cfg: &LayoutConfig) -> Vec<Line> {
    order(lines, cfg, false)
}

/// [`reading_order`] for lines read from a PDF's own text, handed over in the
/// order the PDF draws them.
///
/// Such a page's lines are never split at a word space, so a white band
/// through them is a gutter however narrow — a browser sets two columns an em
/// apart, less than any gutter a recognized page could be trusted with. What
/// tells a page's gutter from a table's is the drawing order: a page draws one
/// column and then the next, a table draws row by row, crossing its gutters
/// on every row.
pub(crate) fn reading_order_exact(lines: Vec<Line>, cfg: &LayoutConfig) -> Vec<Line> {
    order(lines, cfg, true)
}

/// The narrowest gutter, in line heights, on a page read from its own text:
/// half a line, which is six tenths of an em.
const EXACT_GUTTER_FACTOR: f32 = 0.5;
/// And the narrowest as a share of the width being divided: a hundredth, a
/// floor for pages of very small print only.
const EXACT_GUTTER_FRACTION: f32 = 0.01;

fn order(lines: Vec<Line>, cfg: &LayoutConfig, exact: bool) -> Vec<Line> {
    if lines.len() < 2 {
        return lines;
    }
    // Lines set at another angle than the page's — a stamp up the margin, a
    // watermark across it — come last. Their boxes span every line they
    // cross, and would join them all into one.
    let mut angles: Vec<f32> = lines.iter().map(|l| l.angle).collect();
    angles.sort_by(|a, b| cmp_f32(*a, *b));
    let page_angle = angles[angles.len() / 2];
    let (lines, turned): (Vec<Line>, Vec<Line>) = lines
        .into_iter()
        .partition(|l| (l.angle - page_angle).abs() <= TURNED_DEGREES);
    if lines.is_empty() {
        return turned;
    }
    let lines = if exact {
        attach_drop_caps(lines)
    } else {
        lines
    };
    let scale = median_height(&lines);
    // Whether the page has a repeating column structure is a property of the
    // whole page, so it is decided here: after the first horizontal cut a single
    // row no longer looks like a table.
    let max_merge_gap =
        if cfg.merge_baselines && has_repeating_columns(&lines, scale, cfg.baseline_overlap) {
            f32::MAX
        } else {
            scale * cfg.max_merge_gap_factor
        };
    let mut out = Vec::with_capacity(lines.len() + turned.len());
    let columns = PageColumns::of(&lines, scale, cfg.baseline_overlap);
    let sheet = Sheet {
        scale,
        width: horizontal_span(&lines),
        exact,
        columns: &columns,
    };
    xy_cut(lines, sheet, cfg, max_merge_gap, 0, &mut out);
    out.extend(turned);
    out
}

/// Whether a line is set in a monospaced font, as code is: from one word to
/// the next, its words advance by the same width for each character of the
/// text between them. The first word is left out: the others' boxes begin
/// halfway across the space before them, its box where its text does.
/// `None` for a line of too few words to tell, or one mostly in Chinese or
/// Japanese, whose characters are all one width whatever the font.
fn monospace_verdict(line: &Line) -> Option<bool> {
    let letters = line.text.chars().filter(|c| !c.is_whitespace()).count();
    if line.text.chars().filter(|&c| unspaced(c)).count() * 2 >= letters {
        return None;
    }
    let mut offsets = Vec::with_capacity(line.words.len());
    let mut from = 0usize;
    for word in &line.words {
        let at = line.text[from..].find(word.text.as_str())?;
        offsets.push((line.text[..from + at].chars().count(), word.bbox.x0));
        from += at + word.text.len();
    }
    let steps: Vec<f32> = offsets
        .windows(2)
        .skip(1)
        .filter(|pair| pair[1].0 > pair[0].0)
        .map(|pair| (pair[1].1 - pair[0].1) / (pair[1].0 - pair[0].0) as f32)
        .collect();
    let (least, most) = steps
        .iter()
        .fold((f32::MAX, f32::MIN), |(lo, hi), &w| (lo.min(w), hi.max(w)));
    (steps.len() >= 2).then_some(least > 0.0 && most <= least * MONOSPACED_SPREAD)
}

/// How far a monospaced line's advance per character may spread: rounding.
const MONOSPACED_SPREAD: f32 = 1.03;

/// How far from the page's own angle, in degrees, a line is set at another.
const TURNED_DEGREES: f32 = 5.0;
/// How many of the text's lines tall a single letter is, at least, to be a
/// paragraph's drop cap.
const DROP_CAP_LINES: f32 = 2.0;

/// Puts each drop cap back at the start of its paragraph's first line.
///
/// A paragraph's initial set two or three lines tall (a magazine's, a
/// chapter's, Word's Drop Cap) shares a baseline with every line beside it,
/// and read as a line of its own it joins whichever of them sits lowest. It
/// is a single letter, clearly taller than the text, and a line starts just
/// to its right, level with its top, with the rest of the letter's word —
/// in lower case, or in small capitals: the letter is that line's first. A
/// glossary's section letter beside its first entry (`A` / `Abschreibung`)
/// is a heading of its own.
fn attach_drop_caps(mut lines: Vec<Line>) -> Vec<Line> {
    /// Whether `text` starts with the rest of a word: in lower case, or a
    /// word set wholly in capitals.
    fn goes_on_a_word(text: &str) -> bool {
        let word: String = text
            .trim_start()
            .chars()
            .take_while(|c| c.is_alphabetic())
            .collect();
        word.chars().next().is_some_and(char::is_lowercase)
            || (word.chars().count() >= 2 && word.chars().all(char::is_uppercase))
    }

    let scale = median_height(&lines);
    let mut i = 0;
    while i < lines.len() {
        let cap = &lines[i];
        let letter = cap.text.trim();
        let is_cap = letter.chars().count() == 1
            && letter.chars().all(char::is_alphabetic)
            && cap.bbox.height() >= scale * DROP_CAP_LINES;
        let first = is_cap
            .then(|| {
                lines
                    .iter()
                    .enumerate()
                    .filter(|(j, line)| {
                        *j != i
                            && line.bbox.height() < cap.bbox.height() / DROP_CAP_LINES
                            && line.bbox.x0 >= cap.bbox.x1 - scale * 0.2
                            && line.bbox.x0 - cap.bbox.x1 <= scale * 1.5
                            && line.bbox.center_y() > cap.bbox.y0
                            && line.bbox.center_y() < cap.bbox.y1
                    })
                    .min_by(|a, b| cmp_f32(a.1.bbox.y0, b.1.bbox.y0))
                    .map(|(j, _)| j)
            })
            .flatten()
            .filter(|&j| lines[j].bbox.y0 - cap.bbox.y0 <= scale)
            .filter(|&j| goes_on_a_word(&lines[j].text));
        let Some(j) = first else {
            i += 1;
            continue;
        };
        let cap = lines.remove(i);
        let j = if j > i { j - 1 } else { j };
        let line = &mut lines[j];
        let letter = cap.text.trim().to_string();
        line.text = format!("{letter}{}", line.text.trim_start());
        if let Some(word) = line.words.first_mut() {
            word.text = format!("{letter}{}", word.text);
            word.bbox.x0 = cap.bbox.x0;
        }
        if let Some(segment) = line.segments.first_mut() {
            segment.text = format!("{letter}{}", segment.text.trim_start());
            segment.bbox.x0 = cap.bbox.x0;
        }
        line.bbox.x0 = cap.bbox.x0;
        line.quad = Quad::from_rect(line.bbox);
    }
    lines
}
/// How much smaller than a line across the gutter a line may be set and still
/// be a cell of its row.
const TWO_FLOWS_SIZE: f32 = 0.92;
/// How far off a line's baseline across the gutter, in line heights, a line
/// may sit and still be a cell of its row.
const TWO_FLOWS_DRIFT: f32 = 0.2;
/// [`TWO_FLOWS_DRIFT`] for a gutter narrower than the region's widest, which
/// only cuts the region where the two sides are plainly set on their own.
const TWO_FLOWS_DRIFT_NARROWER: f32 = 0.1;
/// How long a label before a colon may be: `Leistungszeitraum`, `Ihr Zeichen`.
const LABEL_CHARS: usize = 32;

/// Where a page's table columns begin and end: left edges, right edges and
/// middles that three rows share.
#[derive(Default)]
struct PageColumns {
    left: Vec<f32>,
    right: Vec<f32>,
    middle: Vec<f32>,
    tolerance: f32,
}

impl PageColumns {
    fn of(lines: &[Line], scale: f32, min_overlap: f32) -> Self {
        let bands = baseline_bands(lines, min_overlap);
        let tolerance = scale.max(1.0);
        let shared = |edge: fn(&Line) -> f32| -> Vec<f32> {
            let mut clusters: Vec<(f32, Vec<usize>)> = Vec::new();
            for (index, band) in bands.iter().enumerate() {
                if band.len() < 2 {
                    continue;
                }
                for line in band {
                    let x = edge(line);
                    match clusters
                        .iter_mut()
                        .find(|(c, _)| (*c - x).abs() <= tolerance)
                    {
                        Some((_, members)) if !members.contains(&index) => members.push(index),
                        Some(_) => {}
                        None => clusters.push((x, vec![index])),
                    }
                }
            }
            clusters
                .into_iter()
                .filter(|(_, members)| members.len() >= 3)
                .map(|(x, _)| x)
                .collect()
        };
        PageColumns {
            left: shared(|l| l.bbox.x0),
            right: shared(|l| l.bbox.x1),
            middle: shared(|l| l.bbox.center_x()),
            tolerance,
        }
    }

    /// Whether a box starts, ends or is centred where a column of the page's
    /// tables does.
    fn holds(&self, bbox: &Rect) -> bool {
        let near = |edges: &[f32], x: f32| edges.iter().any(|e| (e - x).abs() <= self.tolerance);
        near(&self.left, bbox.x0)
            || near(&self.right, bbox.x1)
            || near(&self.middle, bbox.center_x())
    }

    /// Whether every row of cells among `bands` sets its cells in the page's
    /// table columns — a row of that table, not an address with a date
    /// beside it or a browser's header.
    fn rows_of(&self, bands: &[Vec<&Line>]) -> bool {
        bands
            .iter()
            .filter(|band| band.len() >= 2)
            .all(|band| band.iter().all(|line| self.holds(&line.bbox)))
    }
}

/// Whether each line alone on its baseline belongs to the table: it starts
/// where one of the page's table columns does and heads a row of cells that
/// starts there too — a category over the rows, of which there are more than
/// one: a single row under a line is as likely an address with a date beside
/// it — or it stays within the column of a cell in a row of cells above it, the
/// next line of a cell that wraps or was broken by hand. A line reaching
/// across into the next column is the text under a table, not part of it.
fn wrapped_cells(bands: &[Vec<&Line>], columns: &PageColumns, scale: f32) -> bool {
    let rows: Vec<&Vec<&Line>> = bands.iter().filter(|band| band.len() >= 2).collect();
    !rows.is_empty()
        && bands.iter().filter(|band| band.len() == 1).all(|band| {
            let line = band[0];
            let top = |band: &Vec<&Line>| band.iter().map(|l| l.bbox.y0).fold(f32::MAX, f32::min);
            let under = bands
                .iter()
                .filter(|band| top(band) > line.bbox.center_y())
                .min_by(|a, b| top(a).total_cmp(&top(b)));
            let at_its_edge = |x: f32| (x - line.bbox.x0).abs() <= columns.tolerance;
            let starts_a_column = rows.len() >= 2
                && columns.left.iter().any(|&x| at_its_edge(x))
                && under.is_some_and(|row| {
                    row.len() >= 2 && row.iter().any(|cell| at_its_edge(cell.bbox.x0))
                })
                && rows.iter().all(|row| {
                    row.iter()
                        .all(|cell| line.bbox.x1 <= cell.bbox.x1 || cell.bbox.x0 <= line.bbox.x0)
                });
            starts_a_column
                || rows.iter().any(|row| {
                    row.iter().any(|cell| {
                        let next = row
                            .iter()
                            .map(|other| other.bbox.x0)
                            .filter(|&x0| x0 > cell.bbox.x1)
                            .reduce(f32::min);
                        cell.bbox.y1 <= line.bbox.y0 + scale
                            && line.bbox.x0 >= cell.bbox.x0 - scale
                            && line.bbox.x0 < cell.bbox.x1
                            && next.is_none_or(|next| line.bbox.x1 <= next)
                    })
                })
        })
}

/// What a region keeps of the page it was cut out of.
#[derive(Clone, Copy)]
struct Sheet<'a> {
    /// Median line height, the scale everything else is measured in.
    scale: f32,
    /// Width of the page's content, which is what a column corridor divides.
    width: f32,
    /// Whether the lines are a PDF's own text, in the order it drew them.
    exact: bool,
    /// Where the page's table columns are.
    columns: &'a PageColumns,
}

/// Whether reading `lines` in the order they came crosses the `splits` no
/// more often than columns are crossed: once per column, and a few times more
/// for a float, a footnote or a running head. A table's rows cross its gutters
/// every time.
fn read_in_turn(lines: &[Line], splits: &[f32]) -> bool {
    let crossings = lines
        .windows(2)
        .filter(|pair| column_of(splits, &pair[0]) != column_of(splits, &pair[1]))
        .count();
    crossings <= splits.len() + 2 + lines.len() / 10
}

fn xy_cut(
    lines: Vec<Line>,
    sheet: Sheet<'_>,
    cfg: &LayoutConfig,
    inherited_merge_gap: f32,
    depth: usize,
    out: &mut Vec<Line>,
) {
    let scale = sheet.scale;
    // Each region decides for itself whether it is tabular, as long as it still
    // has enough baselines to tell: a drawing's title block is a table even
    // though the sheet around it is not, and the dimension labels outside it must
    // not be merged just because the block below them repeats.
    //
    // A region too small to judge, or one that repeats no columns of its own,
    // still merges across its gutters on a page with tables — when its rows of
    // cells stand in the page's table columns: a row of that table, with a
    // cell that wraps onto lines of its own. Anything else keeps its boxes
    // apart: an address with the date beside it, a browser's page header.
    let limited = scale * cfg.max_merge_gap_factor;
    let bands = baseline_bands(&lines, cfg.baseline_overlap);
    let in_the_table = inherited_merge_gap == f32::MAX && sheet.columns.rows_of(&bands);
    let max_merge_gap = if !cfg.merge_baselines {
        inherited_merge_gap
    } else if bands.len() >= 3 {
        if has_repeating_columns(&lines, scale, cfg.baseline_overlap)
            || (in_the_table && wrapped_cells(&bands, sheet.columns, scale))
        {
            f32::MAX
        } else {
            limited
        }
    } else if inherited_merge_gap == f32::MAX && !in_the_table {
        limited
    } else {
        inherited_merge_gap
    };

    if lines.len() <= 1 || depth > 12 {
        emit_leaf(lines, cfg, max_merge_gap, out);
        return;
    }

    // Columns before rows: a page whose columns sit on one baseline grid has a
    // horizontal gap between every pair of lines, and cutting there first reads
    // the lines across the gutter.
    if cfg.detect_columns {
        let splits = column_corridors(&lines, sheet, cfg);
        if !splits.is_empty() {
            let mut columns: Vec<Vec<Line>> = vec![Vec::new(); splits.len() + 1];
            for line in lines {
                columns[column_of(&splits, &line)].push(line);
            }
            for column in columns {
                xy_cut(column, sheet, cfg, max_merge_gap, depth + 1, out);
            }
            return;
        }
    }

    // Horizontal band: a y gap that no box spans.
    if let Some((split, _)) = find_gap(&lines, scale * 0.6, |l| (l.bbox.y0, l.bbox.y1)) {
        let (top, bottom): (Vec<Line>, Vec<Line>) =
            lines.into_iter().partition(|l| l.bbox.center_y() < split);
        if !top.is_empty() && !bottom.is_empty() {
            xy_cut(top, sheet, cfg, max_merge_gap, depth + 1, out);
            xy_cut(bottom, sheet, cfg, max_merge_gap, depth + 1, out);
            return;
        }
        return_sorted(lines_from(top, bottom), cfg, max_merge_gap, out);
        return;
    }

    // Text across the full width over two columns or under them — a paper's
    // abstract, a newsletter's lead, a note across the page — set closer to
    // them than a horizontal cut needs: where the lines past the first or
    // last few leave a gutter those cross, the region is cut there.
    if sheet.exact && cfg.detect_columns {
        let content_width = horizontal_span(&lines);
        let min_gap = (scale * EXACT_GUTTER_FACTOR).max(content_width * EXACT_GUTTER_FRACTION);
        if let Some(split) = spanning_edge(&lines, scale, min_gap) {
            let (top, bottom): (Vec<Line>, Vec<Line>) =
                lines.into_iter().partition(|l| l.bbox.center_y() < split);
            xy_cut(top, sheet, cfg, max_merge_gap, depth + 1, out);
            xy_cut(bottom, sheet, cfg, max_merge_gap, depth + 1, out);
            return;
        }
    }

    // Vertical gutter: a column break.
    if cfg.detect_columns {
        let content_width = lines.iter().map(|l| l.bbox.x1).fold(f32::MIN, f32::max)
            - lines.iter().map(|l| l.bbox.x0).fold(f32::MAX, f32::min);
        let (factor, fraction) = if sheet.exact {
            (EXACT_GUTTER_FACTOR, EXACT_GUTTER_FRACTION)
        } else {
            (cfg.column_gap_factor, cfg.column_gap_min_fraction)
        };
        let min_gap = (scale * factor).max(content_width * fraction);
        // The widest gutter first; on a page read from its own text, where a
        // fact box's label and value columns may leave a wider gutter than
        // the one between the box and the text beside it, the others in turn
        // — each only where the lines either side are set on their own.
        let mut gaps = find_gaps(&lines, min_gap, |l| (l.bbox.x0, l.bbox.x1));
        if !sheet.exact {
            gaps.truncate(1);
        }
        for (nth, &(split, gap)) in gaps.iter().enumerate() {
            let in_turn = !sheet.exact || read_in_turn(&lines, &[split]);
            let (left, right): (Vec<Line>, Vec<Line>) = lines
                .iter()
                .cloned()
                .partition(|l| l.bbox.center_x() < split);
            // A letter's reference block, each line with its own label, is
            // no column of values whose labels stand beside it; nor is a
            // list beside a paragraph a column of a table — a paragraph, its
            // lines filled; numbered steps beside what each means are rows.
            let list_beside_paragraph =
                (listed(&left) && filled(&right)) || (listed(&right) && filled(&left));
            let self_labelled =
                sheet.exact && (labelled(&left) != labelled(&right) || list_beside_paragraph);
            let in_turn = in_turn
                && !(sheet.exact
                    && !self_labelled
                    && labels_and_values(
                        &[left.iter().collect(), right.iter().collect()],
                        scale,
                        cfg,
                    ));
            let straddled = shared_baseline_share(&left, &right, cfg.baseline_overlap);
            // A column is a block of lines. One baseline on a side — a cell or
            // two of a row, a page number, a stray label — is never a column.
            let thin_side = baseline_bands(&left, cfg.baseline_overlap).len() < 2
                || baseline_bands(&right, cfg.baseline_overlap).len() < 2;
            let narrowest = horizontal_span(&left).min(horizontal_span(&right));
            let region_width = horizontal_span(&left).max(0.0) + gap + horizontal_span(&right);
            let too_narrow = narrowest < gap * cfg.column_min_width_over_gap
                || narrowest < region_width * cfg.column_min_width_fraction;
            // A column's lines are one box each. Several boxes on a baseline are
            // the cells of a row, and a gutter between them runs through the
            // rows however wide the sides are.
            let left_boxes = boxes_per_band(&left, cfg.baseline_overlap);
            let right_boxes = boxes_per_band(&right, cfg.baseline_overlap);
            let crowded = left_boxes.max(right_boxes) > cfg.column_max_boxes_per_band;
            log::debug!(
                "column split at {split:.0} (gap {gap:.0}): left {} lines / {:.0} wide, \
                 right {} lines / {:.0} wide, straddled {straddled:.2}, \
                 thin {thin_side}, too_narrow {too_narrow}, crowded {crowded}, \
                 boxes/band L {left_boxes:.2} R {right_boxes:.2}",
                left.len(),
                horizontal_span(&left),
                right.len(),
                horizontal_span(&right),
            );
            // Text set beside a box — a fact box, a sidebar — shares its
            // baselines only by chance: the box is set smaller, or at a pitch
            // of its own, where a row's cells share size and baseline. And a
            // letter's reference block is no column of a table either. A
            // gutter past the widest cuts only where the two sides are set on
            // their own; that is told there from lines a tenth of a line off
            // each other's baselines already.
            let drift = if nth == 0 {
                TWO_FLOWS_DRIFT
            } else {
                TWO_FLOWS_DRIFT_NARROWER
            };
            let two_flows = self_labelled
                || (sheet.exact && two_flows(&left, &right, cfg.baseline_overlap, drift));
            if thin_side
                || !in_turn
                || (nth > 0 && !two_flows)
                || (straddled >= cfg.column_shared_baseline_veto
                    && (too_narrow || crowded)
                    && !two_flows)
            {
                // The gutter runs through rows, not between columns.
                continue;
            }
            if !left.is_empty() && !right.is_empty() {
                xy_cut(left, sheet, cfg, max_merge_gap, depth + 1, out);
                xy_cut(right, sheet, cfg, max_merge_gap, depth + 1, out);
                return;
            }
        }
    }

    emit_leaf(lines, cfg, max_merge_gap, out);
}

/// Where to cut lines that run across the full width off the columns under
/// or over them: the height between them, when the lines past the first (or
/// before the last) few leave a gutter at least `min_gap` wide with two
/// lines on either side, every one of those few lines crosses it, and they
/// end above (begin below) the rest.
fn spanning_edge(lines: &[Line], scale: f32, min_gap: f32) -> Option<f32> {
    let mut sorted: Vec<&Line> = lines.iter().collect();
    sorted.sort_by(|a, b| cmp_f32(a.bbox.y0, b.bbox.y0));
    let n = sorted.len();
    // The gutter of `rest` that the lines of `across` cross — each group of
    // them but its last line, which a paragraph may end short under the
    // others, not beside them — with two lines of `rest` at least on either
    // side.
    let gutter = |groups: &[&[&Line]], rest: &[&Line]| -> bool {
        let mut spans: Vec<(f32, f32)> = rest.iter().map(|l| (l.bbox.x0, l.bbox.x1)).collect();
        spans.sort_by(|a, b| cmp_f32(a.0, b.0));
        let Some(&(_, first_end)) = spans.first() else {
            return false;
        };
        let mut reach = first_end;
        for &(start, end) in &spans[1..] {
            let (from, to) = (reach, start);
            reach = reach.max(end);
            if to - from <= min_gap {
                continue;
            }
            let split = (from + to) / 2.0;
            let left = rest.iter().filter(|l| l.bbox.center_x() < split).count();
            let crosses = |l: &&Line| l.bbox.x0 < from && l.bbox.x1 > to;
            let across = groups.iter().all(|group| match group.split_last() {
                Some((last, before)) => {
                    let under = before
                        .iter()
                        .all(|l| last.bbox.y0 >= l.bbox.y1 - l.bbox.height() * 0.3);
                    before.iter().all(crosses) && (crosses(last) || (!before.is_empty() && under))
                }
                None => true,
            });
            if left >= 2 && rest.len() - left >= 2 && across {
                return true;
            }
        }
        false
    };
    let bottom_of = |ls: &[&Line]| ls.iter().map(|l| l.bbox.y1).fold(f32::MIN, f32::max);
    let top_of = |ls: &[&Line]| ls.iter().map(|l| l.bbox.y0).fold(f32::MAX, f32::min);
    // A few lines across the top and a few across the bottom, the columns
    // between them: either set may be empty, not both. The most lines across
    // the top first: a lead's short last line over the columns is the lead's.
    let most = SPANNING_LINES.min(n);
    for head in 0..=most {
        for tail in 0..=most {
            if head + tail == 0 || head + tail + 4 > n {
                continue;
            }
            let (top, rest, bottom) = (
                &sorted[..head],
                &sorted[head..n - tail],
                &sorted[n - tail..],
            );
            let clear_top = head == 0 || bottom_of(top) <= top_of(rest) + scale * 0.3;
            let clear_bottom = tail == 0 || top_of(bottom) >= bottom_of(rest) - scale * 0.3;
            if clear_top && clear_bottom && gutter(&[top, bottom], rest) {
                return Some(if head > 0 {
                    (bottom_of(top) + top_of(rest)) / 2.0
                } else {
                    (bottom_of(rest) + top_of(bottom)) / 2.0
                });
            }
        }
    }
    None
}

/// How many lines across the full width, above or below two columns, are
/// looked for at most: a lead of a dozen lines.
const SPANNING_LINES: usize = 12;

/// Emits one region of the page: boxes on a shared baseline become one line.
fn emit_leaf(mut lines: Vec<Line>, cfg: &LayoutConfig, max_merge_gap: f32, out: &mut Vec<Line>) {
    lines.sort_by(|a, b| cmp_f32(a.bbox.y0, b.bbox.y0).then(cmp_f32(a.bbox.x0, b.bbox.x0)));
    if cfg.merge_baselines {
        lines = merge_baselines(lines, cfg.baseline_overlap, max_merge_gap);
    }
    out.append(&mut lines);
}

/// True when boxes line up in the same columns across several baselines.
///
/// That is what a table, a receipt or a price list looks like, and it is what
/// tells them from a drawing whose labels merely share a height.
fn has_repeating_columns(lines: &[Line], scale: f32, min_overlap: f32) -> bool {
    if lines.len() < 4 {
        return false;
    }
    let bands = baseline_bands(lines, min_overlap);
    if bands.len() < 3 {
        return false;
    }
    // Cluster the left edges; a column is a cluster fed by at least three bands.
    let tolerance = scale.max(1.0);
    let mut columns: Vec<(f32, Vec<usize>)> = Vec::new();
    for (band_index, band) in bands.iter().enumerate() {
        for line in band {
            let x = line.bbox.x0;
            match columns
                .iter_mut()
                .find(|(centre, _)| (*centre - x).abs() <= tolerance)
            {
                Some((_, members)) => {
                    if !members.contains(&band_index) {
                        members.push(band_index);
                    }
                }
                None => columns.push((x, vec![band_index])),
            }
        }
    }
    columns.iter().filter(|(_, bands)| bands.len() >= 3).count() >= 2
}

/// Splits lines into groups that share a baseline, whatever order they come
/// in: a PDF drawn cell by cell sets a row's first cell, then that cell's
/// second line, then the row's other cells.
fn baseline_bands<'a>(
    lines: impl IntoIterator<Item = &'a Line>,
    min_overlap: f32,
) -> Vec<Vec<&'a Line>> {
    let mut bands: Vec<Vec<&Line>> = Vec::new();
    for line in lines {
        // The latest band first: lines mostly come in reading order.
        let joined = bands.iter().rposition(|band| {
            band.iter().any(|existing| {
                let overlap = existing.bbox.vertical_overlap(&line.bbox);
                let shorter = existing.bbox.height().min(line.bbox.height()).max(1.0);
                overlap / shorter >= min_overlap
            })
        });
        match joined {
            Some(at) => bands[at].push(line),
            None => bands.push(vec![line]),
        }
    }
    bands
}

/// Width covered by a group of lines.
fn horizontal_span<'a>(lines: impl IntoIterator<Item = &'a Line>) -> f32 {
    let (min, max) = lines.into_iter().fold((f32::MAX, f32::MIN), |(lo, hi), l| {
        (lo.min(l.bbox.x0), hi.max(l.bbox.x1))
    });
    (max - min).max(0.0)
}

/// Boxes per baseline: one for a paragraph's lines, more for a table's rows.
fn boxes_per_band<'a>(lines: impl IntoIterator<Item = &'a Line>, min_overlap: f32) -> f32 {
    let bands = baseline_bands(lines, min_overlap);
    let boxes: usize = bands.iter().map(Vec::len).sum();
    boxes as f32 / bands.len().max(1) as f32
}

/// The corridors between this region's columns, if it is laid out in columns.
///
/// Whitespace between columns is wide, runs the full height of the region and
/// has a column of text on either side. That last part is what a table does not
/// have: a table's rows put several boxes on a baseline, and its name column and
/// its figures are nothing like the same width.
fn column_corridors(lines: &[Line], sheet: Sheet<'_>, cfg: &LayoutConfig) -> Vec<f32> {
    if lines.len() < cfg.column_min_bands * 2 {
        return Vec::new();
    }
    let width = horizontal_span(lines);
    if width < sheet.width * cfg.column_corridor_page_share {
        return Vec::new();
    }
    let (factor, fraction) = if sheet.exact {
        (EXACT_GUTTER_FACTOR, EXACT_GUTTER_FRACTION)
    } else {
        (cfg.column_corridor_gap_factor, cfg.column_gap_min_fraction)
    };
    let min_gap = (sheet.scale * factor).max(width * fraction);
    let gaps = find_gaps(lines, min_gap, |l| (l.bbox.x0, l.bbox.x1));
    // The widest corridor first, then the two widest together, and so on: a page
    // set in three columns has two corridors, and neither of them alone leaves
    // columns of text on both sides of it.
    for count in 1..=gaps.len().min(cfg.column_max_corridors) {
        let mut splits: Vec<f32> = gaps[..count].iter().map(|(split, _)| *split).collect();
        splits.sort_by(|a, b| cmp_f32(*a, *b));
        let columns = slice_at(lines, &splits);
        let ok = columns.iter().all(|column| reads_as_column(column, cfg))
            && evenly_wide(&columns, cfg)
            && (!sheet.exact
                || (read_in_turn(lines, &splits)
                    && !labels_and_values(&columns, sheet.scale, cfg)));
        log::debug!(
            "corridors {:?} of {width:.0}: {:?} lines, columns {ok}",
            splits.iter().map(|s| *s as i32).collect::<Vec<_>>(),
            columns.iter().map(Vec::len).collect::<Vec<_>>(),
        );
        if ok {
            return splits;
        }
    }
    Vec::new()
}

/// Which column a line falls in, counting the corridors to its left.
fn column_of(splits: &[f32], line: &Line) -> usize {
    splits.partition_point(|split| *split <= line.bbox.center_x())
}

/// Cuts the lines at each corridor, left to right.
fn slice_at<'a>(lines: &'a [Line], splits: &[f32]) -> Vec<Vec<&'a Line>> {
    let mut columns = vec![Vec::new(); splits.len() + 1];
    for line in lines {
        columns[column_of(splits, line)].push(line);
    }
    columns
}

/// Whether a slice between two corridors reads as a column of text.
fn reads_as_column(column: &[&Line], cfg: &LayoutConfig) -> bool {
    let bands = baseline_bands(column.iter().copied(), cfg.baseline_overlap).len();
    bands >= cfg.column_min_bands
        && column.len() as f32 <= bands as f32 * cfg.column_max_boxes_per_band
}

/// Whether the slices are of a kind: a page's columns are near enough the same
/// width, a label and the value beside it are not.
fn evenly_wide(columns: &[Vec<&Line>], cfg: &LayoutConfig) -> bool {
    let spans: Vec<f32> = columns
        .iter()
        .map(|column| horizontal_span(column.iter().copied()))
        .collect();
    let widest = spans.iter().copied().fold(0.0, f32::max);
    spans
        .iter()
        .all(|span| *span >= widest * cfg.column_width_evenness)
}

/// How narrow, in line heights, a column of running text can be on a page
/// read from its own text: a newspaper's is twice as wide.
const EXACT_COLUMN_MIN_WIDTH: f32 = 8.0;

/// Whether two slices side by side on a page read from its own text are a
/// table's labels and values rather than two columns of text, whatever order
/// they were drawn in — a form's template is drawn first and its data after.
/// Their lines pair up on shared baselines, one for one, and one side is
/// too narrow for running text or is mostly labels ending in a colon.
fn labels_and_values(columns: &[Vec<&Line>], scale: f32, cfg: &LayoutConfig) -> bool {
    columns.windows(2).any(|pair| {
        let (left, right) = (&pair[0], &pair[1]);
        let veto = cfg.column_shared_baseline_veto;
        if shared_share(left, right, cfg.baseline_overlap) < veto
            || shared_share(right, left, cfg.baseline_overlap) < veto
        {
            return false;
        }
        let narrowest =
            horizontal_span(left.iter().copied()).min(horizontal_span(right.iter().copied()));
        let labels = left
            .iter()
            .filter(|line| line.text.trim_end().ends_with(':'))
            .count();
        narrowest < scale * EXACT_COLUMN_MIN_WIDTH || labels * 2 > left.len()
    })
}

/// Share of the left side's baselines that also carry a box on the right.
/// Whether the lines either side of a gutter are set independently of each
/// other: most of the lines that share a baseline with one across it differ
/// from it in size, or sit off its baseline by a fifth of a line. The cells
/// of a table's row are set in one size on one baseline; a paragraph beside
/// a fact box set at 88 % is not.
fn two_flows(left: &[Line], right: &[Line], min_overlap: f32, drift: f32) -> bool {
    let mut pairs = 0usize;
    let mut apart = 0usize;
    for l in left {
        let across = right
            .iter()
            .map(|r| (r, l.bbox.vertical_overlap(&r.bbox)))
            .filter(|(r, overlap)| {
                *overlap / l.bbox.height().min(r.bbox.height()).max(1.0) >= min_overlap
            })
            .max_by(|a, b| a.1.total_cmp(&b.1));
        let Some((r, _)) = across else { continue };
        let (short, tall) = (
            l.bbox.height().min(r.bbox.height()),
            l.bbox.height().max(r.bbox.height()).max(1.0),
        );
        pairs += 1;
        if short / tall < TWO_FLOWS_SIZE
            || (l.bbox.center_y() - r.bbox.center_y()).abs() > short * drift
        {
            apart += 1;
        }
    }
    pairs >= 2 && apart * 2 >= pairs
}

/// Whether most of `lines`, two at least, open with a bullet or a number:
/// `1. Konzeption (Q1)`, `• Server`.
fn listed(lines: &[Line]) -> bool {
    let opens_item = |text: &str| {
        let text = text.trim_start();
        let digits = text.chars().take_while(char::is_ascii_digit).count();
        let rest = &text[digits..];
        let numbered =
            (1..=3).contains(&digits) && (rest.starts_with(". ") || rest.starts_with(") "));
        numbered || text.starts_with(['•', '◦', '▪', '●', '○', '■', '–', '-', '*', '►', '✓'])
    };
    let count = lines.iter().filter(|l| opens_item(&l.text)).count();
    lines.len() >= 2 && count * 4 >= lines.len() * 3
}

/// Whether `lines` fill their width as a paragraph's do: each but the last
/// ends within a sixth of the widest's end. Items and values of their own
/// end where they end.
fn filled(lines: &[Line]) -> bool {
    let mut sorted: Vec<&Line> = lines.iter().collect();
    sorted.sort_by(|a, b| a.bbox.y0.total_cmp(&b.bbox.y0));
    let left = sorted.iter().map(|l| l.bbox.x0).fold(f32::MAX, f32::min);
    let right = sorted.iter().map(|l| l.bbox.x1).fold(f32::MIN, f32::max);
    let reach = (right - left) / 6.0;
    sorted.len() >= 2
        && sorted[..sorted.len() - 1]
            .iter()
            .all(|l| l.bbox.x1 >= right - reach)
}

/// Whether most of `lines`, two at least, carry their own label:
/// `Kundennummer: K-4711`, `Tel.: 030 1234`.
fn labelled(lines: &[Line]) -> bool {
    let count = lines.iter().filter(|l| carries_label(&l.text)).count();
    lines.len() >= 2 && count * 4 >= lines.len() * 3
}

/// Whether a line is a label and its value: a few words ending in a colon,
/// then the value after a space. A time (`10:30`) is not.
fn carries_label(text: &str) -> bool {
    let Some((label, value)) = text.trim().split_once(':') else {
        return false;
    };
    let label = label.trim();
    label.chars().next().is_some_and(char::is_alphabetic)
        && label.chars().count() <= LABEL_CHARS
        && label
            .chars()
            .all(|c| c.is_alphanumeric() || " .-/()".contains(c))
        && value.starts_with(' ')
        && !value.trim().is_empty()
}

fn shared_baseline_share(left: &[Line], right: &[Line], min_overlap: f32) -> f32 {
    let left: Vec<&Line> = left.iter().collect();
    let right: Vec<&Line> = right.iter().collect();
    shared_share(&left, &right, min_overlap)
}

fn shared_share(left: &[&Line], right: &[&Line], min_overlap: f32) -> f32 {
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    let shared = left
        .iter()
        .filter(|l| {
            right.iter().any(|r| {
                let overlap = l.bbox.vertical_overlap(&r.bbox);
                let shorter = l.bbox.height().min(r.bbox.height()).max(1.0);
                overlap / shorter >= min_overlap
            })
        })
        .count();
    shared as f32 / left.len() as f32
}

/// Groups boxes that share a baseline and joins each group into one line.
///
/// Groups are taken greedily from the top: a box joins the open group when it
/// overlaps it vertically by `min_overlap` of the shorter height. Within a group
/// the boxes are read left to right and joined with a single space.
fn merge_baselines(lines: Vec<Line>, min_overlap: f32, max_gap: f32) -> Vec<Line> {
    let mut out: Vec<Line> = Vec::with_capacity(lines.len());
    let mut group: Vec<Line> = Vec::new();

    for line in lines {
        // Two boxes one above the other are two lines, however tall a box
        // beside them is: a big step number or initial shares a band with
        // each of the lines set beside it, and must not make them one.
        let stacked = group.iter().any(|existing| {
            existing.bbox.horizontal_overlap(&line.bbox)
                > STACKED_OVERLAP * existing.bbox.width().min(line.bbox.width())
        });
        let shares_baseline = !stacked
            && group.iter().any(|existing| {
                let overlap = existing.bbox.vertical_overlap(&line.bbox);
                let shorter = existing.bbox.height().min(line.bbox.height()).max(1.0);
                overlap / shorter >= min_overlap
            })
            && group
                .iter()
                .map(|existing| {
                    (line.bbox.x0 - existing.bbox.x1).max(existing.bbox.x0 - line.bbox.x1)
                })
                .fold(f32::MAX, f32::min)
                <= max_gap;
        if shares_baseline {
            group.push(line);
        } else {
            if let Some(joined) = join_group(std::mem::take(&mut group)) {
                out.push(joined);
            }
            group.push(line);
        }
    }
    if let Some(joined) = join_group(group) {
        out.push(joined);
    }
    out
}

/// How much of the narrower box two boxes overlap by across, at most, to
/// sit side by side on one baseline rather than one above the other.
const STACKED_OVERLAP: f32 = 0.25;

/// Joins boxes of one baseline group into a single line, left to right.
fn join_group(mut group: Vec<Line>) -> Option<Line> {
    match group.len() {
        0 => return None,
        1 => return group.pop(),
        _ => {}
    }
    group.sort_by(|a, b| cmp_f32(a.bbox.x0, b.bbox.x0));

    let mut text = String::new();
    let mut bbox = group[0].bbox;
    let mut quad_points = group[0].quad.ordered().points;
    let mut confidence_sum = 0.0;
    let mut det_sum = 0.0;
    let mut margin_sum = 0.0;
    let mut words = Vec::new();
    let mut angle_sum = 0.0;
    // Where the boxes were, kept so a table row's columns survive the merge.
    let mut segments = Vec::with_capacity(group.len());

    for (index, part) in group.iter().enumerate() {
        if index > 0 && !text.ends_with(' ') && !part.text.starts_with(' ') {
            text.push(' ');
        }
        text.push_str(part.text.trim());
        bbox = bbox.union(&part.bbox);
        confidence_sum += part.confidence;
        det_sum += part.det_score;
        margin_sum += part.margin;
        angle_sum += part.angle;
        words.extend(part.words.iter().cloned());
        // A part that was itself merged contributes its own boxes, not a box
        // around them: two merges in a row must not lose the innermost cells.
        if part.segments.is_empty() {
            segments.push(crate::doc::Segment {
                text: part.text.trim().to_string(),
                bbox: part.bbox,
                confidence: part.confidence,
            });
        } else {
            segments.extend(part.segments.iter().cloned());
        }
    }
    let count = group.len() as f32;

    // The merged quad spans the group: keep the outermost corners so that a
    // rotated row still describes the ink it covers.
    let last = group[group.len() - 1].quad.ordered().points;
    quad_points[1] = last[1];
    quad_points[2] = last[2];

    Some(Line {
        text,
        confidence: confidence_sum / count,
        margin: margin_sum / count,
        quad: Quad::new(quad_points).ordered(),
        bbox,
        angle: angle_sum / count,
        det_score: det_sum / count,
        words,
        segments,
    })
}

fn lines_from(mut a: Vec<Line>, mut b: Vec<Line>) -> Vec<Line> {
    a.append(&mut b);
    a
}

fn return_sorted(lines: Vec<Line>, cfg: &LayoutConfig, max_merge_gap: f32, out: &mut Vec<Line>) {
    emit_leaf(lines, cfg, max_merge_gap, out);
}

/// Finds the centre of the widest interval that no box covers.
///
/// `extent` yields each box's `(start, end)` along the axis being cut.
fn find_gap(
    lines: &[Line],
    min_gap: f32,
    extent: impl Fn(&Line) -> (f32, f32),
) -> Option<(f32, f32)> {
    find_gaps(lines, min_gap, extent).into_iter().next()
}

/// Every whitespace band wider than `min_gap` that no box spans, widest first.
fn find_gaps(
    lines: &[Line],
    min_gap: f32,
    extent: impl Fn(&Line) -> (f32, f32),
) -> Vec<(f32, f32)> {
    let mut spans: Vec<(f32, f32)> = lines.iter().map(&extent).collect();
    if spans.is_empty() {
        return Vec::new();
    }
    spans.sort_by(|a, b| cmp_f32(a.0, b.0));

    let mut found: Vec<(f32, f32)> = Vec::new(); // (split position, gap size)
    let mut reach = spans[0].1;
    for &(start, end) in &spans[1..] {
        let gap = start - reach;
        if gap > min_gap {
            found.push((reach + gap * 0.5, gap));
        }
        reach = reach.max(end);
    }
    found.sort_by(|a, b| cmp_f32(b.1, a.1));
    found
}

fn cmp_f32(a: f32, b: f32) -> std::cmp::Ordering {
    a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
}

/// Groups ordered lines into blocks and classifies them.
pub fn group_blocks(lines: Vec<Line>, cfg: &LayoutConfig) -> Vec<Block> {
    group(lines, cfg, false, 0.0, &[])
}

/// [`group_blocks`] for lines read from a PDF's own text rather than
/// recognized: each word's box closes up to its neighbours', and a line's
/// cells are already cut into its segments, where no space glyph sat between
/// them. The gaps between those segments are the page's gutters, however
/// narrow — a browser sets a table's cells scarcely further apart than its
/// words — so they, not the word spaces, set the width a column needs.
///
/// `pixels_per_point` is the page's scale, when it is known (0 otherwise): it
/// says which print is small — terms of business, footnotes — and so never the
/// body text a heading stands out from. `rules` are the straight lines the
/// page draws, which say where a table's rows end.
pub(crate) fn group_exact_blocks(
    lines: Vec<Line>,
    cfg: &LayoutConfig,
    pixels_per_point: f32,
    rules: &[Rect],
) -> Vec<Block> {
    group(lines, cfg, true, pixels_per_point, rules)
}

fn group(
    lines: Vec<Line>,
    cfg: &LayoutConfig,
    exact: bool,
    pixels_per_point: f32,
    rules: &[Rect],
) -> Vec<Block> {
    if lines.is_empty() {
        return Vec::new();
    }
    let scale = median_height(&lines);
    let text_height = median_text_height(&lines);
    let leading = if exact { usual_leading(&lines) } else { None };

    // Tables are looked for over the whole page, not inside each block. The
    // blocks come from vertical gaps, and a ruled form's rows are spaced widely
    // enough that the gap rule cuts its table in two; a word space, meanwhile, is
    // a property of the page rather than of one block.
    let tables = if cfg.detect_tables {
        let narrowest_cell_gap = lines
            .iter()
            .flat_map(|line| line.segments.windows(2))
            .map(|pair| pair[1].bbox.x0 - pair[0].bbox.x1)
            .filter(|gap| *gap > 0.0)
            .reduce(f32::min);
        let gutter = match narrowest_cell_gap {
            // Half of it: two columns are two wherever white separates them,
            // and a column's own ragged edge never opens that wide a gap.
            Some(gap) if exact => (gap / 2.0).max(1.0),
            _ => crate::table::gutter_width(&lines, text_height),
        };
        crate::table::find(&lines, text_height, gutter, rules)
    } else {
        Vec::new()
    };

    let mut blocks: Vec<Block> = Vec::new();
    let mut lines = lines;
    // The detector promises runs in order that never overlap; a run that
    // breaks the promise is left as text rather than trusted with indices.
    let mut reach = 0usize;
    let tables: Vec<_> = tables
        .into_iter()
        .filter(|t| {
            let sound = t.start >= reach && t.start < t.end && t.end <= lines.len();
            if sound {
                reach = t.end;
            }
            sound
        })
        .collect();
    // Where sizes are exact a heading needs to stand out only a little, so the
    // text it stands out from has to be the body's, whatever the tables and
    // the small print around it.
    let text_height = if exact {
        body_text_height(&lines, &tables, pixels_per_point).unwrap_or(text_height)
    } else {
        text_height
    };
    // From the back, so the indices the detector reported still hold.
    let mut tail = lines.len();
    for found in tables.into_iter().rev() {
        let after = lines.split_off(found.end);
        let table_lines = lines.split_off(found.start);
        if !after.is_empty() {
            blocks.extend(paragraphs(after, scale, text_height, leading, exact, cfg));
        }
        blocks.push(Block {
            kind: BlockKind::Table,
            bbox: bounds_of(&table_lines),
            lines: table_lines,
            table: Some(found.table),
        });
        tail = found.start;
    }
    debug_assert!(lines.len() == tail);
    if !lines.is_empty() {
        blocks.extend(paragraphs(lines, scale, text_height, leading, exact, cfg));
    }
    blocks.reverse();
    blocks
}

/// Extra space between two lines, in line heights, that ends a paragraph on
/// a page read from its own text: half a line less a little. A word processor
/// sets six to eight points after an eleven-point paragraph, a browser a
/// whole line; the lines inside one differ from each other by nothing.
const EXACT_PARAGRAPH_SPACE: f32 = 0.4;
/// How far apart two lines' boxes may be in height, as a share of the taller,
/// and still count as one size when a page's usual leading is measured.
const EXACT_SIZE_STEP: f32 = 0.2;

/// How far apart, in line heights, the lines of a paragraph usually sit on a
/// page read from its own text, or `None` with too few lines to tell.
///
/// The quarter point of the gaps between neighbouring lines of one size in
/// one column: most such pairs are two lines of one paragraph, and the
/// quarter point stays among them on a page of short paragraphs too.
fn usual_leading(lines: &[Line]) -> Option<f32> {
    let mut gaps: Vec<f32> = lines
        .windows(2)
        .filter_map(|pair| {
            let (a, b) = (&pair[0].bbox, &pair[1].bbox);
            let (ha, hb) = (a.height(), b.height());
            let height = ha.min(hb).max(1.0);
            let same_size = (ha - hb).abs() <= EXACT_SIZE_STEP * ha.max(hb);
            let shares_column = b.horizontal_overlap(a) > 0.25 * a.width().min(b.width());
            let gap = (b.y0 - a.y1) / height;
            (same_size && shares_column && gap > -0.5 && gap < 1.5).then_some(gap)
        })
        .collect();
    if gaps.len() < 3 {
        return None;
    }
    gaps.sort_by(|a, b| cmp_f32(*a, *b));
    Some(gaps[(gaps.len() - 1) / 4])
}

/// Groups lines into paragraphs, headings and list items by their vertical gaps.
///
/// `leading` is [`usual_leading`] on a page read from its own text: there a
/// paragraph ends where the lines open up by more than their usual spacing,
/// or change size, whatever the gap measures in absolute terms.
fn paragraphs(
    lines: Vec<Line>,
    scale: f32,
    text_height: f32,
    leading: Option<f32>,
    exact: bool,
    cfg: &LayoutConfig,
) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    let mut current: Vec<Line> = Vec::new();

    for line in lines {
        let starts_new = match current.last() {
            None => false,
            Some(prev) => {
                let gap = line.bbox.y0 - prev.bbox.y1;
                let shares_column = line.bbox.horizontal_overlap(&prev.bbox)
                    > 0.25 * prev.bbox.width().min(line.bbox.width());
                let set_apart = leading.is_some_and(|leading| {
                    let height = prev.bbox.height().min(line.bbox.height()).max(1.0);
                    gap / height - leading > EXACT_PARAGRAPH_SPACE
                });
                // Where sizes are exact, a change of size is a new block however
                // few lines the page has: a heading set right above its text.
                let resized = exact && {
                    let apart = |a: f32, b: f32| a.max(b) >= EXACT_SIZE_CHANGE * a.min(b).max(1.0);
                    let (s0, s1) = (text_size(prev), text_size(&line));
                    let (l0, l1) = (largest_size(prev), largest_size(&line));
                    apart(s0, s1) && apart(l0, l1)
                };
                // A bullet opens an item, however close it follows: a list set
                // right under its lead-in is still a list.
                let bullet = strip_bullet(&line.text).len() < line.text.trim_start().len();
                gap > scale * cfg.line_gap_factor
                    || !shares_column
                    || set_apart
                    || resized
                    || bullet
            }
        };
        if starts_new {
            blocks.push(finish_block(
                std::mem::take(&mut current),
                text_height,
                exact,
                cfg,
            ));
        }
        current.push(line);
    }
    if !current.is_empty() {
        blocks.push(finish_block(current, text_height, exact, cfg));
    }
    // Emitted back to front by the caller, which reverses the whole list.
    blocks.reverse();
    blocks
}

/// How much larger, as a factor, one line's text has to be than the next
/// one's to be set apart from it where sizes are exact: less than a heading
/// has to stand out ([`EXACT_HEADING_FACTOR`]), so every heading is a block.
const EXACT_SIZE_CHANGE: f32 = 1.15;

/// The size of a line's largest words: a line set mostly in code, in a
/// smaller font, still has its words of text beside it.
fn largest_size(line: &Line) -> f32 {
    line.words
        .iter()
        .map(|w| w.bbox.height())
        .reduce(f32::max)
        .unwrap_or_else(|| line.bbox.height())
}

/// The size a line's text is set in: its middle word's height, which a
/// superscript on one word does not change.
fn text_size(line: &Line) -> f32 {
    let mut heights: Vec<f32> = line.words.iter().map(|w| w.bbox.height()).collect();
    if heights.is_empty() {
        return line.bbox.height();
    }
    heights.sort_by(|a, b| cmp_f32(*a, *b));
    heights[heights.len() / 2]
}

fn finish_block(mut lines: Vec<Line>, text_height: f32, exact: bool, cfg: &LayoutConfig) -> Block {
    if cfg.dehyphenate {
        dehyphenate(&mut lines);
    }
    plain_block(lines, text_height, exact, cfg)
}

fn plain_block(lines: Vec<Line>, text_height: f32, exact: bool, cfg: &LayoutConfig) -> Block {
    Block {
        kind: classify_block(&lines, text_height, exact, cfg),
        bbox: bounds_of(&lines),
        lines,
        table: None,
    }
}

fn bounds_of(lines: &[Line]) -> Rect {
    lines
        .iter()
        .map(|l| l.bbox)
        .reduce(|a, b| a.union(&b))
        .unwrap_or(Rect::new(0.0, 0.0, 0.0, 0.0))
}

/// Characters a list item may start with.
///
/// Shared with the Markdown exporter so that the classifier and the renderer can
/// never disagree about what a bullet is.
pub(crate) const BULLETS: [char; 6] = ['•', '‣', '·', '–', '—', '*'];

/// Strips one leading bullet and the space behind it.
///
/// A hyphen only counts as a bullet when a space follows: `-19,90` is a number,
/// and stripping its sign would turn a credit into a charge.
pub(crate) fn strip_bullet(line: &str) -> &str {
    let trimmed = line.trim_start();
    let rest = match trimmed.chars().next() {
        Some(c) if BULLETS.contains(&c) => &trimmed[c.len_utf8()..],
        Some('-') if trimmed[1..].starts_with(' ') => &trimmed[1..],
        _ => return trimmed,
    };
    rest.trim_start()
}

/// Lines a heading may run to on a page read from its own text, where sizes
/// are exact: a title set large in a narrow column wraps more than twice.
const EXACT_HEADING_LINES: usize = 4;
/// How much larger than the text a heading's size has to be where sizes are
/// exact: a sixteen-point heading over twelve-point text is one, though its
/// recognized box would not stand out enough to be sure.
const EXACT_HEADING_FACTOR: f32 = 1.2;

fn classify_block(lines: &[Line], text_height: f32, exact: bool, cfg: &LayoutConfig) -> BlockKind {
    let first = match lines.first() {
        Some(l) => l,
        None => return BlockKind::Paragraph,
    };
    // The quad, not the box: a de-hyphenated line's box spans both of the lines
    // it came from, which would make every short hyphenated paragraph a heading.
    let factor = if exact {
        EXACT_HEADING_FACTOR
    } else {
        cfg.heading_height_factor
    };
    let tall = first.quad.edge_height() > text_height * factor;
    let most_lines = if exact { EXACT_HEADING_LINES } else { 2 };
    let chars: usize = lines.iter().map(|l| l.text.chars().count()).sum();
    let heading =
        tall && lines.len() <= most_lines && first.text.chars().count() <= 120 && chars <= 240;
    let trimmed = first.text.trim_start();
    // `3. Punkt` and `3) Punkt`; not `12.03.2026 fand …` or `3.5 Tonnen …`,
    // whose number runs on past the dot.
    let numbered = trimmed.split_once(['.', ')']).is_some_and(|(head, rest)| {
        !head.is_empty()
            && head.len() <= 3
            && head.chars().all(|c| c.is_ascii_digit())
            && (rest.is_empty() || rest.starts_with(char::is_whitespace))
    });
    // A number in front of a large line numbers a chapter, not a list item.
    if trimmed.starts_with(BULLETS) || (numbered && !heading) {
        return BlockKind::ListItem;
    }
    if heading {
        return BlockKind::Heading;
    }
    BlockKind::Paragraph
}

/// Words that follow a suspended hyphen: "Vor- und Nachname", "Ein- oder
/// Ausgang", "pre- and post-processing". The abbreviations count with their
/// dot only. Words that also end ordinary words ("to" of "Konto", "or" of
/// "color", "als" of "materials") are left out: there a hyphen at the line
/// end is a broken word far more often.
const AFTER_SUSPENDED_HYPHEN: &[(&str, bool)] = &[
    ("und", false),
    ("oder", false),
    ("sowie", false),
    ("bis", false),
    ("noch", false),
    ("wie", false),
    ("respektive", false),
    ("and", false),
    ("bzw", true),
    ("resp", true),
    ("u", true),
    ("o", true),
];

/// Whether a line that ends in a hyphen goes on with a conjunction and the
/// second half of a pair — "und Nachname" — rather than the rest of a word.
pub(crate) fn continues_a_suspended_hyphen(next: &str) -> bool {
    let word: String = next.chars().take_while(|c| c.is_alphabetic()).collect();
    let Some(&(_, dotted)) = AFTER_SUSPENDED_HYPHEN.iter().find(|(w, _)| *w == word) else {
        return false;
    };
    let mut rest = &next[word.len()..];
    if dotted {
        match rest.strip_prefix('.') {
            Some(after) => rest = after,
            None => return false,
        }
    }
    // Another word or number follows, or nothing: "und Nachname", "bis 12-",
    // "und" at the line end. Punctuation after it ("to,") is a broken word.
    rest.trim().is_empty()
        || (rest.starts_with(char::is_whitespace)
            && rest
                .trim_start()
                .chars()
                .next()
                .is_some_and(char::is_alphanumeric))
}

/// Joins words broken by a hyphen at a line end.
fn dehyphenate(lines: &mut Vec<Line>) {
    let mut i = 0;
    while i + 1 < lines.len() {
        let head = lines[i].text.trim_end();
        // A hyphen after a digit never breaks a word: "2- und 3-Zimmer",
        // "3-" / "fach" — the hyphen is part of what was written.
        let ends_hyphen = head.ends_with(['-', '\u{2010}', '\u{00ad}'])
            && !head.chars().rev().nth(1).is_some_and(char::is_numeric);
        let next = lines[i + 1].text.trim_start();
        let next_starts_lower = next.chars().next().is_some_and(|c| c.is_lowercase());
        // "E-Mail-" over "Adresse", "IT-" over "basierten": the hyphen is the
        // compound's own, and the word goes on without a space.
        let compound = head.ends_with('-')
            && head.chars().rev().nth(1).is_some_and(char::is_alphabetic)
            && (next.chars().next().is_some_and(char::is_uppercase)
                || (abbreviation_before_hyphen(head)
                    && next.chars().next().is_some_and(char::is_alphabetic)
                    && !continues_a_suspended_hyphen(next)));
        if compound {
            let tail = lines.remove(i + 1);
            let head = &mut lines[i];
            head.text = format!("{}{}", head.text.trim_end(), tail.text.trim_start());
            head.bbox = head.bbox.union(&tail.bbox);
            head.confidence = (head.confidence + tail.confidence) * 0.5;
            head.words.extend(tail.words);
            continue;
        }
        if ends_hyphen && next_starts_lower && !continues_a_suspended_hyphen(next) {
            let tail = lines.remove(i + 1);
            let head = &mut lines[i];
            let stem = head.text.trim_end();
            let stem = &stem[..stem.len() - stem.chars().last().map_or(0, char::len_utf8)];
            head.text = format!("{}{}", stem, tail.text.trim_start());
            head.bbox = head.bbox.union(&tail.bbox);
            head.confidence = (head.confidence + tail.confidence) * 0.5;
            head.words.extend(tail.words);
            continue; // the joined line may end in a hyphen again
        }
        i += 1;
    }
}

/// Whether the word a line ends in, before its hyphen, is an abbreviation in
/// capitals — `IT-`, `PDF-`, `GPU-`: hyphenation never breaks a word right
/// after a run of capitals, so the hyphen is the compound's own.
pub(crate) fn abbreviation_before_hyphen(text: &str) -> bool {
    let word = text
        .trim_end()
        .trim_end_matches(['-', '\u{2010}'])
        .rsplit(|c: char| c.is_whitespace() || c == '-' || c == '/')
        .next()
        .unwrap_or("");
    word.chars().filter(|c| c.is_alphabetic()).count() >= 2
        && word.chars().all(|c| c.is_uppercase() || c.is_ascii_digit())
}

/// Whether a character belongs to a script written without spaces between
/// its words: Chinese, Japanese, and their punctuation.
pub(crate) fn unspaced(c: char) -> bool {
    matches!(c,
        '\u{3000}'..='\u{303F}' | '\u{3040}'..='\u{30FF}' | '\u{3400}'..='\u{4DBF}'
        | '\u{4E00}'..='\u{9FFF}' | '\u{F900}'..='\u{FAFF}' | '\u{FF00}'..='\u{FFEF}'
        | '\u{20000}'..='\u{2FFFF}')
}

/// Turns CTC character positions into word boxes along a line quad.
///
/// Character positions are fractions of the crop the recognizer read, so they
/// are interpolated along the quad's own edges: a rotated line gets boxes that
/// follow it instead of one tall box per word, and a vertical line — whose crop
/// [`crop_stands_up`] turned upright — gets them stacked bottom to top.
pub fn words_from_chars(quad: &Quad, chars: &[CharSpan]) -> Vec<Word> {
    if chars.is_empty() {
        return Vec::new();
    }
    let q = quad.ordered();
    let stood_up = crop_stands_up(&q);

    /// One word under construction: its text, its confidence, and the stretch
    /// of the crop it covers.
    struct Pending {
        text: String,
        conf_sum: f32,
        count: u32,
        start: f32,
        end: f32,
    }

    let mut pending: Vec<Pending> = Vec::new();
    let mut open = false;
    for span in chars {
        if span.text.trim().is_empty() {
            open = false;
            continue;
        }
        if !open {
            pending.push(Pending {
                text: String::new(),
                conf_sum: 0.0,
                count: 0,
                start: f32::MAX,
                end: f32::MIN,
            });
            open = true;
        }
        if let Some(word) = pending.last_mut() {
            word.text.push_str(&span.text);
            word.conf_sum += span.confidence;
            word.count += 1;
            word.start = word.start.min(span.x_center - span.x_width * 0.5);
            word.end = word.end.max(span.x_center + span.x_width * 0.5);
        }
    }

    pending
        .into_iter()
        .filter(|word| !word.text.trim().is_empty())
        .map(|word| Word {
            bbox: span_box(&q, stood_up, word.start, word.end),
            confidence: if word.count == 0 {
                0.0
            } else {
                word.conf_sum / word.count as f32
            },
            text: word.text,
        })
        .collect()
}

/// Box around the slice of a line between two fractions of its reading
/// direction.
///
/// The slice is bounded by two cuts across the line, each interpolated between
/// the quad's two long edges, so the box tilts with the line.
fn span_box(quad: &Quad, stood_up: bool, start: f32, end: f32) -> Rect {
    let p = quad.points;
    // `[tl, tr, br, bl]`: reading runs along the top and bottom edges, or up the
    // left and right ones when the crop was stood upright.
    let (a0, a1, b0, b1) = if stood_up {
        (p[3], p[0], p[2], p[1])
    } else {
        (p[0], p[1], p[3], p[2])
    };
    let lerp =
        |a: Point, b: Point, t: f32| Point::new(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t);
    let (s, e) = (start.clamp(0.0, 1.0), end.clamp(0.0, 1.0));
    Quad::new([
        lerp(a0, a1, s),
        lerp(a0, a1, e),
        lerp(b0, b1, e),
        lerp(b0, b1, s),
    ])
    .bounds()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line_at(text: &str, x0: f32, y0: f32, x1: f32, y1: f32) -> Line {
        let r = Rect::new(x0, y0, x1, y1);
        Line {
            text: text.into(),
            confidence: 0.9,
            quad: Quad::from_rect(r),
            bbox: r,
            det_score: 0.9,
            ..Default::default()
        }
    }

    fn texts(lines: &[Line]) -> Vec<&str> {
        lines.iter().map(|l| l.text.as_str()).collect()
    }

    /// Two columns of ten lines each, an em apart, drawn one column after
    /// the other or row by row across both.
    fn narrow_columns(row_by_row: bool) -> Vec<Line> {
        let mut left = Vec::new();
        let mut right = Vec::new();
        for row in 0..10 {
            let y = row as f32 * 14.0;
            left.push(line_at(&format!("L{row}"), 0.0, y, 300.0, y + 12.0));
            right.push(line_at(&format!("R{row}"), 310.0, y, 610.0, y + 12.0));
        }
        if row_by_row {
            left.into_iter()
                .zip(right)
                .flat_map(|(l, r)| [l, r])
                .collect()
        } else {
            left.into_iter().chain(right).collect()
        }
    }

    #[test]
    fn a_bullet_opens_a_list_item_right_under_its_lead_in() {
        let lines = vec![
            line_at("Offene Punkte der Begehung:", 0.0, 0.0, 300.0, 12.0),
            line_at("• Brandschutz prüfen", 0.0, 14.0, 250.0, 26.0),
            line_at("• Abnahme terminieren", 0.0, 28.0, 250.0, 40.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        let kinds: Vec<BlockKind> = blocks.iter().map(|b| b.kind).collect();
        assert_eq!(
            kinds,
            [
                BlockKind::Paragraph,
                BlockKind::ListItem,
                BlockKind::ListItem
            ]
        );
    }

    #[test]
    fn a_page_read_from_its_text_finds_columns_an_em_apart() {
        let cfg = LayoutConfig::default();
        let ordered = reading_order_exact(narrow_columns(false), &cfg);
        let texts: Vec<&str> = ordered.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(texts[..3], ["L0", "L1", "L2"]);
        assert_eq!(texts[10..13], ["R0", "R1", "R2"]);
        // A recognized page cannot tell such a gutter from a word space.
        let recognized = reading_order(narrow_columns(false), &cfg);
        assert_eq!(recognized.len(), 10, "rows merged across the gutter");
    }

    #[test]
    fn a_table_drawn_row_by_row_is_not_two_columns() {
        let cfg = LayoutConfig::default();
        let ordered = reading_order_exact(narrow_columns(true), &cfg);
        // Each row is one line of two cells, in order.
        assert_eq!(ordered.len(), 10);
        assert_eq!(ordered[0].text, "L0 R0");
        assert_eq!(ordered[9].text, "L9 R9");
    }

    #[test]
    fn a_form_drawn_labels_first_keeps_each_value_beside_its_label() {
        let cfg = LayoutConfig::default();
        let pairs = [
            ("Rechnungsnummer:", "RE-2026-0042"),
            ("Rechnungsdatum:", "15.03.2026"),
            ("Kundennummer:", "K-10077"),
            ("Zahlungsziel:", "14 Tage netto"),
            ("Betrag:", "1.299,90 EUR"),
        ];
        // The template first, the data after: the order a form is filled in.
        let mut lines: Vec<Line> = Vec::new();
        for (row, (label, _)) in pairs.iter().enumerate() {
            let y = row as f32 * 16.0;
            lines.push(line_at(label, 72.0, y, 162.0, y + 13.0));
        }
        for (row, (_, value)) in pairs.iter().enumerate() {
            let y = row as f32 * 16.0;
            lines.push(line_at(value, 190.0, y, 260.0, y + 13.0));
        }
        let ordered = reading_order_exact(lines, &cfg);
        let text = texts(&ordered).join(" ");
        for (label, value) in pairs {
            assert!(text.contains(&format!("{label} {value}")), "{text}");
        }
    }

    #[test]
    fn the_body_size_is_neither_a_price_list_nor_the_small_print() {
        let mut lines = vec![
            line_at(
                "The committee met on Monday to review the budget",
                0.0,
                0.0,
                300.0,
                13.2,
            ),
            line_at(
                "for the coming year, and all members agreed.",
                0.0,
                16.0,
                280.0,
                29.2,
            ),
        ];
        for row in 0..8 {
            let y = 40.0 + row as f32 * 12.0;
            lines.push(line_at(&format!("Item {row}"), 0.0, y, 40.0, y + 10.8));
        }
        assert!((median_text_height(&lines) - 10.8).abs() < 0.01);
        let price_list = crate::table::Found {
            start: 2,
            end: 10,
            table: crate::doc::Table {
                rows: 8,
                columns: 1,
                cells: Vec::new(),
            },
        };
        assert!((body_text_height(&lines, &[price_list], 0.0).unwrap() - 13.2).abs() < 0.01);
        // A slide: a title over one line of text. The text is the body.
        let slide = vec![
            line_at("What comes next", 0.0, 0.0, 300.0, 28.8),
            line_at("We open two new offices.", 0.0, 40.0, 200.0, 53.2),
        ];
        assert!((body_text_height(&slide, &[], 1.0).unwrap() - 13.2).abs() < 0.01);
        // Terms of business in seven points with nine-point headings: the
        // small print is the body.
        let mut terms: Vec<Line> = Vec::new();
        for section in 0..4 {
            let y = section as f32 * 100.0;
            terms.push(line_at("§ 1 Geltung", 0.0, y, 80.0, y + 10.8));
            for row in 0..8 {
                let y = y + 14.0 + row as f32 * 10.0;
                terms.push(line_at(
                    &"Diese Bedingungen gelten ".repeat(3),
                    0.0,
                    y,
                    400.0,
                    y + 8.4,
                ));
            }
        }
        assert!((body_text_height(&terms, &[], 1.0).unwrap() - 8.4).abs() < 0.01);
        // A letter in eleven points over the terms of business in six: fewer
        // characters than the small print, and still the body.
        let mut letter: Vec<Line> = (0..7)
            .map(|row| {
                let y = row as f32 * 16.0;
                line_at(
                    "Die Ware wurde heute an Sie versandt.",
                    0.0,
                    y,
                    250.0,
                    y + 13.2,
                )
            })
            .collect();
        let small = "Es gelten unsere Allgemeinen Geschäftsbedingungen ".repeat(4);
        letter.extend((0..5).map(|row| {
            let y = 140.0 + row as f32 * 8.0;
            line_at(&small, 0.0, y, 500.0, y + 7.2)
        }));
        assert!((body_text_height(&letter, &[], 0.0).unwrap() - 13.2).abs() < 0.01);
        // A shorter letter over more small print: known to be six points at
        // this scale, the small print is not what the letter stands out from.
        let mut short: Vec<Line> = (0..4)
            .map(|row| {
                let y = row as f32 * 24.0;
                line_at("Die Ware wurde versandt.", 0.0, y, 200.0, y + 13.2)
            })
            .collect();
        short.extend((0..5).map(|row| {
            let y = 140.0 + row as f32 * 8.0;
            line_at(&small, 0.0, y, 500.0, y + 7.2)
        }));
        assert!((body_text_height(&short, &[], 1.0).unwrap() - 13.2).abs() < 0.01);
    }

    #[test]
    fn a_cell_that_wraps_stays_in_its_row() {
        let lines = vec![
            line_at("Aufgabe", 200.0, 230.0, 320.0, 263.0),
            line_at("Verantwortlich", 833.0, 230.0, 1030.0, 263.0),
            line_at("Frist", 1166.0, 230.0, 1220.0, 263.0),
            line_at("Server migrieren", 200.0, 291.0, 450.0, 325.0),
            line_at("Mueller", 833.0, 291.0, 924.0, 325.0),
            line_at("30.06.2026", 1166.0, 291.0, 1305.0, 325.0),
            line_at("Monitoring einrichten", 200.0, 352.0, 457.0, 386.0),
            line_at("Schmidt", 833.0, 352.0, 941.0, 386.0),
            line_at("15.07.2026", 1166.0, 352.0, 1305.0, 386.0),
            line_at("Abnahme", 200.0, 413.0, 318.0, 447.0),
            line_at("Kunde", 833.0, 413.0, 913.0, 447.0),
            line_at("und Partner", 833.0, 447.0, 990.0, 481.0),
            line_at("31.07.2026", 1166.0, 413.0, 1305.0, 447.0),
        ];
        let cfg = LayoutConfig::default();
        let blocks = group_exact_blocks(reading_order_exact(lines, &cfg), &cfg, 0.0, &[]);
        assert_eq!(blocks.len(), 1);
        let table = blocks[0].table.as_ref().expect("a table");
        assert_eq!(table.rows, 4);
        assert_eq!(
            table.row_text(3),
            ["Abnahme", "Kunde und Partner", "31.07.2026"]
        );
    }

    #[test]
    fn a_row_with_an_empty_cell_under_tight_rows_is_a_row_of_its_own() {
        // Single spacing: each line's box reaches into the next one's.
        let rows = [
            ("Bezeichnung", "Menge", "Preis"),
            ("Schrauben M4", "100", "4,90"),
            ("Muttern M4", "", "2,10"),
            ("Unterlegscheiben M4", "50", "1,20"),
            ("Kabelbinder", "200", "3,50"),
        ];
        let mut lines = Vec::new();
        for (row, (a, b, c)) in rows.iter().enumerate() {
            let y = row as f32 * 31.0;
            lines.push(line_at(
                a,
                200.0,
                y,
                200.0 + 14.0 * a.len() as f32,
                y + 33.0,
            ));
            if !b.is_empty() {
                lines.push(line_at(
                    b,
                    700.0,
                    y,
                    700.0 + 14.0 * b.len() as f32,
                    y + 33.0,
                ));
            }
            lines.push(line_at(
                c,
                1000.0,
                y,
                1000.0 + 14.0 * c.len() as f32,
                y + 33.0,
            ));
        }
        let cfg = LayoutConfig::default();
        let blocks = group_exact_blocks(reading_order_exact(lines, &cfg), &cfg, 0.0, &[]);
        let table = blocks
            .iter()
            .find_map(|b| b.table.as_ref())
            .expect("a table");
        assert_eq!(table.rows, 5);
        assert_eq!(table.row_text(2), ["Muttern M4", "", "2,10"]);
    }

    #[test]
    fn a_line_led_by_a_date_or_an_amount_is_not_a_list_item() {
        let cfg = LayoutConfig::default();
        for text in [
            "12.03.2026 fand die Abnahme statt.",
            "3.5 Tonnen wurden geliefert.",
            "1.299,90 EUR wurden überwiesen.",
        ] {
            let line = line_at(text, 0.0, 0.0, 300.0, 12.0);
            assert_eq!(
                classify_block(&[line], 12.0, true, &cfg),
                BlockKind::Paragraph
            );
        }
        let item = line_at("3. Abnahme", 0.0, 0.0, 300.0, 12.0);
        assert_eq!(
            classify_block(&[item], 12.0, true, &cfg),
            BlockKind::ListItem
        );
    }

    #[test]
    fn a_large_numbered_line_is_a_heading_not_a_list_item() {
        let cfg = LayoutConfig::default();
        let heading = line_at("1. Einleitung", 0.0, 0.0, 200.0, 30.0);
        let item = line_at("1. Antrag stellen", 0.0, 0.0, 200.0, 12.0);
        assert_eq!(
            classify_block(&[heading], 12.0, false, &cfg),
            BlockKind::Heading
        );
        assert_eq!(
            classify_block(&[item], 12.0, false, &cfg),
            BlockKind::ListItem
        );
        // Exact sizes allow a title wrapped over three lines in a narrow column.
        let title: Vec<Line> = (0..3)
            .map(|i| {
                line_at(
                    "Bericht Nord",
                    0.0,
                    i as f32 * 32.0,
                    200.0,
                    i as f32 * 32.0 + 30.0,
                )
            })
            .collect();
        assert_eq!(classify_block(&title, 12.0, true, &cfg), BlockKind::Heading);
        assert_eq!(
            classify_block(&title, 12.0, false, &cfg),
            BlockKind::Paragraph
        );
        // A sixteen-point section heading over twelve-point text: a heading
        // where sizes are exact, too close to call where they are recognized.
        let section = line_at("Ergebnisse", 0.0, 0.0, 200.0, 16.0);
        assert_eq!(
            classify_block(std::slice::from_ref(&section), 12.0, true, &cfg),
            BlockKind::Heading
        );
        assert_eq!(
            classify_block(&[section], 12.0, false, &cfg),
            BlockKind::Paragraph
        );
        // A line a point larger than the text is not.
        let larger = line_at("Ergebnisse", 0.0, 0.0, 200.0, 13.0);
        assert_eq!(
            classify_block(&[larger], 12.0, true, &cfg),
            BlockKind::Paragraph
        );
    }

    #[test]
    fn a_page_read_from_its_text_breaks_paragraphs_at_extra_space() {
        // Twelve-pixel lines at a fourteen-pixel pitch; seven pixels more
        // after the first paragraph — far less than a line, as a word
        // processor sets it — and a heading of a larger size set right
        // above the text it heads.
        let lines = vec![
            line_at("Erster Absatz, erste Zeile", 0.0, 0.0, 300.0, 12.0),
            line_at("und seine zweite Zeile", 0.0, 14.0, 280.0, 26.0),
            line_at("und seine dritte.", 0.0, 28.0, 200.0, 40.0),
            line_at("Zweiter Absatz, erste Zeile", 0.0, 49.0, 300.0, 61.0),
            line_at("und seine zweite.", 0.0, 63.0, 200.0, 75.0),
            line_at("Überschrift", 0.0, 84.0, 200.0, 102.0),
            line_at("Text unter ihr", 0.0, 103.0, 200.0, 115.0),
            line_at("in zwei Zeilen.", 0.0, 117.0, 200.0, 129.0),
        ];
        let cfg = LayoutConfig::default();
        let starts = |blocks: &[Block]| -> Vec<String> {
            blocks.iter().map(|b| b.lines[0].text.clone()).collect()
        };
        let exact = group_exact_blocks(lines.clone(), &cfg, 0.0, &[]);
        assert_eq!(
            starts(&exact),
            [
                "Erster Absatz, erste Zeile",
                "Zweiter Absatz, erste Zeile",
                "Überschrift",
                "Text unter ihr",
            ]
        );
        // Recognized boxes vary too much for that: there only a gap of most
        // of a line counts, and none of these is one.
        assert_eq!(group_blocks(lines, &cfg).len(), 1);
    }

    #[test]
    fn a_page_read_from_its_text_takes_its_gutters_from_its_cells() {
        // Cells a third of a line apart, as a browser sets a bordered table:
        // narrower than any gutter a recognized page would be allowed.
        let row = |y: f32, cells: &[(&str, f32, f32)]| {
            let mut line = line_at(
                &cells.iter().map(|c| c.0).collect::<Vec<_>>().join(" "),
                cells[0].1,
                y,
                cells[cells.len() - 1].2,
                y + 12.0,
            );
            for &(text, x0, x1) in cells {
                let bbox = Rect::new(x0, y, x1, y + 12.0);
                line.words.push(crate::doc::Word {
                    text: text.into(),
                    bbox,
                    confidence: 1.0,
                });
                line.segments.push(crate::doc::Segment {
                    text: text.into(),
                    bbox,
                    confidence: 1.0,
                });
            }
            line
        };
        let lines = vec![
            row(
                0.0,
                &[
                    ("Gewerk", 10.0, 60.0),
                    ("Budget", 80.0, 130.0),
                    ("Ist", 160.0, 180.0),
                ],
            ),
            row(
                15.0,
                &[
                    ("Elektro", 0.0, 50.0),
                    ("120.000", 70.0, 130.0),
                    ("98.500", 134.0, 180.0),
                ],
            ),
            row(
                30.0,
                &[
                    ("Trockenbau", 0.0, 66.0),
                    ("45.000", 70.0, 118.0),
                    ("47.200", 134.0, 180.0),
                ],
            ),
        ];
        let cfg = LayoutConfig::default();
        let exact = group_exact_blocks(lines.clone(), &cfg, 0.0, &[]);
        let table = exact[0].table.as_ref().expect("a table");
        assert_eq!((table.rows, table.columns), (3, 3));
        let last: Vec<&str> = table
            .cells
            .iter()
            .filter(|c| c.column == 2)
            .map(|c| c.text.as_str())
            .collect();
        assert_eq!(last, ["Ist", "98.500", "47.200"]);
    }

    #[test]
    fn single_column_reads_top_to_bottom() {
        let lines = vec![
            line_at("second", 0.0, 30.0, 100.0, 40.0),
            line_at("first", 0.0, 10.0, 100.0, 20.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["first", "second"]);
    }

    #[test]
    fn two_columns_read_column_by_column() {
        // Left column x 0..90, right column x 130..220, gutter of 40px with
        // 10px lines: far wider than column_gap_factor * median height.
        let lines = vec![
            line_at("L1", 0.0, 0.0, 90.0, 10.0),
            line_at("R1", 130.0, 0.0, 220.0, 10.0),
            line_at("L2", 0.0, 14.0, 90.0, 24.0),
            line_at("R2", 130.0, 14.0, 220.0, 24.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["L1", "L2", "R1", "R2"]);
    }

    #[test]
    fn table_columns_are_not_mistaken_for_page_columns() {
        // A five-column table: cells are close together, so the page must be
        // read row by row, not column by column.
        let mut lines = Vec::new();
        let columns = [0.0, 120.0, 620.0, 800.0, 1100.0];
        let widths = [60.0, 460.0, 120.0, 220.0, 200.0];
        for row in 0..4 {
            let y = 100.0 + row as f32 * 40.0;
            for (index, (&x, &w)) in columns.iter().zip(&widths).enumerate() {
                lines.push(line_at(&format!("r{row}c{index}"), x, y, x + w, y + 24.0));
            }
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        let texts = texts(&ordered);
        // Each row comes back as one line, read across its cells.
        assert_eq!(texts.len(), 4, "{texts:?}");
        assert_eq!(texts[0], "r0c0 r0c1 r0c2 r0c3 r0c4", "{texts:?}");
        assert_eq!(texts[1], "r1c0 r1c1 r1c2 r1c3 r1c4", "{texts:?}");
    }

    #[test]
    fn newspaper_columns_still_split() {
        // Same page width, but a gutter of 180 px (12% of the width) and the
        // tight leading real body text has, so no row-wise cut comes first.
        let mut lines = Vec::new();
        for row in 0..6 {
            let y = 100.0 + row as f32 * 30.0;
            lines.push(line_at(&format!("L{row}"), 0.0, y, 660.0, y + 24.0));
            lines.push(line_at(&format!("R{row}"), 840.0, y, 1500.0, y + 24.0));
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        let texts = texts(&ordered);
        assert_eq!(
            &texts[..6],
            &["L0", "L1", "L2", "L3", "L4", "L5"],
            "{texts:?}"
        );
    }

    #[test]
    fn columns_on_a_baseline_grid_are_not_read_across() {
        // Two columns set on one grid, with the leading a page of prose has: a
        // horizontal gap sits between every pair of lines, so the row-wise cut
        // comes first and reads the page across the gutter — unless the corridor
        // between the columns is taken first.
        let mut lines = Vec::new();
        for row in 0..4 {
            let y = 100.0 + row as f32 * 90.0;
            lines.push(line_at(&format!("L{row}"), 0.0, y, 660.0, y + 24.0));
            lines.push(line_at(&format!("R{row}"), 900.0, y, 1500.0, y + 24.0));
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(
            texts(&ordered),
            ["L0", "L1", "L2", "L3", "R0", "R1", "R2", "R3"]
        );
    }

    #[test]
    fn a_tables_wide_cells_are_not_page_columns() {
        // A price list: names down the left, three columns of figures on the
        // right. Both sides are wide and every baseline carries boxes on both, so
        // what says the gutter runs through the rows is that the right-hand side
        // puts three boxes on each of them.
        let mut lines = Vec::new();
        for row in 0..4 {
            let y = 100.0 + row as f32 * 30.0;
            lines.push(line_at(&format!("Artikel{row}"), 170.0, y, 500.0, y + 24.0));
            for (index, x) in [600.0, 800.0, 1010.0].iter().enumerate() {
                lines.push(line_at(
                    &format!("{row}{index}"),
                    *x,
                    y,
                    x + 180.0,
                    y + 24.0,
                ));
            }
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        let texts = texts(&ordered);
        assert_eq!(texts.len(), 4, "{texts:?}");
        assert_eq!(texts[0], "Artikel0 00 01 02", "{texts:?}");
    }

    #[test]
    fn a_corner_of_the_sheet_is_not_split_into_columns() {
        // A drawing's title block: five labels and the values beside them. It
        // reads label then value, and what keeps the corridor between them from
        // cutting it in two is that it covers a corner of the sheet rather than
        // the width of it.
        let mut lines = Vec::new();
        for row in 0..5 {
            let y = 900.0 + row as f32 * 40.0;
            lines.push(line_at(&format!("Feld{row}"), 2300.0, y, 2500.0, y + 24.0));
            lines.push(line_at(&format!("Wert{row}"), 2650.0, y, 2950.0, y + 24.0));
        }
        let cfg = LayoutConfig::default();
        let columns = PageColumns::default();
        let sheet = Sheet {
            scale: 24.0,
            width: 3000.0,
            exact: false,
            columns: &columns,
        };
        assert!(column_corridors(&lines, sheet, &cfg).is_empty());
        // On a page that holds nothing else, the same block is all there is to go
        // by, and then its two sides are the page's columns.
        let alone = Sheet {
            width: 650.0,
            ..sheet
        };
        assert_eq!(column_corridors(&lines, alone, &cfg).len(), 1);
    }

    #[test]
    fn headline_above_columns_comes_first() {
        let lines = vec![
            line_at("L1", 0.0, 60.0, 90.0, 70.0),
            line_at("R1", 130.0, 60.0, 220.0, 70.0),
            line_at("HEADLINE", 0.0, 0.0, 220.0, 20.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered)[0], "HEADLINE");
    }

    #[test]
    fn boxes_on_one_baseline_become_one_line() {
        // A receipt: item and right-aligned price are two boxes per row, and the
        // rows repeat the same two columns.
        let items = ["Milch 1L", "Brot 500g", "Kaffee 500g", "Butter 250g"];
        let prices = ["1,19", "2,49", "6,99", "2,29"];
        let mut lines = Vec::new();
        for (row, (item, price)) in items.iter().zip(prices).enumerate() {
            let y = 100.0 + row as f32 * 40.0;
            lines.push(line_at(item, 10.0, y, 130.0, y + 24.0));
            lines.push(line_at(price, 260.0, y + 1.0, 300.0, y + 23.0));
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(
            texts(&ordered),
            [
                "Milch 1L 1,19",
                "Brot 500g 2,49",
                "Kaffee 500g 6,99",
                "Butter 250g 2,29"
            ]
        );
        // The merged line covers both boxes.
        assert_eq!(ordered[0].bbox.x1, 300.0);
        assert!(ordered[0].confidence > 0.0);
    }

    #[test]
    fn one_box_per_side_is_never_a_column_split() {
        // Label and value on one line: the gap between them must not become a
        // column boundary.
        let lines = vec![
            line_at("Summe", 10.0, 100.0, 120.0, 124.0),
            line_at("16,45", 150.0, 101.0, 210.0, 123.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["Summe 16,45"]);
    }

    #[test]
    fn a_single_wide_gap_is_not_a_table() {
        // Two boxes on one baseline with nothing repeating below them: joining
        // them would invent a line, so they stay separate.
        let lines = vec![
            line_at("Anlage", 10.0, 100.0, 120.0, 124.0),
            line_at("Seite 4", 600.0, 101.0, 700.0, 123.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["Anlage", "Seite 4"]);
    }

    #[test]
    fn table_rows_read_across_their_cells() {
        let mut lines = Vec::new();
        for (row, cells) in [["1", "Schiene", "12"], ["2", "Lager", "48"]]
            .iter()
            .enumerate()
        {
            let y = 100.0 + row as f32 * 40.0;
            for (index, cell) in cells.iter().enumerate() {
                let x = 20.0 + index as f32 * 200.0;
                lines.push(line_at(cell, x, y, x + 120.0, y + 24.0));
            }
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["1 Schiene 12", "2 Lager 48"]);
    }

    #[test]
    fn scattered_labels_at_one_height_are_not_joined() {
        // A drawing: two labels far apart that happen to share a height. Without
        // repeating columns they must stay separate lines.
        let lines = vec![
            line_at("MASSSTAB 1:2", 40.0, 100.0, 220.0, 124.0),
            line_at("R110", 900.0, 102.0, 980.0, 122.0),
            line_at("BLATT 1 VON 3", 40.0, 300.0, 240.0, 324.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["MASSSTAB 1:2", "R110", "BLATT 1 VON 3"]);
    }

    #[test]
    fn repeating_columns_are_recognized() {
        let mut lines = Vec::new();
        for row in 0..4 {
            let y = 100.0 + row as f32 * 40.0;
            lines.push(line_at("item", 20.0, y, 140.0, y + 24.0));
            lines.push(line_at("1,19", 400.0, y, 460.0, y + 24.0));
        }
        let scale = median_height(&lines);
        assert!(has_repeating_columns(&lines, scale, 0.55));

        // Three scattered labels do not qualify.
        let scattered = vec![
            line_at("a", 0.0, 0.0, 50.0, 20.0),
            line_at("b", 500.0, 100.0, 560.0, 120.0),
            line_at("c", 200.0, 220.0, 260.0, 240.0),
            line_at("d", 800.0, 330.0, 860.0, 350.0),
        ];
        let scale = median_height(&scattered);
        assert!(!has_repeating_columns(&scattered, scale, 0.55));
    }

    #[test]
    fn separate_baselines_stay_separate() {
        let lines = vec![
            line_at("erste", 10.0, 100.0, 200.0, 124.0),
            line_at("zweite", 10.0, 140.0, 200.0, 164.0),
        ];
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(texts(&ordered), ["erste", "zweite"]);
    }

    #[test]
    fn merging_can_be_switched_off() {
        let lines = vec![
            line_at("Milch 1L", 10.0, 100.0, 120.0, 124.0),
            line_at("1,19", 260.0, 101.0, 300.0, 123.0),
        ];
        let cfg = LayoutConfig {
            merge_baselines: false,
            ..LayoutConfig::default()
        };
        assert_eq!(texts(&reading_order(lines, &cfg)), ["Milch 1L", "1,19"]);
    }

    #[test]
    fn merged_lines_keep_their_word_boxes() {
        // Boxes close enough to merge without needing a table structure.
        let mut left = line_at("Summe", 10.0, 100.0, 120.0, 124.0);
        left.words = vec![Word {
            text: "Summe".into(),
            bbox: Rect::new(10.0, 100.0, 120.0, 124.0),
            confidence: 0.9,
        }];
        let mut right = line_at("16,45", 150.0, 101.0, 210.0, 123.0);
        right.words = vec![Word {
            text: "16,45".into(),
            bbox: Rect::new(150.0, 101.0, 210.0, 123.0),
            confidence: 0.8,
        }];
        let ordered = reading_order(vec![left, right], &LayoutConfig::default());
        assert_eq!(ordered.len(), 1);
        assert_eq!(ordered[0].words.len(), 2);
        assert_eq!(ordered[0].words[1].text, "16,45");
    }

    #[test]
    fn newspaper_columns_are_not_merged_across_the_gutter() {
        // Two columns whose lines share y positions: the gutter must win, so
        // each column keeps its own lines.
        let mut lines = Vec::new();
        for row in 0..6 {
            let y = 100.0 + row as f32 * 30.0;
            lines.push(line_at(&format!("L{row}"), 0.0, y, 660.0, y + 24.0));
            lines.push(line_at(&format!("R{row}"), 840.0, y, 1500.0, y + 24.0));
        }
        let ordered = reading_order(lines, &LayoutConfig::default());
        assert_eq!(&texts(&ordered)[..6], &["L0", "L1", "L2", "L3", "L4", "L5"]);
    }

    #[test]
    fn paragraph_break_splits_blocks() {
        let lines = vec![
            line_at("a", 0.0, 0.0, 100.0, 10.0),
            line_at("b", 0.0, 12.0, 100.0, 22.0),
            // 30px gap: clearly a new paragraph for 10px lines.
            line_at("c", 0.0, 52.0, 100.0, 62.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].text(), "a\nb");
        assert_eq!(blocks[1].text(), "c");
    }

    #[test]
    fn a_hyphenated_paragraph_is_not_a_heading() {
        // De-hyphenation unions the two boxes, which used to read as one line of
        // twice the height and came out of the Markdown exporter as `## `.
        let lines = vec![
            line_at("Die Unterneh-", 0.0, 0.0, 200.0, 10.0),
            line_at("mensberatung prueft", 0.0, 12.0, 200.0, 22.0),
            line_at("die Bilanz und be-", 0.0, 24.0, 200.0, 34.0),
            line_at("richtet dem Vorstand", 0.0, 36.0, 200.0, 46.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks.len(), 1);
        assert_eq!(
            blocks[0].kind,
            BlockKind::Paragraph,
            "{:?}",
            blocks[0].text()
        );
        assert!(blocks[0].text().contains("Unternehmensberatung"));
    }

    #[test]
    fn hyphenated_words_are_joined() {
        let lines = vec![
            line_at("Zusammen-", 0.0, 0.0, 100.0, 10.0),
            line_at("fassung folgt", 0.0, 12.0, 100.0, 22.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].text(), "Zusammenfassung folgt");
    }

    #[test]
    fn a_suspended_hyphen_is_not_a_broken_word() {
        for (head, tail, joined) in [
            ("Ihren Vor-", "und Nachnamen", "Ihren Vor-\nund Nachnamen"),
            ("Ein-", "oder Ausgang", "Ein-\noder Ausgang"),
            ("pre-", "and post-processing", "pre-\nand post-processing"),
            ("Vor-", "u. Nachname", "Vor-\nu. Nachname"),
            (
                "für 2-",
                "und 3-Zimmer-Wohnungen",
                "für 2-\nund 3-Zimmer-Wohnungen",
            ),
            ("die 10-", "bis 12-jährigen", "die 10-\nbis 12-jährigen"),
            ("Ein-", "und 2-Zimmer", "Ein-\nund 2-Zimmer"),
            // "und" as the start of a longer word is a broken word after all
            ("Verb-", "undenkbar", "Verbundenkbar"),
            // and so are words that end in what could be a conjunction
            ("Kon-", "to 12345", "Konto 12345"),
            ("Por-", "to, bitte", "Porto, bitte"),
            ("net-", "to", "netto"),
            ("col-", "or of the", "color of the"),
            ("Mate-", "rials und", "Materials und"),
        ] {
            let lines = vec![
                line_at(head, 0.0, 0.0, 100.0, 10.0),
                line_at(tail, 0.0, 12.0, 100.0, 22.0),
            ];
            let blocks = group_blocks(lines, &LayoutConfig::default());
            assert_eq!(blocks[0].text(), joined);
        }
    }

    #[test]
    fn a_compound_broken_at_its_own_hyphen_is_one_word_again() {
        let lines = vec![
            line_at("Nord-", 0.0, 0.0, 100.0, 10.0),
            line_at("Süd-Achse", 0.0, 12.0, 100.0, 22.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].text(), "Nord-Süd-Achse");
        let lines = vec![
            line_at("Wir bieten IT-", 0.0, 0.0, 100.0, 10.0),
            line_at("basierte Dienste an.", 0.0, 12.0, 100.0, 22.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].text(), "Wir bieten IT-basierte Dienste an.");
        assert!(abbreviation_before_hyphen("GPU-") && abbreviation_before_hyphen("für PDF-"));
        assert!(!abbreviation_before_hyphen("Instandhal-") && !abbreviation_before_hyphen("A-"));
    }

    #[test]
    fn a_line_of_code_is_monospaced_and_a_sentence_is_not() {
        let with_words = |text: &str, words: &[(&str, f32, f32)]| {
            let mut line = line_at(text, words[0].1, 0.0, words[words.len() - 1].2, 10.0);
            line.words = words
                .iter()
                .map(|(t, x0, x1)| Word {
                    text: (*t).into(),
                    bbox: Rect::new(*x0, 0.0, *x1, 10.0),
                    confidence: 1.0,
                })
                .collect();
            line
        };
        let code = with_words(
            "result_0 = compute(value_0, factor=0)",
            &[
                ("result_0", 159.0, 297.6),
                ("=", 297.6, 330.2),
                ("compute(value_0,", 330.2, 607.4),
                ("factor=0)", 607.4, 762.3),
            ],
        );
        assert_eq!(monospace_verdict(&code), Some(true));
        let prose = with_words(
            "Run the processing function over",
            &[
                ("Run", 93.7, 153.5),
                ("the", 153.5, 202.5),
                ("processing", 202.5, 353.4),
                ("function", 353.4, 472.9),
                ("over", 472.9, 540.4),
            ],
        );
        assert_eq!(monospace_verdict(&prose), Some(false));
    }

    #[test]
    fn a_big_number_beside_two_lines_leaves_them_two_lines() {
        let lines = vec![
            line_at("1", 0.0, 0.0, 30.0, 40.0),
            line_at("Registrieren Sie sich mit Ihrer", 40.0, 5.0, 300.0, 15.0),
            line_at(
                "E-Mail-Adresse und einem Passwort.",
                40.0,
                20.0,
                300.0,
                30.0,
            ),
        ];
        let merged = merge_baselines(lines, 0.5, f32::MAX);
        let texts: Vec<&str> = merged.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(
            texts,
            [
                "1 Registrieren Sie sich mit Ihrer",
                "E-Mail-Adresse und einem Passwort."
            ]
        );
    }

    #[test]
    fn full_width_text_close_over_two_columns_is_cut_off_them() {
        let mut lines = vec![
            line_at(
                "Der Vorspann laeuft ueber beide Spalten der Seite",
                0.0,
                0.0,
                500.0,
                10.0,
            ),
            line_at(
                "und endet knapp ueber ihnen ohne Abstand.",
                0.0,
                12.0,
                480.0,
                22.0,
            ),
        ];
        for i in 0..3 {
            let y = 24.0 + 12.0 * i as f32;
            lines.push(line_at("linke Spalte mit Text", 0.0, y, 230.0, y + 10.0));
            lines.push(line_at("rechte Spalte mit Text", 270.0, y, 500.0, y + 10.0));
        }
        let split = spanning_edge(&lines, 10.0, 20.0).expect("a cut under the lead");
        assert!(split > 22.0 && split < 24.0, "{split}");
        // No text across the gutter, no cut: the columns are read as they are.
        assert!(spanning_edge(&lines[2..], 10.0, 20.0).is_none());
    }

    #[test]
    fn a_drop_cap_opens_its_paragraphs_first_line() {
        // The initial three lines tall, level with the first line's top.
        let lines = vec![
            line_at("D", 0.0, 0.0, 30.0, 34.0),
            line_at("ie Stadt liegt am Ufer", 34.0, 0.0, 300.0, 10.0),
            line_at("des Flusses und ist seit", 34.0, 12.0, 300.0, 22.0),
            line_at("dem Mittelalter ein Ort.", 34.0, 24.0, 300.0, 34.0),
            line_at("Im neunzehnten Jahrhundert", 0.0, 36.0, 300.0, 46.0),
        ];
        let lines = attach_drop_caps(lines);
        let texts: Vec<&str> = lines.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(texts[0], "Die Stadt liegt am Ufer");
        assert_eq!(lines.len(), 4);
        assert_eq!(lines[0].bbox.x0, 0.0);
        // A large single letter with nothing level with its top is left be.
        let alone = vec![
            line_at("A", 0.0, 0.0, 30.0, 34.0),
            line_at("text below it", 0.0, 40.0, 300.0, 50.0),
            line_at("more text", 0.0, 52.0, 300.0, 62.0),
        ];
        assert_eq!(attach_drop_caps(alone).len(), 3);
        // A glossary's letter beside its first entry is a heading of its own.
        let glossary = vec![
            line_at("A", 0.0, 0.0, 30.0, 34.0),
            line_at("Abschreibung – Wertminderung", 34.0, 0.0, 300.0, 10.0),
            line_at("Aktiva – Vermögen", 34.0, 12.0, 300.0, 22.0),
        ];
        assert_eq!(attach_drop_caps(glossary).len(), 3);
    }

    #[test]
    fn tall_short_line_is_a_heading() {
        let lines = vec![
            line_at("Title", 0.0, 0.0, 80.0, 24.0),
            line_at("body text one", 0.0, 40.0, 100.0, 50.0),
            line_at("body text two", 0.0, 52.0, 100.0, 62.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].kind, BlockKind::Heading);
        assert_eq!(blocks[1].kind, BlockKind::Paragraph);
    }

    #[test]
    fn bullets_become_list_items() {
        let lines = vec![line_at("• erste Position", 0.0, 0.0, 100.0, 10.0)];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].kind, BlockKind::ListItem);
    }

    /// Evenly spaced character spans, as a recognizer reports them.
    fn char_spans(text: &str) -> Vec<CharSpan> {
        let n = text.chars().count() as f32;
        text.chars()
            .enumerate()
            .map(|(i, c)| CharSpan {
                text: c.to_string(),
                x_center: (i as f32 + 0.5) / n,
                x_width: 1.0 / n,
                confidence: 0.8,
            })
            .collect()
    }

    #[test]
    fn word_boxes_follow_a_rotated_line() {
        // A line tilted 45 degrees: its words have to walk down the diagonal
        // instead of each covering the full bounding box.
        let quad = Quad::new([
            Point::new(0.0, 0.0),
            Point::new(70.0, 70.0),
            Point::new(63.0, 77.0),
            Point::new(-7.0, 7.0),
        ]);
        let words = words_from_chars(&quad, &char_spans("ab cd"));
        assert_eq!(words.len(), 2);
        let (left, right) = (&words[0].bbox, &words[1].bbox);
        assert!(
            right.y0 > left.y0 + 10.0,
            "the second word should sit lower: {left:?} {right:?}"
        );
        let bounds = quad.bounds();
        assert!(
            left.height() < bounds.height() * 0.7,
            "a word of a tilted line must not span its whole bbox: {left:?}"
        );
        for word in &words {
            assert!(
                word.bbox.x0 >= bounds.x0 - 0.1 && word.bbox.x1 <= bounds.x1 + 0.1,
                "{:?} left the line",
                word.bbox
            );
        }
    }

    #[test]
    fn word_boxes_of_a_vertical_line_run_bottom_to_top() {
        // `crop_quad` stands tall crops up, so the recognizer read this line
        // from the bottom: the first word belongs at the bottom of the box.
        let quad = Quad::from_rect(Rect::new(0.0, 0.0, 20.0, 200.0));
        let words = words_from_chars(&quad, &char_spans("ab cd"));
        assert_eq!(words.len(), 2);
        assert!(
            words[0].bbox.y0 > words[1].bbox.y1 - 1.0,
            "first word should be lowest: {:?} {:?}",
            words[0].bbox,
            words[1].bbox
        );
        for word in &words {
            assert!(
                word.bbox.y1 <= 200.5 && word.bbox.y0 >= -0.5,
                "{:?}",
                word.bbox
            );
        }
    }

    #[test]
    fn a_bullet_is_stripped_but_a_minus_sign_is_not() {
        assert_eq!(strip_bullet("\u{2022} Milch"), "Milch");
        assert_eq!(strip_bullet("  \u{2013} Brot"), "Brot");
        assert_eq!(strip_bullet("- Eier"), "Eier");
        assert_eq!(strip_bullet("-19,90 Gutschrift"), "-19,90 Gutschrift");
        assert_eq!(strip_bullet("1. Punkt"), "1. Punkt");
        assert_eq!(strip_bullet("-"), "-");
    }

    #[test]
    fn words_split_on_space_spans() {
        let quad = Quad::from_rect(Rect::new(0.0, 0.0, 100.0, 10.0));
        let words = words_from_chars(&quad, &char_spans("ab cd"));
        assert_eq!(words.len(), 2);
        assert_eq!(words[0].text, "ab");
        assert_eq!(words[1].text, "cd");
        assert!(words[0].bbox.x1 <= words[1].bbox.x0 + 1.0);
        assert!(words[1].bbox.x1 <= 100.5, "{:?}", words[1].bbox);
    }
}
