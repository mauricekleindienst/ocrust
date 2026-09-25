//! Recovering a table's rows and columns from where its cells sit.
//!
//! No table model and no ruling lines are read. Two things say where the cells
//! are: the detector returns one box per cell when the columns are far enough
//! apart, and [`crate::layout::merge_baselines`] keeps those boxes on the row it
//! joins them into; and within a box, the recognizer's own word positions show
//! the gaps. Either way a cell is a run of words, and a column is a stretch of the
//! page that row after row puts ink in.
//!
//! That finds the tables people actually scan — invoices, receipts, statements,
//! price lists — ruled or not. It does not read row spans: a cell belongs to the
//! row its baseline is on. Nor does it see a header cell that spans two columns
//! and has further header cells beneath it.
//!
//! The guardrails matter more than the algorithm, because a table wrongly found
//! in a letter is worse than a table missed in an invoice. A table has to be at
//! least three rows of at least two columns; the gaps between its cells have to
//! be wider than a word space; and they have to fall in the same places row
//! after row. One gap in one place is where a sentence happened to be split.
//! The same gap on every line is a column.

use crate::doc::{Cell, Line, Segment, Table};
use crate::geom::Rect;

/// Rows a table needs before it is called one.
const MIN_ROWS: usize = 3;
/// Columns a table needs.
const MIN_COLUMNS: usize = 2;
/// The floor under the gap that separates two cells, in text heights.
///
/// Only a floor: a page whose word gaps are all tiny, because the recognizer
/// found few of them, must not end up with a threshold of a pixel or two.
const MIN_GUTTER: f32 = 0.75;
/// How many word spaces wide a gap has to be to separate two cells.
///
/// Word spaces and column gutters are both gaps, and how wide each is depends
/// entirely on the document. Measured over the evaluation corpus: a receipt's
/// word spaces run to 0.87 of the text height while its narrowest gutter is 2.5
/// times it, and a densely set invoice has word spaces at 0.66 and gutters from
/// 0.83. No multiple of the text height sits between both pairs, so the spaces
/// the document itself sets are the scale.
///
/// Twice that. A page with no table has every gap within a factor of two of the
/// quarter point — the corpus's degraded scans run from 18 to 36 pixels and
/// nothing else — so nothing is split and no table is found, which is the right
/// answer. A page with a table has gutters three to eight times its word spaces.
/// At one and a half, three sentences of similar length come back as a
/// three-column table.
///
/// A header whose column titles sit closer together than the figures beneath them
/// reads as one cell at this width; the columns, once found, cut it (see
/// [`Candidate::cut_at`]).
const GUTTER_OVER_SPACE: f32 = 2.0;
/// Where in the sorted gaps to read the document's word space off.
///
/// The quarter point rather than the median: a table of short cells has more
/// gutters than word spaces, and the median would then measure a gutter.
const SPACE_QUANTILE: f32 = 0.25;
/// How much wider than the gap below it a gap has to be to end the word spaces.
///
/// Measured on a page that is nothing but a price list: its word spaces run to
/// 0.58 of the text height and its narrowest gutter is 0.91 of it, so the two
/// groups are only a factor of 1.6 apart even where nothing lies between them.
const GUTTER_JUMP: f32 = 1.5;
/// How many of the gaps have to sit below that jump for it to count.
///
/// A line whose recognizer split one word in two leaves a gap of a pixel or
/// three, and a handful of those must not pass for the page's word spacing.
const GUTTER_JUMP_SHARE: f32 = 0.1;
/// A cell covering this much of the table spans it — a title or a total — and is
/// not evidence about where the columns are.
const SPANNING_SHARE: f32 = 0.6;
/// How many of a table's rows have to carry more than one cell.
///
/// The check that keeps prose out. A page of sentences of similar length leaves
/// its last word a little further from the one before it on every line, and three
/// such lines look like a right-hand column; what gives them away is that the
/// lines between them carry no cells at all. A table is mostly rows of cells.
const MIN_MULTI_CELL_SHARE: f32 = 0.6;
/// Columns beyond this are a sign the page is not a table at all.
const MAX_COLUMNS: usize = 64;
/// How much wider than a word space a gap has to be for the columns to cut a cell
/// there.
///
/// A quarter wider. The first pass has already decided this gap is a space, so the
/// bar is low — a header sets its titles closer together than the figures beneath
/// them — but not at the space itself, or `Mehrwertsteuer 19 %` loses its `%` to
/// the column of quantities the `19` happens to sit over.
const RECUT_OVER_SPACE: f32 = 1.25;
/// How much of a cell has to sit in a column before it counts as being in it.
///
/// Right-aligned figures and left-aligned labels do not start in the same place,
/// so a cell may reach a little way into the column beside its own. A fifth is
/// the line between reaching in and belonging to both.
const MIN_CELL_OVERLAP: f32 = 0.2;
/// Rows that have to put something in a stretch before it counts as a column.
const MIN_COLUMN_ROWS: usize = 2;
/// How far apart two rows may sit, in text heights, and still be one table.
const MAX_ROW_GAP: f32 = 2.0;
/// How much smaller a line's print has to be than its neighbour's to be the
/// page's margin rather than a row: a browser prints its date, title and page
/// number at eight points over ten.
const SMALLER_PRINT: f32 = 0.87;
/// How much further apart than any two rows before it, in text heights, a
/// line may sit and still be the table's.
const ROW_GAP_JUMP: f32 = 0.5;

/// How close under a row, in text heights, the next line of a cell sits: at
/// the text's line spacing, up to one and a half lines, with nothing between.
/// Rows set further apart than that are rows, however their cells end.
const CONTINUATION_GAP: f32 = 0.3;
/// How much of the shorter one's height two lines overlap by, at least, when
/// they are the lines of one row set between each other's.
const INTERLEAVED: f32 = 0.35;
/// The same, when the two lines have their cells in different columns.
const INTERLEAVED_APART: f32 = 0.25;

/// A run of lines that reads as a table.
pub(crate) struct Found {
    /// The first line of the table, and one past its last.
    pub start: usize,
    pub end: usize,
    pub table: Table,
}

/// Finds the tables among `lines`, in order and never overlapping.
///
/// Lines outside the runs it returns are ordinary text. An invoice is one block
/// of lines from the layout's point of view — address, table, totals, terms — so
/// looking for a table anywhere in a block rather than assuming the whole block
/// is one is what makes it work on the documents that have tables.
///
/// `rules` are the straight lines the page draws, known when it was read from
/// its own text: a table's borders, which say where its rows and columns are.
pub(crate) fn find(lines: &[Line], text_height: f32, gutter: f32, rules: &[Rect]) -> Vec<Found> {
    let rules = Rules::of(rules, text_height);
    let rows: Vec<Vec<Candidate>> = lines
        .iter()
        .map(|line| rules.split(cells_of(line, gutter), line))
        .collect();
    let ruled = rules.above(lines, &rows);
    if log::log_enabled!(log::Level::Debug) {
        log::debug!("table scan: text height {text_height:.1}, gutter {gutter:.1}");
        for (index, row) in rows.iter().enumerate() {
            let cells: Vec<String> = row
                .iter()
                .map(|c| {
                    format!(
                        "{:?}[{:.0}..{:.0}]",
                        c.cell.text, c.cell.bbox.x0, c.cell.bbox.x1
                    )
                })
                .collect();
            log::debug!("  row {index}: {}", cells.join("  "));
        }
    }

    let mut found = Vec::new();
    let mut at = 0usize;
    // One past the last row a table already took: a header row above a run
    // may be adopted only if no table owns it yet.
    let mut taken = 0usize;
    while at < rows.len() {
        if rows[at].len() < MIN_COLUMNS {
            at += 1;
            continue;
        }
        let (end, candidates) = grow(&rows, lines, &ruled, &rules, at, text_height);
        log::debug!("  grow from {at}: end {end}, {candidates} candidate rows");
        if candidates >= MIN_ROWS {
            let continues = continuations(
                &rows[at..end],
                &lines[at..end],
                &ruled[at..end],
                text_height,
            );
            let own_rows = whole_rows(&rows[at..end], &continues);
            // A table drawn with its borders says where its columns are,
            // however close its cells sit.
            let ruled_columns = rules
                .columns(&lines[at..end], &rows[at..end], text_height)
                .filter(|_| multi_cell_rows(&own_rows));
            if let Some(columns) = ruled_columns.or_else(|| columns_of(&own_rows, gutter)) {
                log::debug!(
                    "  run {at}..{end} ({candidates} candidates) -> columns {}",
                    columns
                        .iter()
                        .map(|(a, b)| format!("[{a:.0}..{b:.0}]"))
                        .collect::<Vec<_>>()
                        .join(" ")
                );
                // A header often sets its column titles closer together than the
                // figures beneath them, so it reads as one cell and the run does
                // not begin on it. The columns, now known, say where its titles
                // are. One row only: a table has one header, and everything
                // above it is the page.
                let adopted = at > taken
                    && !in_the_margin(&lines[at - 1], &lines[at], text_height)
                    && adopts(&rows[at - 1], &columns, gutter);
                let start = if adopted { at - 1 } else { at };
                let mut table = grid(
                    &rows[start..end],
                    &continuations(
                        &rows[start..end],
                        &lines[start..end],
                        &ruled[start..end],
                        text_height,
                    ),
                    &columns,
                    gutter,
                    adopted,
                );
                let start = over_the_header(
                    &mut table,
                    lines,
                    &rows,
                    start,
                    taken,
                    &columns,
                    gutter,
                    text_height,
                );
                found.push(Found { start, end, table });
                at = end;
                taken = end;
                continue;
            }
        }
        at += 1;
    }
    found
}

