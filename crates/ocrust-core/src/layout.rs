//! Layout analysis: reading order, paragraph grouping and word boxes.
//!
//! A detector returns an unordered pile of line boxes. Everything that makes
//! OCR output *readable* — column order, paragraph breaks, joined hyphenated
//! words — happens here, without any extra model.

use crate::doc::{Block, BlockKind, Line, Word};
use crate::geom::{Quad, Rect};
use crate::recognize::CharSpan;

/// Tunables for layout grouping.
#[derive(Debug, Clone)]
pub struct LayoutConfig {
    /// Lines closer than `line_gap_factor * median_height` join one paragraph.
    pub line_gap_factor: f32,
    /// Minimum gutter width (in median line heights) to accept a column split.
    pub column_gap_factor: f32,
    /// Minimum gutter width as a fraction of the content width.
    ///
    /// This is what separates a page laid out in columns from a table: a
    /// newspaper's gutter is several percent of the page, while the gaps between
    /// table cells are around one percent. Without it, every ruled invoice comes
    /// back read column by column.
    pub column_gap_min_fraction: f32,
    /// A line this much taller than the median is treated as a heading.
    pub heading_height_factor: f32,
    /// Join words split across a line break by a trailing hyphen.
    pub dehyphenate: bool,
    /// Detect columns and read them one after another.
    pub detect_columns: bool,
}

impl Default for LayoutConfig {
    fn default() -> Self {
        Self {
            line_gap_factor: 0.9,
            column_gap_factor: 1.2,
            column_gap_min_fraction: 0.035,
            heading_height_factor: 1.45,
            dehyphenate: true,
            detect_columns: true,
        }
    }
}

/// Median of the line heights, used as the page's scale reference.
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
    let mut out = Vec::with_capacity(lines.len());
    xy_cut(lines, scale, cfg, 0, &mut out);
    out
}

fn xy_cut(mut lines: Vec<Line>, scale: f32, cfg: &LayoutConfig, depth: usize, out: &mut Vec<Line>) {
    if lines.len() <= 1 || depth > 12 {
        lines.sort_by(|a, b| cmp_f32(a.bbox.y0, b.bbox.y0).then(cmp_f32(a.bbox.x0, b.bbox.x0)));
        out.append(&mut lines);
        return;
    }

    // Horizontal band: a y gap that no box spans.
    if let Some(split) = find_gap(&lines, scale * 0.6, |l| (l.bbox.y0, l.bbox.y1)) {
        let (top, bottom): (Vec<Line>, Vec<Line>) =
            lines.into_iter().partition(|l| l.bbox.center_y() < split);
        if !top.is_empty() && !bottom.is_empty() {
            xy_cut(top, scale, cfg, depth + 1, out);
            xy_cut(bottom, scale, cfg, depth + 1, out);
            return;
        }
        return_sorted(lines_from(top, bottom), out);
        return;
    }

    // Vertical gutter: a column break.
    if cfg.detect_columns {
        let content_width = lines.iter().map(|l| l.bbox.x1).fold(f32::MIN, f32::max)
            - lines.iter().map(|l| l.bbox.x0).fold(f32::MAX, f32::min);
        let min_gap =
            (scale * cfg.column_gap_factor).max(content_width * cfg.column_gap_min_fraction);
        if let Some(split) = find_gap(&lines, min_gap, |l| (l.bbox.x0, l.bbox.x1)) {
            let (left, right): (Vec<Line>, Vec<Line>) =
                lines.into_iter().partition(|l| l.bbox.center_x() < split);
            if !left.is_empty() && !right.is_empty() {
                xy_cut(left, scale, cfg, depth + 1, out);
                xy_cut(right, scale, cfg, depth + 1, out);
                return;
            }
            return_sorted(lines_from(left, right), out);
            return;
        }
    }

    lines.sort_by(|a, b| cmp_f32(a.bbox.y0, b.bbox.y0).then(cmp_f32(a.bbox.x0, b.bbox.x0)));
    out.append(&mut lines);
}

fn lines_from(mut a: Vec<Line>, mut b: Vec<Line>) -> Vec<Line> {
    a.append(&mut b);
    a
}

fn return_sorted(mut lines: Vec<Line>, out: &mut Vec<Line>) {
    lines.sort_by(|a, b| cmp_f32(a.bbox.y0, b.bbox.y0).then(cmp_f32(a.bbox.x0, b.bbox.x0)));
    out.append(&mut lines);
}

/// Finds the centre of the widest interval that no box covers.
///
/// `extent` yields each box's `(start, end)` along the axis being cut.
fn find_gap(lines: &[Line], min_gap: f32, extent: impl Fn(&Line) -> (f32, f32)) -> Option<f32> {
    let mut spans: Vec<(f32, f32)> = lines.iter().map(&extent).collect();
    spans.sort_by(|a, b| cmp_f32(a.0, b.0));

    let mut best: Option<(f32, f32)> = None; // (gap size, split position)
    let mut reach = spans[0].1;
    for &(start, end) in &spans[1..] {
        let gap = start - reach;
        if gap > min_gap && best.is_none_or(|(g, _)| gap > g) {
            best = Some((gap, reach + gap * 0.5));
        }
        reach = reach.max(end);
    }
    best.map(|(_, split)| split)
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
            blocks.push(finish_block(std::mem::take(&mut current), scale, cfg));
        }
        current.push(line);
    }
    if !current.is_empty() {
        blocks.push(finish_block(current, scale, cfg));
    }
    blocks
}

