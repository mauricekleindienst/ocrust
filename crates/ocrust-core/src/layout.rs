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
    if lines.len() < 2 {
        return lines;
    }
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
    let mut out = Vec::with_capacity(lines.len());
    let sheet = Sheet {
        scale,
        width: horizontal_span(&lines),
    };
    xy_cut(lines, sheet, cfg, max_merge_gap, 0, &mut out);
    out
}

/// What a region keeps of the page it was cut out of.
#[derive(Clone, Copy)]
struct Sheet {
    /// Median line height, the scale everything else is measured in.
    scale: f32,
    /// Width of the page's content, which is what a column corridor divides.
    width: f32,
}

fn xy_cut(
    lines: Vec<Line>,
    sheet: Sheet,
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
    let max_merge_gap =
        if cfg.merge_baselines && baseline_bands(&lines, cfg.baseline_overlap).len() >= 3 {
            if has_repeating_columns(&lines, scale, cfg.baseline_overlap) {
                f32::MAX
            } else {
                scale * cfg.max_merge_gap_factor
            }
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

    // Vertical gutter: a column break.
    if cfg.detect_columns {
        let content_width = lines.iter().map(|l| l.bbox.x1).fold(f32::MIN, f32::max)
            - lines.iter().map(|l| l.bbox.x0).fold(f32::MAX, f32::min);
        let min_gap =
            (scale * cfg.column_gap_factor).max(content_width * cfg.column_gap_min_fraction);
        if let Some((split, gap)) = find_gap(&lines, min_gap, |l| (l.bbox.x0, l.bbox.x1)) {
            let (left, right): (Vec<Line>, Vec<Line>) =
                lines.into_iter().partition(|l| l.bbox.center_x() < split);
            let straddled = shared_baseline_share(&left, &right, cfg.baseline_overlap);
            // A column is a block of lines. One box on a side is a cell, a page
            // number or a stray label — never a column.
            let thin_side = left.len() < 2 || right.len() < 2;
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
            if thin_side
                || (straddled >= cfg.column_shared_baseline_veto && (too_narrow || crowded))
            {
                // The gutter runs through rows, not between columns.
                emit_leaf(lines_from(left, right), cfg, max_merge_gap, out);
                return;
            }
            if !left.is_empty() && !right.is_empty() {
                xy_cut(left, sheet, cfg, max_merge_gap, depth + 1, out);
                xy_cut(right, sheet, cfg, max_merge_gap, depth + 1, out);
                return;
            }
            return_sorted(lines_from(left, right), cfg, max_merge_gap, out);
            return;
        }
    }

    emit_leaf(lines, cfg, max_merge_gap, out);
}

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

/// Splits lines into groups that share a baseline.
fn baseline_bands<'a>(
    lines: impl IntoIterator<Item = &'a Line>,
    min_overlap: f32,
) -> Vec<Vec<&'a Line>> {
    let mut bands: Vec<Vec<&Line>> = Vec::new();
    for line in lines {
        let joined = bands.last_mut().is_some_and(|band| {
            band.iter().any(|existing| {
                let overlap = existing.bbox.vertical_overlap(&line.bbox);
                let shorter = existing.bbox.height().min(line.bbox.height()).max(1.0);
                overlap / shorter >= min_overlap
            })
        });
        if joined {
            bands.last_mut().expect("checked above").push(line);
        } else {
            bands.push(vec![line]);
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
fn column_corridors(lines: &[Line], sheet: Sheet, cfg: &LayoutConfig) -> Vec<f32> {
    if lines.len() < cfg.column_min_bands * 2 {
        return Vec::new();
    }
    let width = horizontal_span(lines);
    if width < sheet.width * cfg.column_corridor_page_share {
        return Vec::new();
    }
    let min_gap =
        (sheet.scale * cfg.column_corridor_gap_factor).max(width * cfg.column_gap_min_fraction);
    let gaps = find_gaps(lines, min_gap, |l| (l.bbox.x0, l.bbox.x1));
    // The widest corridor first, then the two widest together, and so on: a page
    // set in three columns has two corridors, and neither of them alone leaves
    // columns of text on both sides of it.
    for count in 1..=gaps.len().min(cfg.column_max_corridors) {
        let mut splits: Vec<f32> = gaps[..count].iter().map(|(split, _)| *split).collect();
        splits.sort_by(|a, b| cmp_f32(*a, *b));
        let columns = slice_at(lines, &splits);
        let ok =
            columns.iter().all(|column| reads_as_column(column, cfg)) && evenly_wide(&columns, cfg);
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

/// Share of the left side's baselines that also carry a box on the right.
fn shared_baseline_share(left: &[Line], right: &[Line], min_overlap: f32) -> f32 {
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
        let shares_baseline = group.iter().any(|existing| {
            let overlap = existing.bbox.vertical_overlap(&line.bbox);
            let shorter = existing.bbox.height().min(line.bbox.height()).max(1.0);
            overlap / shorter >= min_overlap
        }) && group
            .iter()
            .map(|existing| (line.bbox.x0 - existing.bbox.x1).max(existing.bbox.x0 - line.bbox.x1))
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
    if lines.is_empty() {
        return Vec::new();
    }
    let scale = median_height(&lines);
    let text_height = median_text_height(&lines);

    // Tables are looked for over the whole page, not inside each block. The
    // blocks come from vertical gaps, and a ruled form's rows are spaced widely
    // enough that the gap rule cuts its table in two; a word space, meanwhile, is
    // a property of the page rather than of one block.
    let tables = if cfg.detect_tables {
        let gutter = crate::table::gutter_width(&lines, text_height);
        crate::table::find(&lines, text_height, gutter)
    } else {
        Vec::new()
    };

    let mut blocks: Vec<Block> = Vec::new();
    let mut lines = lines;
    // From the back, so the indices the detector reported still hold.
    let mut tail = lines.len();
    for found in tables.into_iter().rev() {
        let after = lines.split_off(found.end);
        let table_lines = lines.split_off(found.start);
        if !after.is_empty() {
            blocks.extend(paragraphs(after, scale, text_height, cfg));
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
        blocks.extend(paragraphs(lines, scale, text_height, cfg));
    }
    blocks.reverse();
    blocks
}

/// Groups lines into paragraphs, headings and list items by their vertical gaps.
fn paragraphs(lines: Vec<Line>, scale: f32, text_height: f32, cfg: &LayoutConfig) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    let mut current: Vec<Line> = Vec::new();

    for line in lines {
        let starts_new = match current.last() {
            None => false,
            Some(prev) => {
                let gap = line.bbox.y0 - prev.bbox.y1;
                let shares_column = line.bbox.horizontal_overlap(&prev.bbox)
                    > 0.25 * prev.bbox.width().min(line.bbox.width());
                gap > scale * cfg.line_gap_factor || !shares_column
            }
        };
        if starts_new {
            blocks.push(finish_block(std::mem::take(&mut current), text_height, cfg));
        }
        current.push(line);
    }
    if !current.is_empty() {
        blocks.push(finish_block(current, text_height, cfg));
    }
    // Emitted back to front by the caller, which reverses the whole list.
    blocks.reverse();
    blocks
}

fn finish_block(mut lines: Vec<Line>, text_height: f32, cfg: &LayoutConfig) -> Block {
    if cfg.dehyphenate {
        dehyphenate(&mut lines);
    }
    plain_block(lines, text_height, cfg)
}

fn plain_block(lines: Vec<Line>, text_height: f32, cfg: &LayoutConfig) -> Block {
    Block {
        kind: classify_block(&lines, text_height, cfg),
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

fn classify_block(lines: &[Line], text_height: f32, cfg: &LayoutConfig) -> BlockKind {
    let first = match lines.first() {
        Some(l) => l,
        None => return BlockKind::Paragraph,
    };
    let trimmed = first.text.trim_start();
    if trimmed.starts_with(BULLETS)
        || trimmed.split_once(['.', ')']).is_some_and(|(head, _)| {
            !head.is_empty() && head.len() <= 3 && head.chars().all(|c| c.is_ascii_digit())
        })
    {
        return BlockKind::ListItem;
    }
    // The quad, not the box: a de-hyphenated line's box spans both of the lines
    // it came from, which would make every short hyphenated paragraph a heading.
    let tall = first.quad.edge_height() > text_height * cfg.heading_height_factor;
    if tall && lines.len() <= 2 && first.text.chars().count() <= 120 {
        return BlockKind::Heading;
    }
    BlockKind::Paragraph
}

/// Joins words broken by a hyphen at a line end.
fn dehyphenate(lines: &mut Vec<Line>) {
    let mut i = 0;
    while i + 1 < lines.len() {
        let ends_hyphen = lines[i]
            .text
            .trim_end()
            .ends_with(['-', '\u{2010}', '\u{00ad}']);
        let next_starts_lower = lines[i + 1]
            .text
            .trim_start()
            .chars()
            .next()
            .is_some_and(|c| c.is_lowercase());
        if ends_hyphen && next_starts_lower {
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
        let sheet = Sheet {
            scale: 24.0,
            width: 3000.0,
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
    fn hyphen_kept_before_capitalized_word() {
        let lines = vec![
            line_at("Nord-", 0.0, 0.0, 100.0, 10.0),
            line_at("Süd-Achse", 0.0, 12.0, 100.0, 22.0),
        ];
        let blocks = group_blocks(lines, &LayoutConfig::default());
        assert_eq!(blocks[0].text(), "Nord-\nSüd-Achse");
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