/// The narrowest gap that separates two cells rather than two words.
///
/// See [`GUTTER_OVER_SPACE`]: twice the quarter point of every word gap, and never
/// narrower than [`MIN_GUTTER`] of the text height.
///
/// Measured over a whole page, not over one block. A word space is a property of
/// how the document was set; a block that is nothing but table rows has more
/// gutters in it than word spaces, and its own quarter point lands among the
/// gutters — 231 pixels on one corpus form, which then reads the whole header as
/// a single cell.
pub(crate) fn gutter_width(lines: &[Line], text_height: f32) -> f32 {
    let floor = (text_height * MIN_GUTTER).max(1.0);
    let mut gaps: Vec<f32> = Vec::new();
    for line in lines {
        for pair in line.words.windows(2) {
            let gap = pair[1].bbox.x0 - pair[0].bbox.x1;
            if gap > 0.0 {
                gaps.push(gap);
            }
        }
    }
    if gaps.is_empty() {
        return floor;
    }
    gaps.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let at = ((gaps.len() - 1) as f32 * SPACE_QUANTILE).round() as usize;
    // The quarter point stands in for the page's word space; where the gaps fall
    // into two groups, the stretch between them says it outright and is never
    // allowed to widen the estimate.
    let from_spaces = gaps[at] * GUTTER_OVER_SPACE;
    let between = gutter_between_groups(&gaps);
    log::debug!(
        "{} gaps: quarter point {:.1}, from spaces {from_spaces:.1}, between groups {between:?}",
        gaps.len(),
        gaps[at],
    );
    floor.max(between.map_or(from_spaces, |gutter| gutter.min(from_spaces)))
}

/// The empty stretch between a page's word spaces and its column gutters.
///
/// A page that is nothing but a table has more gutters than word spaces — its
/// cells hold a word each — and then even the quarter point measures a gutter.
/// The two groups are still far apart, and the stretch between them is where the
/// threshold belongs. A page of prose has no such stretch: its gaps crowd
/// together, and there the quarter point is the better measure.
///
/// The highest stretch wins, not the widest. A heading whose two words sit
/// further apart than the text beneath it opens a stretch of its own, well below
/// the gutters, and a threshold taken from that one cuts the heading into cells.
/// A stretch out among the gutters themselves costs nothing instead, because the
/// caller never lets this widen the quarter point's estimate.
///
/// Jumps with almost nothing below them do not count: a line whose recognizer
/// split one word in two leaves a gap of a pixel or three, and a handful of
/// those is not the page's word spacing.
fn gutter_between_groups(gaps: &[f32]) -> Option<f32> {
    let least = (gaps.len() as f32 * GUTTER_JUMP_SHARE).ceil().max(1.0) as usize;
    let mut highest = None;
    for at in least..gaps.len() {
        let (below, above) = (gaps[at - 1], gaps[at]);
        if below > 0.0 && above >= below * GUTTER_JUMP {
            // In the middle of the stretch, on the scale the gaps are spread over.
            highest = Some((below * above).sqrt());
        }
    }
    highest
}

/// Extends a run from `start` for as long as the rows keep the same columns.
///
/// Returns one past the last row of the run and how many of its rows carry more
/// than one cell.
fn grow(
    rows: &[Vec<Candidate>],
    lines: &[Line],
    ruled: &[bool],
    rules: &Rules,
    start: usize,
    text_height: f32,
) -> (usize, usize) {
    let max_row_gap = text_height * MAX_ROW_GAP;
    let mut last = start;
    let mut candidates = 1usize;
    // A line in smaller print than the one under it — the date and title a
    // browser prints over a table running on from the page before — is the
    // page's margin, not the table's first row.
    if lines
        .get(start + 1)
        .is_some_and(|next| in_the_margin(&lines[start], next, text_height))
    {
        return (start + 1, 1);
    }
    let mut previous = &rows[start];
    // The widest gap between two lines of the run so far.
    let mut widest: Option<f32> = None;

    for (offset, row) in rows.iter().enumerate().skip(start + 1) {
        // Two tables of the same shape on one page are two tables. What separates
        // them is the white space, so a row far below the last one ends the run —
        // and so does a line of one cell set clearly further apart than the rows
        // so far: the subject and salutation under a letter's address are
        // paragraphs. Totals set apart from the items are still the table's.
        let gap = lines[offset].bbox.y0 - lines[offset - 1].bbox.y1;
        let jump = widest.is_some_and(|w| gap > w + text_height * ROW_GAP_JUMP);
        if gap > max_row_gap || (jump && row.len() < MIN_COLUMNS) {
            break;
        }
        if in_the_margin(&lines[offset], &lines[offset - 1], text_height) {
            // The page's footer under the table, or its footnotes.
            break;
        }
        widest = Some(widest.map_or(gap, |w| w.max(gap)));
        if row.len() >= MIN_COLUMNS {
            // Against the row before, or the run's first — its header, as a
            // rule, with every column filled: two rows that leave different
            // cells empty may share no gap at all.
            if !aligns(previous, row) && !aligns(&rows[start], row) {
                break;
            }
            previous = row;
            candidates += 1;
            last = offset;
        } else if offset == last + 1
            && carries_on(
                &rows[start..=offset],
                &lines[start..=offset],
                &ruled[start..=offset],
                offset - start,
                text_height,
            )
        {
            // The next line of a cell that wraps: part of the row above.
            last = offset;
        } else if ruled[offset]
            && ruled.get(offset + 1).copied().unwrap_or(false)
            && row.iter().all(|c| rules.boxed(&c.cell.bbox))
        {
            // A row of the table's grid whose other cells are empty: ruled off
            // above and below, with the table's borders either side.
            last = offset;
        } else if spans_the_row(row, &rows[start..=last])
            || opens_alone(row, &rows[start..=last])
            || in_a_column(row, &rows[start..=last])
        {
            // A line across the whole width — a title, a total — a category
            // alone in the first column, or a row with one cell filled, sits
            // inside the table without saying anything about its columns.
            continue;
        } else {
            break;
        }
    }
    (last + 1, candidates)
}

/// Whether a line of one cell starts in the table's first column: a category
/// over the rows that follow, or the first cell of a row whose others are
/// empty.
fn opens_alone(row: &[Candidate], table: &[Vec<Candidate>]) -> bool {
    let Some(left) = table
        .iter()
        .flatten()
        .map(|c| c.cell.bbox.x0)
        .reduce(f32::min)
    else {
        return false;
    };
    let height = row.first().map_or(0.0, |c| c.cell.bbox.height());
    row.len() == 1 && (row[0].cell.bbox.x0 - left).abs() <= height
}

/// Whether `line` is the page's margin beside the table line `other`: in
/// clearly smaller print, and starting further left than the table does — a
/// browser's date and title over it, the URL and page number under it, which
/// stand at the paper's edge. A table's own header, in smaller print over its
/// rows, starts where they do.
fn in_the_margin(line: &Line, other: &Line, text_height: f32) -> bool {
    smaller_print(line, other) && line.bbox.x0 < other.bbox.x0 - text_height * 0.5
}

/// Whether `line` is set in clearly smaller print than `other`: by the median
/// height of its words, which a superscript does not stretch as it does the
/// line's box.
fn smaller_print(line: &Line, other: &Line) -> bool {
    let size = |line: &Line| {
        let mut heights: Vec<f32> = line.words.iter().map(|w| w.bbox.height()).collect();
        heights.sort_by(f32::total_cmp);
        heights
            .get(heights.len() / 2)
            .copied()
            .unwrap_or(line.bbox.height())
    };
    size(line) < size(other) * SMALLER_PRINT
}

/// Takes in the header's upper row, when the table has two: the line right
/// above it whose cells each stand centred over a group of columns — the
/// years over their measures — and a label in the first column set beside
/// both header rows, where the header leaves its first cell empty. Markdown
/// has one header row, so each column's title is its group's and its own
/// (`2024 Umsatz`). Returns where the table now starts.
#[allow(clippy::too_many_arguments)]
fn over_the_header(
    table: &mut Table,
    lines: &[Line],
    rows: &[Vec<Candidate>],
    start: usize,
    taken: usize,
    columns: &[(f32, f32)],
    gutter: f32,
    text_height: f32,
) -> usize {
    let Some(&(first_x0, first_x1)) = columns.first() else {
        return start;
    };
    // The first column's stretch reaches halfway to the second: a label may
    // be wider than the names under it.
    let first_reach = columns
        .get(1)
        .map_or(first_x1 + gutter, |next| (first_x1 + next.0) / 2.0);
    let empty_first = !table.cells.iter().any(|c| c.row == 0 && c.column == 0);
    let mut groups: Option<Vec<(usize, usize, String)>> = None;
    let mut label: Option<String> = None;
    let mut top = start;
    while top > taken && start - top < 2 {
        let (above, below) = (&lines[top - 1], &lines[top]);
        if below.bbox.y0 - above.bbox.y1 > text_height || smaller_print(above, below) {
            break;
        }
        let row = &rows[top - 1];
        let in_first_column =
            |c: &Candidate| c.cell.bbox.x0 >= first_x0 - gutter && c.cell.bbox.x1 <= first_reach;
        if label.is_none() && empty_first && row.len() == 1 && in_first_column(&row[0]) {
            label = Some(row[0].cell.text.clone());
        } else if groups.is_none() && row.iter().all(|c| c.cell.bbox.x0 > first_x1) {
            match grouped(row, columns) {
                Some(found) => groups = Some(found),
                None => break,
            }
        } else {
            break;
        }
        top -= 1;
    }
    // A label beside one header row is only a line above the table.
    if groups.is_none() {
        return start;
    }
    for (from, to, text) in groups.into_iter().flatten() {
        for (column, &(x0, x1)) in columns.iter().enumerate().take(to + 1).skip(from) {
            match table
                .cells
                .iter_mut()
                .find(|c| c.row == 0 && c.column <= column && column < c.column + c.column_span)
            {
                Some(cell) if !cell.text.starts_with(&text) => {
                    cell.text = format!("{text} {}", cell.text);
                }
                Some(_) => {}
                None => table.cells.push(Cell {
                    row: 0,
                    column,
                    column_span: 1,
                    text: text.clone(),
                    bbox: Rect::new(x0, 0.0, x1, 0.0),
                    confidence: 1.0,
                }),
            }
        }
    }
    if let Some(text) = label {
        table.cells.push(Cell {
            row: 0,
            column: 0,
            column_span: 1,
            text,
            bbox: Rect::new(first_x0, 0.0, first_x1, 0.0),
            confidence: 1.0,
        });
    }
    table.cells.sort_by_key(|c| (c.row, c.column));
    top
}