fn finish_block(mut lines: Vec<Line>, scale: f32, cfg: &LayoutConfig) -> Block {
    if cfg.dehyphenate {
        dehyphenate(&mut lines);
    }
    let bbox = lines
        .iter()
        .map(|l| l.bbox)
        .reduce(|a, b| a.union(&b))
        .unwrap_or(Rect::new(0.0, 0.0, 0.0, 0.0));

    let kind = classify_block(&lines, scale, cfg);
    Block { kind, bbox, lines }
}

fn classify_block(lines: &[Line], scale: f32, cfg: &LayoutConfig) -> BlockKind {
    let first = match lines.first() {
        Some(l) => l,
        None => return BlockKind::Paragraph,
    };
    let trimmed = first.text.trim_start();
    if trimmed.starts_with(['•', '‣', '·', '–', '—', '*'])
        || trimmed.split_once(['.', ')']).is_some_and(|(head, _)| {
            !head.is_empty() && head.len() <= 3 && head.chars().all(|c| c.is_ascii_digit())
        })
    {
        return BlockKind::ListItem;
    }
    let tall = first.bbox.height() > scale * cfg.heading_height_factor;
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
/// Character positions are fractions of the line width, so the boxes follow the
/// line's own rotation instead of assuming horizontal text.
pub fn words_from_chars(quad: &Quad, chars: &[CharSpan]) -> Vec<Word> {
    if chars.is_empty() {
        return Vec::new();
    }
    let q = quad.ordered();
    let bbox = q.bounds();
    let (x0, width) = (bbox.x0, bbox.width());

    let mut words = Vec::new();
    let mut buf = String::new();
    let mut conf_sum = 0.0f32;
    let mut count = 0u32;
    let mut start = f32::MAX;
    let mut end = f32::MIN;

    let flush = |words: &mut Vec<Word>,
                 buf: &mut String,
                 conf_sum: &mut f32,
                 count: &mut u32,
                 start: &mut f32,
                 end: &mut f32| {
        if buf.trim().is_empty() {
            buf.clear();
            *conf_sum = 0.0;
            *count = 0;
            *start = f32::MAX;
            *end = f32::MIN;
            return;
        }
        words.push(Word {
            text: std::mem::take(buf),
            bbox: Rect::new(
                x0 + start.max(0.0) * width,
                bbox.y0,
                x0 + end.min(1.0) * width,
                bbox.y1,
            ),
            confidence: if *count == 0 {
                0.0
            } else {
                *conf_sum / *count as f32
            },
        });
        *conf_sum = 0.0;
        *count = 0;
        *start = f32::MAX;
        *end = f32::MIN;
    };

    for span in chars {
        if span.text.trim().is_empty() {
            flush(
                &mut words,
                &mut buf,
                &mut conf_sum,
                &mut count,
                &mut start,
                &mut end,
            );
            continue;
        }
        buf.push_str(&span.text);
        conf_sum += span.confidence;
        count += 1;
        start = start.min(span.x_center - span.x_width * 0.5);
        end = end.max(span.x_center + span.x_width * 0.5);
    }
    flush(
        &mut words,
        &mut buf,
        &mut conf_sum,
        &mut count,
        &mut start,
        &mut end,
    );
    words
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
            angle: 0.0,
            det_score: 0.9,
            words: Vec::new(),
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
        // Row-major order: the first row's cells come before the second row's.
        assert_eq!(
            &texts[..5],
            &["r0c0", "r0c1", "r0c2", "r0c3", "r0c4"],
            "{texts:?}"
        );
        assert_eq!(texts[5], "r1c0", "{texts:?}");
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

    #[test]
    fn words_split_on_space_spans() {
        let quad = Quad::from_rect(Rect::new(0.0, 0.0, 100.0, 10.0));
        let spans = |s: &str| -> Vec<CharSpan> {
            let n = s.chars().count() as f32;
            s.chars()
                .enumerate()
                .map(|(i, c)| CharSpan {
                    text: c.to_string(),
                    x_center: (i as f32 + 0.5) / n,
                    x_width: 1.0 / n,
                    confidence: 0.8,
                })
                .collect()
        };
        let words = words_from_chars(&quad, &spans("ab cd"));
        assert_eq!(words.len(), 2);
        assert_eq!(words[0].text, "ab");
        assert_eq!(words[1].text, "cd");
        assert!(words[0].bbox.x1 <= words[1].bbox.x0 + 1.0);
        assert!(words[1].bbox.x1 <= 100.5, "{:?}", words[1].bbox);
    }
}