/// The columns each cell of a group row stands over: the narrowest run of
/// them whose stretch covers it and is centred on it, one run after the
/// other. `None` unless there are two cells at least, every one finds its
/// run, one of them spans two columns or more and none spans them all —
/// else the line is not a row of groups but a caption.
fn grouped(row: &[Candidate], columns: &[(f32, f32)]) -> Option<Vec<(usize, usize, String)>> {
    let n = columns.len();
    // Where each column's stretch begins and ends: halfway across the gaps.
    let bound = |i: usize| -> f32 {
        if i == 0 {
            columns[0].0 - (columns[0].1 - columns[0].0)
        } else if i == n {
            columns[n - 1].1 + (columns[n - 1].1 - columns[n - 1].0)
        } else {
            (columns[i - 1].1 + columns[i].0) / 2.0
        }
    };
    let mut out = Vec::new();
    let mut next = 0usize;
    for candidate in row {
        let bbox = &candidate.cell.bbox;
        let off = |a: usize, b: usize| ((bound(a) + bound(b + 1)) / 2.0 - bbox.center_x()).abs();
        let width = |a: usize, b: usize| bound(b + 1) - bound(a);
        let covering: Vec<(usize, usize)> = (next..n)
            .flat_map(|a| (a..n).map(move |b| (a, b)))
            .filter(|&(a, b)| bound(a) <= bbox.x0 + 1.0 && bound(b + 1) >= bbox.x1 - 1.0)
            .collect();
        // The narrowest run centred on it; a wide one may be centred on it
        // by chance.
        let best = covering
            .iter()
            .copied()
            .filter(|&(a, b)| off(a, b) <= width(a, b) * 0.25)
            .min_by(|&(a, b), &(c, d)| width(a, b).total_cmp(&width(c, d)))
            .or_else(|| {
                covering
                    .iter()
                    .copied()
                    .min_by(|&(a, b), &(c, d)| off(a, b).total_cmp(&off(c, d)))
            })?;
        next = best.1 + 1;
        out.push((best.0, best.1, candidate.cell.text.clone()));
    }
    // A line over every column, or a single line over some, is the table's
    // caption; groups come two at least.
    (out.len() >= 2
        && out.iter().any(|&(a, b, _)| b > a)
        && out.iter().all(|&(a, b, _)| b + 1 - a < n))
    .then_some(out)
}

/// Whether a line of one cell sits in one of the table's columns: a row
/// whose other cells are empty. It lies under a cell of the table, and every
/// cell it reaches under lies in one column with the others — they all share
/// a stretch of the page.
fn in_a_column(row: &[Candidate], table: &[Vec<Candidate>]) -> bool {
    let [alone] = row else {
        return false;
    };
    let bbox = &alone.cell.bbox;
    let over: Vec<&Rect> = table
        .iter()
        .flatten()
        .map(|c| &c.cell.bbox)
        .filter(|c| c.horizontal_overlap(bbox) > 0.0)
        .collect();
    let shared = over.iter().map(|c| c.x0).fold(f32::MIN, f32::max)
        < over.iter().map(|c| c.x1).fold(f32::MAX, f32::min);
    shared
        && over
            .iter()
            .any(|c| c.horizontal_overlap(bbox) >= 0.5 * c.width().min(bbox.width()))
}

/// Whether a cell holds a figure — an amount, a date, a phone number — rather
/// than words.
fn figure(text: &str) -> bool {
    let text = text.trim();
    text.chars().any(|c| c.is_ascii_digit())
        && text
            .chars()
            .all(|c| c.is_ascii_digit() || c.is_whitespace() || ".,:;-–/+%€$£()'".contains(c))
}

/// The cell of `above` each cell of `row` lies under, when there is exactly
/// one for every cell, a different one for each, and no figure lies under a
/// figure: figures do not wrap, so a second one under the first is a new
/// row's.
fn under_one_each<'a>(row: &[Candidate], above: &'a [Candidate]) -> Option<Vec<&'a Candidate>> {
    if row.is_empty() {
        return None;
    }
    row.iter()
        .map(|candidate| {
            let mut over = above
                .iter()
                .filter(|a| a.cell.bbox.horizontal_overlap(&candidate.cell.bbox) > 0.0);
            match (over.next(), over.next()) {
                (Some(upper), None)
                    if !(figure(&upper.cell.text) && figure(&candidate.cell.text)) =>
                {
                    Some(upper)
                }
                _ => None,
            }
        })
        .collect::<Option<Vec<_>>>()
        // Each cell carries on a cell of its own: cells under one wide cell
        // are a row under a title.
        .filter(|uppers| {
            uppers
                .iter()
                .enumerate()
                .all(|(k, a)| uppers[..k].iter().all(|b| !std::ptr::eq(*a, *b)))
        })
}

/// Whether `upper` had filled its column when the line broke: the first word
/// of `next` would not have fit between its end and where the next column
/// starts. The last column ends where its widest cell does.
fn filled(upper: &Candidate, next: &Candidate, table: &[Vec<Candidate>], text_height: f32) -> bool {
    let cell = &upper.cell;
    // The column's text reaches as far as its widest line, and no further
    // than a line short of where the next column starts.
    let widest = table
        .iter()
        .flatten()
        .filter(|c| c.cell.bbox.horizontal_overlap(&cell.bbox) > 0.0)
        .map(|c| c.cell.bbox.x1)
        .fold(cell.bbox.x1, f32::max);
    let right = table
        .iter()
        .flatten()
        .map(|c| c.cell.bbox.x0)
        .filter(|&x0| x0 > widest)
        .reduce(f32::min)
        .map_or(widest, |x0| {
            (x0 - text_height).min(widest + text_height * 0.5)
        });
    let word = next.words.first().map_or_else(
        || {
            let text = &next.cell.text;
            let chars = text.chars().count().max(1) as f32;
            let first_word = text.split_whitespace().next().unwrap_or("");
            next.cell.bbox.width() * first_word.chars().count() as f32 / chars
        },
        |word| word.bbox.width(),
    );
    cell.bbox.x1 + text_height * 0.25 + word > right
}

/// Which rows of a run carry on the row above (see [`carries_on`]).
fn continuations(
    rows: &[Vec<Candidate>],
    lines: &[Line],
    ruled: &[bool],
    text_height: f32,
) -> Vec<bool> {
    (0..rows.len())
        .map(|i| carries_on(rows, lines, ruled, i, text_height))
        .collect()
}

/// Whether row `i` of a run is the next line of the row above rather than a
/// row of its own. A rule drawn between them ends the row; in a table that
/// rules off its rows, a line with none above it carries the row on.
/// Otherwise it is set right under the line above, and each of its cells lies
/// under one cell of it. It carries the row on when, leaving some column
/// empty, each of its cells finishes one above that had filled its column or
/// ended in a word broken with a hyphen; or when the table's rows stand
/// clearly further apart than this line does from the one above (a line break
/// inside a cell).
fn carries_on(
    rows: &[Vec<Candidate>],
    lines: &[Line],
    ruled: &[bool],
    i: usize,
    text_height: f32,
) -> bool {
    carries_on_row(rows, lines, ruled, i, text_height, true)
}

/// [`carries_on`], looking at rows set at their foot only when `bottom`.
fn carries_on_row(
    rows: &[Vec<Candidate>],
    lines: &[Line],
    ruled: &[bool],
    i: usize,
    text_height: f32,
    bottom: bool,
) -> bool {
    if i == 0 || ruled[i] {
        return false;
    }
    let gap = |j: usize| lines[j].bbox.y0 - lines[j - 1].bbox.y1;
    if gap(i) > text_height * CONTINUATION_GAP {
        return false;
    }
    let rules_off = ruled.iter().skip(1).filter(|&&r| r).count();
    if rules_off >= 2 && rules_off * 3 >= rows.len() - 1 {
        return true;
    }
    // A row whose one-line cells are centred beside a cell that wraps comes
    // as lines set between each other's: the one-line cells overlap the
    // wrapped cell's lines by half a line, where two rows never overlap.
    // Not the run's first line: a label centred beside a header of two rows
    // is the header's, which is read above the run. Lines whose cells stand
    // in other columns need less to tell: a raised superscript stretches a
    // line's box a quarter of a line into the one above.
    let (upper, lower) = (&lines[i - 1].bbox, &lines[i].bbox);
    let overlap = upper.vertical_overlap(lower) / upper.height().min(lower.height()).max(1.0);
    let apart = rows[i].iter().all(|c| {
        rows[i - 1]
            .iter()
            .all(|u| u.cell.bbox.horizontal_overlap(&c.cell.bbox) <= 0.0)
    });
    let interleaved = i >= 2 && (overlap >= INTERLEAVED || (apart && overlap >= INTERLEAVED_APART));
    if interleaved {
        return true;
    }
    // A row set at its foot, as a spreadsheet sets it: the line above holds
    // only the first lines of the few cells that wrap, each going on — in
    // words, not figures, and in lower case or finishing a broken word — in
    // a cell of this row's. Unless that line carries on the row above it:
    // then it is the end of that row's cell, not the start of this one's.
    let goes_on = |upper: &Candidate, lower: &Candidate| {
        let text = lower.cell.text.trim_start();
        !figure(text)
            && (broken_word(&upper.cell.text, text)
                || (text.chars().next().is_some_and(char::is_lowercase)
                    && filled(upper, lower, rows, text_height)))
    };
    if bottom
        && rows[i - 1].len() * 2 <= rows[i].len()
        && !carries_on_row(rows, lines, ruled, i - 1, text_height, false)
        && under_one_each(&rows[i - 1], &rows[i]).is_some_and(|lowers| {
            rows[i - 1]
                .iter()
                .zip(&lowers)
                .all(|(upper, lower)| goes_on(upper, lower))
        })
    {
        return true;
    }
    let Some(uppers) = under_one_each(&rows[i], &rows[i - 1]) else {
        return false;
    };
    let left = rows
        .iter()
        .flatten()
        .map(|c| c.cell.bbox.x0)
        .fold(f32::INFINITY, f32::min);
    let first = |row: &[Candidate]| -> Option<usize> {
        row.iter()
            .position(|c| c.cell.bbox.x0 <= left + text_height)
    };
    let opens = first(&rows[i]).is_some();
    if opens && first(&rows[i - 1]).is_none() {
        return false;
    }

    // A line with a cell in every column is a row, however full the cell
    // above it looks: in a table set tight, the columns crowd each other.
    // Each cell has to finish the one above it: an e-mail address under
    // another is the next row's, with its phone number left empty.
    let fullest = rows.iter().map(Vec::len).max().unwrap_or(0);
    if rows[i].len() < fullest
        && rows[i].iter().zip(&uppers).all(|(cell, upper)| {
            broken_word(&upper.cell.text, &cell.cell.text) || filled(upper, cell, rows, text_height)
        })
    {
        return true;
    }
    // How far apart the rows stand: the upper quarter of the gaps above lines
    // that start in the first column, which are rows more often than not.
    let mut row_gaps: Vec<f32> = (1..rows.len())
        .filter(|&j| j != i && first(&rows[j]).is_some())
        .map(gap)
        .collect();
    row_gaps.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let usual = row_gaps.get(row_gaps.len() * 3 / 4).copied().unwrap_or(0.0);
    usual > text_height * CONTINUATION_GAP && gap(i) < usual * 0.5
}

/// The straight lines a page draws, as they bear on its tables: those across
/// it, which end rows, and those down it, which stand between columns.
#[derive(Default)]
struct Rules {
    /// Across the page, sorted by height.
    across: Vec<Rect>,
    down: Vec<Rect>,
}

impl Rules {
    /// Keeps the rules at most half a line thick and at least most of a line
    /// long: shorter strokes are a letter's or a mark's.
    fn of(rules: &[Rect], text_height: f32) -> Self {
        let (thin, long) = (text_height * 0.5, text_height * 0.8);
        let mut across: Vec<Rect> = rules
            .iter()
            .filter(|r| r.height() <= thin && r.width() >= long)
            .copied()
            .collect();
        across.sort_by(|a, b| a.center_y().total_cmp(&b.center_y()));
        let down = rules
            .iter()
            .filter(|r| r.width() <= thin && r.height() >= long)
            .copied()
            .collect();
        Rules { across, down }
    }

    /// For each line, whether a rule runs between it and the line before,
    /// under one of its cells.
    fn above(&self, lines: &[Line], rows: &[Vec<Candidate>]) -> Vec<bool> {
        (0..lines.len())
            .map(|k| {
                k > 0
                    && rows[k]
                        .iter()
                        .any(|c| self.between(&lines[k - 1].bbox, &lines[k].bbox, &c.cell.bbox))
            })
            .collect()
    }

    /// Whether a rule runs between `upper` and `lower` under at least half of
    /// `span`. Between means below the upper line's descenders, where its
    /// underlines are not, and above the lower line's capitals.
    fn between(&self, upper: &Rect, lower: &Rect, span: &Rect) -> bool {
        let top = upper.y1 - upper.height() / 12.0;
        let bottom = lower.y0 + lower.height() / 4.0;
        if bottom < top || span.width() <= 0.0 {
            return false;
        }
        let from = self.across.partition_point(|r| r.center_y() < top);
        self.across[from..]
            .iter()
            .take_while(|r| r.center_y() <= bottom)
            .any(|r| r.horizontal_overlap(span) >= 0.5 * span.width())
    }

    /// Cuts cells where a rule down the page stands between two of their
    /// words: two cells of a table drawn with its borders, however close.
    fn split(&self, row: Vec<Candidate>, line: &Line) -> Vec<Candidate> {
        let y = line.bbox.center_y();
        let standing: Vec<f32> = self
            .down
            .iter()
            .filter(|r| r.y0 <= y && r.y1 >= y)
            .map(Rect::center_x)
            .collect();
        if standing.is_empty() {
            return row;
        }
        let mut out = Vec::new();
        for candidate in row {
            let mut pieces = Vec::new();
            let mut run: Vec<&crate::doc::Word> = Vec::new();
            for word in &candidate.words {
                let parted = run.last().is_some_and(|previous| {
                    standing
                        .iter()
                        .any(|&x| x > previous.bbox.x1 - 0.5 && x < word.bbox.x0 + 0.5)
                });
                if parted {
                    pieces.push(self::candidate(&run, candidate.cell.bbox));
                    run.clear();
                }
                run.push(word);
            }
            if pieces.is_empty() {
                out.push(candidate);
            } else {
                pieces.push(self::candidate(&run, candidate.cell.bbox));
                out.extend(pieces);
            }
        }
        out
    }

    /// The columns a table's borders draw: the stretches between the rules
    /// that stand down the page beside `lines`, which hold `rows`, and that
    /// hold a cell. `None` when fewer than three rules stand there or a cell
    /// sits outside them. A table set to the page's width has its borders far
    /// from its text, so they are not looked for near it.
    fn columns(
        &self,
        lines: &[Line],
        rows: &[Vec<Candidate>],
        text_height: f32,
    ) -> Option<Vec<(f32, f32)>> {
        let (top, bottom) = (
            lines.first()?.bbox.center_y(),
            lines.last()?.bbox.center_y(),
        );
        let mut xs: Vec<f32> = self
            .down
            .iter()
            .filter(|r| r.y0 < bottom && r.y1 > top)
            .map(Rect::center_x)
            .collect();
        xs.sort_by(f32::total_cmp);
        xs.dedup_by(|a, b| *a - *b <= text_height * 0.5);
        if xs.len() < MIN_COLUMNS + 1 {
            return None;
        }
        let inside = rows
            .iter()
            .flatten()
            .all(|c| c.cell.bbox.center_x() > xs[0] && c.cell.bbox.center_x() < xs[xs.len() - 1]);
        let columns: Vec<(f32, f32)> = xs
            .windows(2)
            .map(|pair| (pair[0], pair[1]))
            .filter(|&(x0, x1)| {
                rows.iter()
                    .flatten()
                    .any(|c| c.cell.bbox.center_x() > x0 && c.cell.bbox.center_x() < x1)
            })
            .collect();
        (inside && (MIN_COLUMNS..=MAX_COLUMNS).contains(&columns.len())).then_some(columns)
    }

    /// Whether rules stand either side of `cell` at its height: a cell of a
    /// table drawn with its borders.
    fn boxed(&self, cell: &Rect) -> bool {
        let (y, slack) = (cell.center_y(), cell.height() * 0.5);
        let side = |left: bool| {
            self.down.iter().any(|r| {
                let x = r.center_x();
                r.y0 <= y
                    && r.y1 >= y
                    && if left {
                        x <= cell.x0 + slack
                    } else {
                        x >= cell.x1 - slack
                    }
            })
        };
        side(true) && side(false)
    }
}

/// The gaps between one row's cells.
fn gutters_of(cells: &[Candidate]) -> Vec<(f32, f32)> {
    cells
        .windows(2)
        .map(|pair| (pair[0].cell.bbox.x1, pair[1].cell.bbox.x0))
        .filter(|(from, to)| to > from)
        .collect()
}

/// Whether two rows leave their gaps in the same places.
///
/// Every gap of the row with fewer of them has to line up with one of the
/// other's. A row of a table may merge two columns — a header often does — but it
/// cannot put a gap where the table has none.
///
/// A row that leaves a cell empty has its gaps elsewhere: where the empty
/// cell is, one wide gap, or none before its first cell. Such rows align too
/// when they share a gap and neither sets a cell across the middle of one of
/// the other's.
fn aligns(a: &[Candidate], b: &[Candidate]) -> bool {
    let (ga, gb) = (gutters_of(a), gutters_of(b));
    let (fewer, more) = if ga.len() <= gb.len() {
        (&ga, &gb)
    } else {
        (&gb, &ga)
    };
    if fewer.is_empty() {
        return false;
    }
    let meets = |(from, to): (f32, f32), gaps: &[(f32, f32)]| {
        gaps.iter().any(|&(f, t)| to.min(t) > from.max(f))
    };
    if fewer.iter().all(|&gap| meets(gap, more)) {
        return true;
    }
    let crosses = |cells: &[Candidate], gaps: &[(f32, f32)]| {
        gaps.iter().any(|&(from, to)| {
            let middle = (from + to) / 2.0;
            cells
                .iter()
                .any(|c| c.cell.bbox.x0 < middle && c.cell.bbox.x1 > middle)
        })
    };
    ga.iter().any(|&gap| meets(gap, &gb)) && !crosses(a, &gb) && !crosses(b, &ga)
}

/// Whether a single-cell row covers the width the table has reached.
fn spans_the_row(row: &[Candidate], sofar: &[Vec<Candidate>]) -> bool {
    let Some(candidate) = row.first() else {
        return false;
    };
    let Some(extent) = extent_of(sofar) else {
        return false;
    };
    row.len() == 1 && candidate.cell.bbox.width() >= extent.width() * SPANNING_SHARE
}

fn extent_of(rows: &[Vec<Candidate>]) -> Option<Rect> {
    rows.iter()
        .flatten()
        .map(|candidate| candidate.cell.bbox)
        .reduce(|a, b| a.union(&b))
}

/// Works out a run's columns, or decides the run is not a table after all.
fn columns_of(rows: &[Vec<Candidate>], gutter: f32) -> Option<Vec<(f32, f32)>> {
    let extent = extent_of(rows)?;
    if extent.width() <= 0.0 {
        return None;
    }
    if !multi_cell_rows(rows) {
        return None;
    }
    // The columns are what the table's fullest rows say they are. A summary row's
    // label — `Mehrwertsteuer 19 %` against a column of quantities — genuinely
    // reaches across two of them, and letting it vote would merge the two columns
    // it reaches across.
    let defining = defining_rows(rows);
    let columns = column_bands(
        rows.iter()
            .filter(|row| row.len() >= defining)
            .flatten()
            .map(|candidate| &candidate.cell)
            .filter(|cell| !wide(&cell.bbox, extent)),
        gutter,
    );
    (MIN_COLUMNS..=MAX_COLUMNS)
        .contains(&columns.len())
        .then_some(columns)
}

/// A run's rows whole: each with the cells of the lines that carry it on,
/// joined into the cell they go on or set beside the others. A row whose
/// one-line cells stand between a wrapped cell's lines has all its cells.
fn whole_rows(rows: &[Vec<Candidate>], continues: &[bool]) -> Vec<Vec<Candidate>> {
    let mut out: Vec<Vec<Candidate>> = Vec::new();
    for (row, &carries_on) in rows.iter().zip(continues) {
        let Some(whole) = out.last_mut().filter(|_| carries_on) else {
            out.push(row.clone());
            continue;
        };
        for candidate in row {
            let over = whole
                .iter_mut()
                .find(|c| c.cell.bbox.horizontal_overlap(&candidate.cell.bbox) > 0.0);
            match over {
                Some(cell) => {
                    cell.cell.text = join_wrapped(&cell.cell.text, &candidate.cell.text);
                    cell.cell.bbox = cell.cell.bbox.union(&candidate.cell.bbox);
                    cell.words.extend(candidate.words.iter().cloned());
                }
                None => whole.push(candidate.clone()),
            }
        }
        whole.sort_by(|a, b| a.cell.bbox.x0.total_cmp(&b.cell.bbox.x0));
    }
    out
}

/// Whether enough of a run's rows carry more than one cell to be a table's.
fn multi_cell_rows(rows: &[Vec<Candidate>]) -> bool {
    let multi = rows.iter().filter(|row| row.len() >= MIN_COLUMNS).count();
    multi as f32 >= rows.len() as f32 * MIN_MULTI_CELL_SHARE
}

/// Whether the row above a table is its header.
///
/// It is, when the columns cut it into cells in two or more of them: titles
/// sitting over the columns is what a header is. A sentence is not cut at all,
/// because its words do not happen to stop where the columns do — which is the
/// whole test, and the reason this can be as simple as it is.
///
/// Nothing is asked about how far right it reaches: `Gesamt` over a column of
/// figures is wider than any of them, and so is a title over a column of names.
/// A line starting a gutter further left than the table does is the page around
/// it, not its header.
fn adopts(row: &[Candidate], columns: &[(f32, f32)], gutter: f32) -> bool {
    let Some(first) = columns.first() else {
        return false;
    };
    let left = first.0 - gutter;
    if row.is_empty() || row.iter().any(|c| c.cell.bbox.x0 < left) {
        return false;
    }
    let pieces: Vec<Segment> = row
        .iter()
        .flat_map(|candidate| candidate.cut_at(columns, gutter))
        .collect();
    if pieces.len() < MIN_COLUMNS {
        return false;
    }
    let mut columns_used: Vec<usize> = pieces
        .iter()
        .map(|piece| place(&piece.bbox, columns).0)
        .collect();
    columns_used.sort_unstable();
    columns_used.dedup();
    columns_used.len() >= MIN_COLUMNS
}

/// Whether a cell is wide enough to be spanning the table rather than filling a
/// column of it.
fn wide(bbox: &Rect, extent: Rect) -> bool {
    bbox.width() >= extent.width() * SPANNING_SHARE
}

/// Lays a run of rows out over known columns.
///
/// `header` says the first row was adopted from above, which means the columns
/// already cut it into titles and it must be cut even though it runs the width of
/// the table. Any other row that wide, alone on its line, is a title or a note
/// and stays whole.
fn grid(
    rows: &[Vec<Candidate>],
    continues: &[bool],
    columns: &[(f32, f32)],
    gutter: f32,
    header: bool,
) -> Table {
    let extent = extent_of(rows).unwrap_or_default();
    let mut cells: Vec<Cell> = Vec::new();
    // Where each (row, column) cell is, so a wrapped cell's next line joins it.
    let mut placed: std::collections::HashMap<(usize, usize), usize> =
        std::collections::HashMap::new();
    let mut row = 0usize;
    for (index, row_cells) in rows.iter().enumerate() {
        if index > 0 && !continues.get(index).copied().unwrap_or(false) {
            row += 1;
        }
        for candidate in row_cells {
            // The columns are better evidence than the gap threshold that found
            // them: a header setting two column titles a word space apart is cut
            // where the column below them is. A wide cell alone on its row is
            // left whole — a title is not a row of cells.
            let alone = row_cells.len() == 1 && !(header && row == 0);
            let pieces = if alone && wide(&candidate.cell.bbox, extent) {
                vec![candidate.cell.clone()]
            } else {
                candidate.cut_at(columns, gutter)
            };
            for piece in pieces {
                let (column, span) = place(&piece.bbox, columns);
                if let Some(&at) = placed.get(&(row, column)) {
                    let cell = &mut cells[at];
                    cell.text = join_wrapped(&cell.text, &piece.text);
                    cell.bbox = cell.bbox.union(&piece.bbox);
                    cell.confidence = cell.confidence.min(piece.confidence);
                    continue;
                }
                placed.insert((row, column), cells.len());
                cells.push(Cell {
                    row,
                    column,
                    column_span: span,
                    text: piece.text,
                    bbox: piece.bbox,
                    confidence: piece.confidence,
                });
            }
        }
    }
    cells.sort_by_key(|c| (c.row, c.column));
    Table {
        rows: if rows.is_empty() { 0 } else { row + 1 },
        columns: columns.len(),
        cells,
    }
}

/// A cell's text and the next line of it: a word broken at the line end with a
/// hyphen (`Instandhal-` / `tung`) is one word again.
fn join_wrapped(first: &str, next: &str) -> String {
    let compound = first.ends_with('-')
        && first.chars().rev().nth(1).is_some_and(char::is_alphabetic)
        && (next.chars().next().is_some_and(char::is_uppercase)
            || (crate::layout::abbreviation_before_hyphen(first)
                && !crate::layout::continues_a_suspended_hyphen(next)));
    let unspaced = first.chars().last().is_some_and(crate::layout::unspaced)
        && next.chars().next().is_some_and(crate::layout::unspaced);
    if broken_word(first, next) {
        format!("{}{}", &first[..first.len() - 1], next)
    } else if compound || unspaced {
        // "E-Mail-" over "Adresse": the compound's own hyphen stays; and
        // Chinese or Japanese goes on without a space.
        format!("{first}{next}")
    } else {
        format!("{first} {next}")
    }
}

/// Whether `first` ends in a word broken with a hyphen that `next` finishes —
/// not a suspended one, `Vor-` over `und Nachname`.
fn broken_word(first: &str, next: &str) -> bool {
    let mut chars = first.trim_end().chars().rev();
    first.ends_with('-')
        && !crate::layout::abbreviation_before_hyphen(first)
        && chars.next() == Some('-')
        && chars.next().is_some_and(char::is_alphabetic)
        && next
            .trim_start()
            .chars()
            .next()
            .is_some_and(char::is_lowercase)
        && !crate::layout::continues_a_suspended_hyphen(next.trim_start())
}

/// How many cells a row needs before it helps define the columns.
///
/// The most the table has, as long as it has at least two such rows; a single row
/// the recognizer split once too often must not get to draw the grid on its own.
fn defining_rows(rows: &[Vec<Candidate>]) -> usize {
    let most = rows.iter().map(|row| row.len()).max().unwrap_or(0);
    (MIN_COLUMNS..=most)
        .rev()
        .find(|&k| rows.iter().filter(|row| row.len() >= k).count() >= MIN_COLUMN_ROWS)
        .unwrap_or(MIN_COLUMNS)
}

/// The table's columns: the stretches that row after row puts something in.
///
/// Counting where the ink is, rather than where it is not, is what makes this
/// robust. A stretch belongs to a column when at least two rows have a cell over
/// it — one cell in one place is where a sentence happened to be split, and a
/// header that sets two column titles close enough to read as one cell does not
/// erase the gutter beneath it, because one cell is not two rows. Stretches
/// closer together than a gutter are the same column, since a column's cells
/// have ragged edges.
fn column_bands<'a>(cells: impl Iterator<Item = &'a Segment>, min_gutter: f32) -> Vec<(f32, f32)> {
    let spans: Vec<(f32, f32)> = cells.map(|c| (c.bbox.x0, c.bbox.x1)).collect();
    if spans.is_empty() {
        return Vec::new();
    }

    let mut edges: Vec<f32> = spans.iter().flat_map(|&(a, b)| [a, b]).collect();
    edges.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    edges.dedup();

    // Every stretch between two edges is either part of a column or not.
    let mut cores: Vec<(f32, f32)> = Vec::new();
    for pair in edges.windows(2) {
        let (from, to) = (pair[0], pair[1]);
        if to <= from {
            continue;
        }
        let middle = (from + to) / 2.0;
        let rows = spans
            .iter()
            .filter(|&&(a, b)| a <= middle && middle <= b)
            .count();
        if rows < MIN_COLUMN_ROWS {
            continue;
        }
        match cores.last_mut() {
            // Touching, or nearer than a gutter: the same column.
            Some(last) if from - last.1 < min_gutter => last.1 = to,
            _ => cores.push((from, to)),
        }
    }
    cores
}

/// The column a cell starts in and how many it covers.
///
/// Measured against the columns themselves — the stretches the table actually
/// puts ink in — and not against boundaries drawn halfway between them. A
/// quantity column three characters wide is a narrow stretch with a lot of white
/// space either side, and a cell in the white space belongs to the column it is
/// nearest, not to whichever side of an invented line it fell.
fn place(bbox: &Rect, columns: &[(f32, f32)]) -> (usize, usize) {
    let width = bbox.width().max(1.0);
    let overlap = |&(x0, x1): &(f32, f32)| (bbox.x1.min(x1) - bbox.x0.max(x0)).max(0.0);
    // Against the narrower of the two: a three-character column of quantities is
    // a fraction of the label beside it, and a label spanning the table covers
    // that column completely while barely denting its own width.
    let enough = |column: &(f32, f32)| {
        let reference = width.min((column.1 - column.0).max(1.0));
        overlap(column) >= reference * MIN_CELL_OVERLAP
    };

    let mut first = None;
    let mut last = 0usize;
    for (index, column) in columns.iter().enumerate() {
        if enough(column) {
            first.get_or_insert(index);
            last = index;
        }
    }
    match first {
        Some(start) => (start, last - start + 1),
        // A cell that reaches into nothing sits in the white space beside a
        // column: it belongs to the one it is nearest.
        None => {
            let nearest = columns
                .iter()
                .enumerate()
                .min_by(|(_, a), (_, b)| {
                    distance(bbox, a)
                        .partial_cmp(&distance(bbox, b))
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(i, _)| i)
                .unwrap_or(0);
            (nearest, 1)
        }
    }
}

/// A cell as the first pass found it, with the words it was built from.
///
/// The words are kept because the columns, once known, say better than any gap
/// threshold where the cuts belong.
#[derive(Clone)]
struct Candidate {
    cell: Segment,
    words: Vec<crate::doc::Word>,
}

impl Candidate {
    /// Cuts this cell where the columns say two of its words belong apart.
    ///
    /// A cell with one word is never cut: a word reaching over a column boundary
    /// is still one word. Nor is a gap no wider than a word space — that is what
    /// keeps `Mehrwertsteuer 19 %` one label although its `19` sits over the
    /// column of quantities.
    fn cut_at(&self, columns: &[(f32, f32)], gutter: f32) -> Vec<Segment> {
        if self.words.len() < 2 {
            return vec![self.cell.clone()];
        }
        // The gutter was measured at twice the document's word space, so this
        // recovers the space and asks for a little more: a cut needs a gap wider
        // than the ones inside a phrase, and the columns decide the rest.
        let space = gutter / GUTTER_OVER_SPACE * RECUT_OVER_SPACE;
        let mut pieces = Vec::new();
        let mut run: Vec<&crate::doc::Word> = vec![&self.words[0]];
        for word in &self.words[1..] {
            let previous = run[run.len() - 1];
            let wide_enough = word.bbox.x0 - previous.bbox.x1 >= space;
            // Both words have to sit in a column of their own. A `%` in the white
            // space between two columns belongs to the label it trails, not to
            // whichever column it happens to be nearest.
            let apart = match (
                anchored_column(&previous.bbox, columns),
                anchored_column(&word.bbox, columns),
            ) {
                (Some(here), Some(there)) => here != there,
                _ => false,
            };
            if wide_enough && apart {
                pieces.push(join(&run, self.cell.bbox));
                run.clear();
            }
            run.push(word);
        }
        pieces.push(join(&run, self.cell.bbox));
        pieces
    }
}

/// The column a cell sits in, or `None` when it sits between columns.
///
/// Unlike [`place`], which always answers, because every cell has to go somewhere.
fn anchored_column(bbox: &Rect, columns: &[(f32, f32)]) -> Option<usize> {
    let width = bbox.width().max(1.0);
    columns.iter().position(|column| {
        let overlap = (bbox.x1.min(column.1) - bbox.x0.max(column.0)).max(0.0);
        let reference = width.min((column.1 - column.0).max(1.0));
        overlap >= reference * MIN_CELL_OVERLAP
    })
}

/// How far a cell is from a column, zero when they overlap.
fn distance(bbox: &Rect, &(x0, x1): &(f32, f32)) -> f32 {
    if bbox.x1 < x0 {
        x0 - bbox.x1
    } else if bbox.x0 > x1 {
        bbox.x0 - x1
    } else {
        0.0
    }
}

/// One line's cells: runs of words with more than a word space between them.
///
/// The detector's own boxes are the starting point — a row whose cells it kept
/// apart arrives as several [`Segment`]s — and each of those is split further
/// where the words inside it leave a gap, which is what recovers a table the
/// detector read as one box per row. Where there are words, they also give the
/// cell's own edges: a detection box carries padding, and a box 260 pixels wide
/// around one 190-pixel word reaches into the next column. With word boxes turned
/// off there is nothing to split by and the detector's boxes are all there is.
fn cells_of(line: &Line, gutter: f32) -> Vec<Candidate> {
    let parts = line.parts();
    if line.words.is_empty() {
        return parts
            .into_iter()
            .map(|cell| Candidate {
                cell,
                words: Vec::new(),
            })
            .collect();
    }
    let mut out = Vec::new();
    for part in parts {
        let mut words: Vec<&crate::doc::Word> = line
            .words
            .iter()
            .filter(|w| {
                w.bbox.x0 >= part.bbox.x0 - 1.0
                    && w.bbox.x1 <= part.bbox.x1 + 1.0
                    && w.bbox.vertical_overlap(&part.bbox) > 0.0
            })
            .collect();
        if words.is_empty() {
            out.push(Candidate {
                cell: part,
                words: Vec::new(),
            });
            continue;
        }
        words.sort_by(|a, b| {
            a.bbox
                .x0
                .partial_cmp(&b.bbox.x0)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut run: Vec<&crate::doc::Word> = vec![words[0]];
        for word in &words[1..] {
            let previous = run[run.len() - 1];
            if word.bbox.x0 - previous.bbox.x1 >= gutter {
                out.push(candidate(&run, part.bbox));
                run.clear();
            }
            run.push(word);
        }
        out.push(candidate(&run, part.bbox));
    }
    out
}

fn candidate(run: &[&crate::doc::Word], fallback: Rect) -> Candidate {
    Candidate {
        cell: join(run, fallback),
        words: run.iter().map(|w| (*w).clone()).collect(),
    }
}

/// One run of words as a cell.
fn join(run: &[&crate::doc::Word], fallback: Rect) -> Segment {
    let bbox = run
        .iter()
        .map(|w| w.bbox)
        .reduce(|a, b| a.union(&b))
        .unwrap_or(fallback);
    Segment {
        text: run
            .iter()
            .map(|w| w.text.as_str())
            .collect::<Vec<_>>()
            .join(" "),
        bbox,
        confidence: run.iter().map(|w| w.confidence).sum::<f32>() / run.len().max(1) as f32,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cell_broken_over_two_lines_is_joined_whole() {
        assert_eq!(
            join_wrapped("Instandhal-", "tung der Anlagen"),
            "Instandhaltung der Anlagen"
        );
        assert_eq!(join_wrapped("Vor-", "und Nachname"), "Vor- und Nachname");
        assert_eq!(join_wrapped("Schmidt,", "Weber"), "Schmidt, Weber");
        assert_eq!(join_wrapped("E-Mail-", "Adresse"), "E-Mail-Adresse");
        assert_eq!(
            join_wrapped("für IT-", "basierte Dienste"),
            "für IT-basierte Dienste"
        );
        assert_eq!(join_wrapped("IT-", "und TK-Anlagen"), "IT- und TK-Anlagen");
        assert_eq!(
            join_wrapped("项目的下一阶段", "将于四月开始"),
            "项目的下一阶段将于四月开始"
        );
        assert!(figure("4,90") && figure("030 1234") && figure("12.03.2026"));
        assert!(!figure("Muttern M4") && !figure("12.06. Hinweis:"));
    }
    use crate::doc::Word;
    use crate::geom::Quad;

    const TEXT_HEIGHT: f32 = 10.0;

    /// Every table in `lines`, with the gutter measured from them.
    fn find_here(lines: &[Line]) -> Vec<Found> {
        find(lines, TEXT_HEIGHT, gutter_width(lines, TEXT_HEIGHT), &[])
    }

    #[test]
    fn a_header_row_is_never_taken_from_the_table_above() {
        // Rows of a two-column newsletter as the recognizer read them: the
        // first run of rows has two columns, the next one six, and the last
        // row of the first run fits the second's columns well enough to pass
        // for its header. Taking it made the runs overlap, and splitting the
        // lines by index panicked ("split index (is 5) should be <= len (is 4)").
        let cells: [&[(&str, f32, f32)]; 8] = [
            &[
                ("Messe", 156.0, 259.0),
                ("Logistik aus Bremen.", 860.0, 1134.0),
            ],
            &[
                ("Auf der Hannover Messe", 149.0, 791.0),
                ("Kantine", 870.0, 1006.0),
            ],
            &[
                ("erstmals die Funktion", 149.0, 792.0),
                ("Anmeldungen nimmt", 860.0, 1503.0),
            ],
            &[
                ("Messdatenplattform;", 154.0, 415.0),
                ("im", 472.0, 490.0),
                ("Tarnkappenmodus", 560.0, 797.0),
                ("Monatsende entgegen.", 865.0, 1503.0),
            ],
            &[
                ("bleiben die Namen", 149.0, 791.0),
                ("Ausstellung", 860.0, 1004.0),
                ("Fotos", 1063.0, 1123.0),
                ("aus", 1182.0, 1217.0),
                ("hundert", 1279.0, 1373.0),
                ("Jahren", 1428.0, 1503.0),
            ],
            &[
                ("Sommerfest findet", 149.0, 792.0),
                ("Werksgeschichte.", 867.0, 1084.0),
                ("Neue", 1152.0, 1209.0),
                ("Ladesäulen", 1276.0, 1411.0),
                ("für", 1477.0, 1512.0),
            ],
            &[
                ("Samstag im Juli", 149.0, 797.0),
                ("Elektroautos stehen", 860.0, 1509.0),
            ],
            &[
                ("gratulieren allen", 149.0, 791.0),
                ("bereit.", 858.0, 939.0),
            ],
        ];
        let lines: Vec<Line> = cells
            .iter()
            .enumerate()
            .map(|(i, row_cells)| row(100.0 + i as f32 * TEXT_HEIGHT * 1.3, row_cells))
            .collect();
        let found = find_here(&lines);
        let mut reach = 0;
        for table in &found {
            assert!(
                table.start >= reach,
                "overlap at {}..{}",
                table.start,
                table.end
            );
            reach = table.end;
        }
    }

    /// The one table in `lines`, when there is exactly one.
    fn detect(lines: &[Line], text_height: f32) -> Option<Table> {
        let mut found = find(lines, text_height, gutter_width(lines, text_height), &[]);
        (found.len() == 1).then(|| found.remove(0).table)
    }

    /// A line built from cells at the given x ranges, all on one baseline.
    fn row(y: f32, cells: &[(&str, f32, f32)]) -> Line {
        let segments: Vec<Segment> = cells
            .iter()
            .map(|(text, x0, x1)| Segment {
                text: (*text).into(),
                bbox: Rect::new(*x0, y, *x1, y + TEXT_HEIGHT),
                confidence: 0.95,
            })
            .collect();
        let bbox = segments
            .iter()
            .map(|s| s.bbox)
            .reduce(|a, b| a.union(&b))
            .unwrap();
        Line {
            text: cells
                .iter()
                .map(|(t, _, _)| *t)
                .collect::<Vec<_>>()
                .join(" "),
            confidence: 0.95,
            quad: Quad::from_rect(bbox),
            bbox,
            det_score: 0.9,
            // A single-box line has no segments to keep, which is what `parts`
            // papers over; anything with more than one does.
            segments: if segments.len() > 1 {
                segments
            } else {
                Vec::new()
            },
            ..Default::default()
        }
    }

    /// A line of one cell, built from words at the given x ranges.
    fn worded(y: f32, words: &[(&str, f32, f32)]) -> Line {
        let text: Vec<&str> = words.iter().map(|w| w.0).collect();
        let (x0, x1) = (words[0].1, words[words.len() - 1].2);
        let mut line = row(y, &[(&text.join(" "), x0, x1)]);
        line.words = words
            .iter()
            .map(|(text, x0, x1)| Word {
                text: (*text).into(),
                bbox: Rect::new(*x0, y, *x1, y + TEXT_HEIGHT),
                confidence: 0.95,
            })
            .collect();
        line
    }

    /// A rule across the page under the line at `y`, from `x0` to `x1`.
    fn across(y: f32, x0: f32, x1: f32) -> Rect {
        Rect::new(x0, y + TEXT_HEIGHT + 1.0, x1, y + TEXT_HEIGHT + 2.0)
    }

    /// A rule down the page at `x`, from `y0` to `y1`.
    fn down(x: f32, y0: f32, y1: f32) -> Rect {
        Rect::new(x, y0, x + 1.0, y1)
    }

    #[test]
    fn a_rule_between_two_lines_ends_a_row_and_none_carries_it_on() {
        // A contact list drawn with its borders and set tight: Maximilian has
        // no phone number, and Eva's street wraps onto a second line.
        let lines = vec![
            row(
                0.0,
                &[
                    ("Name", 0.0, 40.0),
                    ("Telefon", 200.0, 260.0),
                    ("E-Mail", 300.0, 350.0),
                ],
            ),
            row(
                12.0,
                &[
                    ("Eva Roth", 0.0, 60.0),
                    ("030 1234", 200.0, 260.0),
                    ("eva@example.de", 300.0, 420.0),
                ],
            ),
            row(22.0, &[("Hauptstr. 1", 0.0, 80.0)]),
            row(
                34.0,
                &[
                    ("Maximilian Berger", 0.0, 150.0),
                    ("max@example.de", 300.0, 420.0),
                ],
            ),
            row(46.0, &[("Jan Ott", 0.0, 50.0), ("030 9876", 200.0, 260.0)]),
        ];
        let rules: Vec<Rect> = [0.0, 22.0, 34.0, 46.0]
            .iter()
            .map(|&y| across(y, -5.0, 430.0))
            .chain(
                [-5.0, 190.0, 290.0, 430.0]
                    .iter()
                    .map(|&x| down(x, -2.0, 60.0)),
            )
            .collect();
        let mut found = find(&lines, TEXT_HEIGHT, 20.0, &rules);
        assert_eq!(found.len(), 1);
        let table = found.remove(0).table;
        assert_eq!(table.rows, 4);
        assert_eq!(
            table.row_text(1),
            ["Eva Roth Hauptstr. 1", "030 1234", "eva@example.de"]
        );
        assert_eq!(
            table.row_text(2),
            ["Maximilian Berger", "", "max@example.de"]
        );
        assert_eq!(table.row_text(3), ["Jan Ott", "030 9876", ""]);
    }

    #[test]
    fn a_row_centred_beside_a_cell_that_wraps_is_one_row() {
        // The description wraps onto two lines; the row's other cells stand
        // centred between them, as a browser sets a table by default.
        let lines = vec![
            row(
                0.0,
                &[
                    ("Pos.", 0.0, 30.0),
                    ("Beschreibung", 60.0, 200.0),
                    ("Betrag", 300.0, 350.0),
                ],
            ),
            row(14.0, &[("Wartung der Heizungsanlage", 60.0, 250.0)]),
            row(20.0, &[("1", 0.0, 10.0), ("340,00", 300.0, 350.0)]),
            row(26.0, &[("im Erdgeschoss", 60.0, 160.0)]),
            row(
                40.0,
                &[
                    ("2", 0.0, 10.0),
                    ("Anfahrt", 60.0, 120.0),
                    ("45,00", 305.0, 350.0),
                ],
            ),
            row(
                54.0,
                &[
                    ("3", 0.0, 10.0),
                    ("Material", 60.0, 130.0),
                    ("12,50", 305.0, 350.0),
                ],
            ),
        ];
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.rows, 4);
        assert_eq!(
            table.row_text(1),
            ["1", "Wartung der Heizungsanlage im Erdgeschoss", "340,00"]
        );
        assert_eq!(table.row_text(2), ["2", "Anfahrt", "45,00"]);
    }

    #[test]
    fn a_rule_down_the_page_parts_two_cells_however_close() {
        // A right-aligned amount and the left-aligned remark beside it, only
        // the cells' padding apart: the border between them is the cut.
        let lines = vec![
            worded(0.0, &[("Betrag", 0.0, 60.0), ("Bemerkung", 72.0, 150.0)]),
            worded(
                12.0,
                &[
                    ("1.169,93", 0.0, 60.0),
                    ("Konzept", 72.0, 130.0),
                    ("Backup", 134.0, 190.0),
                ],
            ),
            worded(24.0, &[("835,22", 10.0, 60.0), ("Wartung", 72.0, 140.0)]),
        ];
        let rules = vec![
            down(-3.0, -2.0, 40.0),
            down(66.0, -2.0, 40.0),
            down(200.0, -2.0, 40.0),
        ];
        let mut found = find(&lines, TEXT_HEIGHT, 20.0, &rules);
        assert_eq!(found.len(), 1);
        let table = found.remove(0).table;
        assert_eq!(table.row_text(1), ["1.169,93", "Konzept Backup"]);
        assert_eq!(table.row_text(2), ["835,22", "Wartung"]);
    }

    #[test]
    fn a_line_in_smaller_print_over_a_table_is_the_pages_margin() {
        // A browser's date and title over a table running on from the page
        // before, set at eight points over ten, at the paper's edge.
        let small = |line: &mut Line| {
            line.bbox.y1 = line.bbox.y0 + 8.0;
            for segment in &mut line.segments {
                segment.bbox.y1 = segment.bbox.y0 + 8.0;
            }
        };
        let mut header = row(
            -14.0,
            &[
                ("9/25/26, 1:07 AM", -40.0, 50.0),
                ("Inventar", 400.0, 450.0),
            ],
        );
        small(&mut header);
        let mut lines = vec![header];
        lines.extend(price_list());
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.rows, 3);
        assert_eq!(table.row_text(0), ["Artikel", "Menge", "Preis"]);
        // A table's own header in smaller print starts where its rows do.
        let mut lines = price_list();
        small(&mut lines[0]);
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.rows, 3);
        assert_eq!(table.row_text(0), ["Artikel", "Menge", "Preis"]);
    }

    #[test]
    fn the_subject_under_a_letters_address_is_not_a_row_of_its_table() {
        // Address and reference data side by side, set line under line; then
        // the subject and salutation a paragraph apart; then the items.
        let mut lines = vec![
            row(
                0.0,
                &[("Herrn", 0.0, 40.0), ("Datum: 14.09.2026", 400.0, 500.0)],
            ),
            row(
                11.0,
                &[
                    ("Firma Beispiel AG", 0.0, 110.0),
                    ("Kundennummer: K-4711", 400.0, 520.0),
                ],
            ),
            row(
                22.0,
                &[
                    ("Hauptstraße 12", 0.0, 90.0),
                    ("Telefon: 030 1234", 400.0, 505.0),
                ],
            ),
            row(40.0, &[("Rechnung Nr. 815", 0.0, 100.0)]),
            row(58.0, &[("Sehr geehrte Frau Schmidt,", 0.0, 160.0)]),
        ];
        lines.extend(price_list().into_iter().map(|mut line| {
            line.bbox.y0 += 76.0;
            line.bbox.y1 += 76.0;
            for segment in &mut line.segments {
                segment.bbox.y0 += 76.0;
                segment.bbox.y1 += 76.0;
            }
            line
        }));
        let found = find_here(&lines);
        let spans: Vec<(usize, usize)> = found.iter().map(|f| (f.start, f.end)).collect();
        assert_eq!(spans.last(), Some(&(5, 8)), "{spans:?}");
        assert!(
            spans.iter().all(|&(start, end)| end <= 3 || start >= 5),
            "{spans:?}"
        );
    }

    #[test]
    fn a_header_of_two_rows_is_one_header() {
        // Years centred over their two measures each, and the rows' label
        // centred beside both header rows.
        let columns = [
            (0.0, 60.0),
            (100.0, 150.0),
            (180.0, 230.0),
            (260.0, 310.0),
            (340.0, 390.0),
        ];
        let mut lines = vec![
            row(0.0, &[("2024", 150.0, 180.0), ("2025", 310.0, 340.0)]),
            row(6.0, &[("Region", 0.0, 50.0)]),
            row(
                12.0,
                &[
                    ("Umsatz", 100.0, 150.0),
                    ("Gewinn", 180.0, 230.0),
                    ("Umsatz", 260.0, 310.0),
                    ("Gewinn", 340.0, 390.0),
                ],
            ),
        ];
        for (i, name) in ["Nord", "Süd", "West"].iter().enumerate() {
            let y = 24.0 + i as f32 * 12.0;
            let cells: Vec<(&str, f32, f32)> = std::iter::once((*name, columns[0].0, 40.0))
                .chain(
                    columns[1..]
                        .iter()
                        .map(|&(x0, x1)| ("1.200", x0 + 10.0, x1)),
                )
                .collect();
            lines.push(row(y, &cells));
        }
        let found = find_here(&lines);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].start, 0);
        assert_eq!(
            found[0].table.row_text(0),
            [
                "Region",
                "2024 Umsatz",
                "2024 Gewinn",
                "2025 Umsatz",
                "2025 Gewinn"
            ]
        );
    }

    /// Three rows of a price list: name, quantity, price.
    fn price_list() -> Vec<Line> {
        vec![
            row(
                0.0,
                &[
                    ("Artikel", 0.0, 90.0),
                    ("Menge", 200.0, 250.0),
                    ("Preis", 400.0, 450.0),
                ],
            ),
            row(
                20.0,
                &[
                    ("Widget A", 0.0, 95.0),
                    ("12", 200.0, 220.0),
                    ("49,90", 400.0, 455.0),
                ],
            ),
            row(
                40.0,
                &[
                    ("Widget B", 0.0, 95.0),
                    ("3", 200.0, 212.0),
                    ("233,70", 400.0, 460.0),
                ],
            ),
        ]
    }

    #[test]
    fn three_rows_of_aligned_cells_are_a_table() {
        let table = detect(&price_list(), TEXT_HEIGHT).expect("a table");
        assert_eq!((table.rows, table.columns), (3, 3));
        assert_eq!(table.row_text(0), ["Artikel", "Menge", "Preis"]);
        assert_eq!(table.row_text(2), ["Widget B", "3", "233,70"]);
        // Cells come out in reading order.
        assert_eq!(
            table
                .cells
                .iter()
                .map(|c| (c.row, c.column))
                .collect::<Vec<_>>(),
            [
                (0, 0),
                (0, 1),
                (0, 2),
                (1, 0),
                (1, 1),
                (1, 2),
                (2, 0),
                (2, 1),
                (2, 2)
            ]
        );
    }

    #[test]
    fn a_label_trailing_into_white_space_stays_one_cell() {
        // `Mehrwertsteuer 19 %` beside a column of quantities. Its last two words
        // sit in the white space between the first column and the second, which is
        // not evidence of a column boundary — the label is still one label.
        let mut lines = price_list();
        lines.push(row(
            60.0,
            &[
                ("Mehrwertsteuer 19 %", 0.0, 170.0),
                ("246,98", 400.0, 460.0),
            ],
        ));
        let last = lines.len() - 1;
        // The words are what a cut works from, so the row needs them.
        lines[last].words = [
            ("Mehrwertsteuer", 0.0, 90.0),
            ("19", 120.0, 145.0),
            ("%", 155.0, 170.0),
        ]
        .iter()
        .map(|(text, x0, x1)| Word {
            text: (*text).into(),
            bbox: Rect::new(*x0, 60.0, *x1, 60.0 + TEXT_HEIGHT),
            confidence: 0.95,
        })
        .collect();

        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.row_text(3), ["Mehrwertsteuer 19 %", "", "246,98"]);
    }

    /// One row whose words sit at the given x ranges, as a recognizer leaves them.
    fn worded_row(y: f32, words: &[(f32, f32)]) -> Line {
        let mut line = row(
            y,
            &words
                .iter()
                .map(|(x0, x1)| ("x", *x0, *x1))
                .collect::<Vec<_>>(),
        );
        line.words = words
            .iter()
            .map(|(x0, x1)| Word {
                text: "x".into(),
                bbox: Rect::new(*x0, y, *x1, y + TEXT_HEIGHT),
                confidence: 0.95,
            })
            .collect();
        line
    }

    #[test]
    fn the_gutter_lands_between_the_word_spaces_and_the_gutters() {
        // A page that is nothing but a price list: its cells hold one word each,
        // so most of the gaps are gutters and even the quarter point measures
        // one. Word spaces of 5 and gutters from 16: the threshold has to fall
        // between them, whatever the quarter point says.
        let lines: Vec<Line> = (0..8)
            .map(|row| {
                worded_row(
                    row as f32 * 20.0,
                    &[
                        (0.0, 40.0),
                        (45.0, 70.0), // a word space inside the first cell
                        (86.0, 106.0),
                        (124.0, 144.0),
                        (163.0, 183.0),
                    ],
                )
            })
            .collect();
        let gutter = gutter_width(&lines, TEXT_HEIGHT);
        assert!(
            (5.0..16.0).contains(&gutter),
            "gutter {gutter} has to part the word spaces from the gutters"
        );
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!((table.rows, table.columns), (8, 4));
    }

    #[test]
    fn a_heading_spaced_wider_than_the_rows_does_not_set_the_gutter() {
        // `RECHNUNG 2026-0042` over a price list: its two words sit further apart
        // than anything in the rows below, which opens a stretch of its own well
        // below the gutters. A threshold taken from that stretch cuts the heading
        // into two cells and the table adopts it as a header row.
        // One detector box holding two words, which is how a heading arrives.
        let mut heading = row(0.0, &[("x x", 0.0, 95.0)]);
        heading.words = [(0.0, 40.0), (55.0, 95.0)]
            .iter()
            .map(|(x0, x1)| Word {
                text: "x".into(),
                bbox: Rect::new(*x0, 0.0, *x1, TEXT_HEIGHT),
                confidence: 0.95,
            })
            .collect();
        let mut lines = vec![heading];
        lines.extend((1..7).map(|row| {
            worded_row(
                row as f32 * 20.0,
                &[
                    (0.0, 40.0),
                    (45.0, 70.0), // a word space inside the first cell
                    (110.0, 130.0),
                    (170.0, 190.0),
                    (230.0, 250.0),
                ],
            )
        }));
        let gutter = gutter_width(&lines, TEXT_HEIGHT);
        assert!(
            (15.0..40.0).contains(&gutter),
            "gutter {gutter} has to leave the heading whole and still part the columns"
        );
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(
            (table.rows, table.columns),
            (6, 4),
            "the heading is not a row"
        );
    }

    #[test]
    fn a_page_of_prose_measures_its_gutter_from_its_word_spaces() {
        // Gaps that crowd together are all word spaces, and there the quarter
        // point is the measure: no jump in the distribution may lower it.
        let lines: Vec<Line> = (0..8)
            .map(|row| {
                worded_row(
                    row as f32 * 20.0,
                    &[
                        (0.0, 40.0),
                        (48.0, 90.0),
                        (99.0, 130.0),
                        (137.0, 180.0),
                        (191.0, 230.0),
                    ],
                )
            })
            .collect();
        // Quarter point of the 8, 9, 11, 7 pattern is 8, and twice that is the
        // gutter — wide enough that none of these lines holds a second cell.
        assert_eq!(gutter_width(&lines, TEXT_HEIGHT), 16.0);
        assert!(find_here(&lines).is_empty());
    }

    #[test]
    fn a_page_of_sentences_is_not_a_table() {
        // Lines of similar length leave their last word a little further out on
        // every line, and three of those look like a right-hand column. What
        // gives them away is the lines between that carry no cells at all.
        let lines = vec![
            row(
                0.0,
                &[
                    ("Kleindienst Maschinenbau", 0.0, 320.0),
                    ("GmbH", 340.0, 420.0),
                ],
            ),
            row(20.0, &[("Industriestrasse 14, 85748 Garching", 0.0, 430.0)]),
            row(40.0, &[("Lieferdatum: 17.03.2026", 0.0, 260.0)]),
            row(60.0, &[("Position 1: Fuehrungsschiene FS-220", 0.0, 430.0)]),
            row(
                80.0,
                &[
                    ("Zwischensumme: 4.812,50", 0.0, 300.0),
                    ("EUR", 330.0, 400.0),
                ],
            ),
            row(
                100.0,
                &[
                    ("Umsatzsteuer 19 %: 914,38", 0.0, 310.0),
                    ("EUR", 330.0, 400.0),
                ],
            ),
        ];
        assert!(find_here(&lines).is_empty());
    }

    #[test]
    fn running_text_is_not_a_table() {
        // One box per line, which is what the detector returns for prose.
        let prose: Vec<Line> = (0..6)
            .map(|i| row(i as f32 * 20.0, &[("a sentence of prose", 0.0, 400.0)]))
            .collect();
        assert!(detect(&prose, TEXT_HEIGHT).is_none());
    }

    #[test]
    fn two_rows_are_not_enough_to_call_it_a_table() {
        let mut short = price_list();
        short.truncate(2);
        assert!(detect(&short, TEXT_HEIGHT).is_none());
    }

    #[test]
    fn a_gap_on_one_line_only_is_not_a_column() {
        // A paragraph the detector split once, twice, and not at all: the split
        // is not in the same place, so there is no column there.
        let lines = vec![
            row(
                0.0,
                &[
                    ("Sehr geehrte", 0.0, 120.0),
                    ("Damen und Herren", 200.0, 400.0),
                ],
            ),
            row(20.0, &[("wir bestaetigen Ihnen den Eingang", 0.0, 400.0)]),
            row(
                40.0,
                &[
                    ("Ihrer Bestellung", 0.0, 150.0),
                    ("vom Montag", 300.0, 400.0),
                ],
            ),
            row(60.0, &[("und danken fuer Ihren Auftrag", 0.0, 400.0)]),
        ];
        assert!(detect(&lines, TEXT_HEIGHT).is_none());
    }

    #[test]
    fn a_row_spanning_the_table_keeps_the_columns_beside_it() {
        // A line across the whole width — a section title inside the grid —
        // covers every gutter. Counted as evidence it would merge the three
        // columns into one.
        let mut lines = price_list();
        lines.insert(1, row(10.0, &[("Zubehoer", 0.0, 460.0)]));
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.columns, 3, "the title does not erase the gutters");
        assert_eq!(table.rows, 4);
        let spanning: Vec<&Cell> = table.row(1).collect();
        assert_eq!(spanning.len(), 1);
        assert_eq!(spanning[0].column, 0);
        assert_eq!(spanning[0].column_span, 3, "the title spans the table");
    }

    #[test]
    fn the_paragraph_after_a_table_stays_a_paragraph() {
        // The line below a table is usually the terms, not a row. A full-width
        // line only belongs to the table when the table continues after it.
        let mut lines = price_list();
        lines.push(row(
            60.0,
            &[("Zahlbar innerhalb von 14 Tagen ohne Abzug.", 0.0, 460.0)],
        ));
        let found = find_here(&lines);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].start, 0);
        assert_eq!(found[0].end, 3, "the table ends before the terms");
        assert_eq!(found[0].table.rows, 3);
    }

    #[test]
    fn a_table_inside_a_block_of_prose_is_found_on_its_own() {
        // An invoice is one block as far as the vertical gaps are concerned.
        let mut lines = vec![
            row(-40.0, &[("RECHNUNG 2026-0042", 0.0, 300.0)]),
            row(-20.0, &[("Musterstrasse 17, 80331 Muenchen", 0.0, 350.0)]),
        ];
        lines.extend(price_list());
        lines.push(row(
            60.0,
            &[("Vielen Dank fuer Ihren Auftrag.", 0.0, 320.0)],
        ));

        let found = find_here(&lines);
        assert_eq!(found.len(), 1);
        assert_eq!((found[0].start, found[0].end), (2, 5));
        assert_eq!(found[0].table.row_text(0), ["Artikel", "Menge", "Preis"]);
    }

    #[test]
    fn a_row_missing_a_cell_leaves_the_column_empty() {
        let mut lines = price_list();
        lines.push(row(
            60.0,
            &[("Rabatt", 0.0, 70.0), ("-20,00", 400.0, 460.0)],
        ));
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.row_text(3), ["Rabatt", "", "-20,00"]);
    }

    #[test]
    fn cells_a_word_space_apart_are_one_column_not_two() {
        // The gutter threshold is what stops `Widget A` becoming two columns.
        let word_gap = TEXT_HEIGHT * 0.3;
        let lines: Vec<Line> = (0..4)
            .map(|i| {
                row(
                    i as f32 * 20.0,
                    &[
                        ("Widget", 0.0, 60.0),
                        ("A", 60.0 + word_gap, 75.0),
                        ("49,90", 400.0, 455.0),
                    ],
                )
            })
            .collect();
        let table = detect(&lines, TEXT_HEIGHT).expect("a table");
        assert_eq!(table.columns, 2, "a word space is not a gutter");
        assert_eq!(table.row_text(0), ["Widget A", "49,90"]);
    }
}
