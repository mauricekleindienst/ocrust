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
pub(crate) fn find(lines: &[Line], text_height: f32, gutter: f32) -> Vec<Found> {
    let rows: Vec<Vec<Candidate>> = lines.iter().map(|line| cells_of(line, gutter)).collect();
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
    while at < rows.len() {
        if rows[at].len() < MIN_COLUMNS {
            at += 1;
            continue;
        }
        let (end, candidates) = grow(&rows, lines, at, text_height * MAX_ROW_GAP);
        log::debug!("  grow from {at}: end {end}, {candidates} candidate rows");
        if candidates >= MIN_ROWS {
            if let Some(columns) = columns_of(&rows[at..end], gutter) {
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
                let adopted = at > 0 && adopts(&rows[at - 1], &columns, gutter);
                let start = if adopted { at - 1 } else { at };
                found.push(Found {
                    start,
                    end,
                    table: grid(&rows[start..end], &columns, gutter, adopted),
                });
                at = end;
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
fn grow(rows: &[Vec<Candidate>], lines: &[Line], start: usize, max_row_gap: f32) -> (usize, usize) {
    let mut last = start;
    let mut candidates = 1usize;
    let mut previous = gutters_of(&rows[start]);

    for (offset, row) in rows.iter().enumerate().skip(start + 1) {
        // Two tables of the same shape on one page are two tables. What separates
        // them is the white space, so a row far below the last one ends the run.
        if lines[offset].bbox.y0 - lines[offset - 1].bbox.y1 > max_row_gap {
            break;
        }
        if row.len() >= MIN_COLUMNS {
            let theirs = gutters_of(row);
            if !aligns(&previous, &theirs) {
                break;
            }
            previous = theirs;
            candidates += 1;
            last = offset;
        } else if spans_the_row(row, &rows[start..=last]) {
            // A line across the whole width — a title, a total — sits inside the
            // table without saying anything about its columns.
            continue;
        } else {
            break;
        }
    }
    (last + 1, candidates)
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
fn aligns(a: &[(f32, f32)], b: &[(f32, f32)]) -> bool {
    let (fewer, more) = if a.len() <= b.len() { (a, b) } else { (b, a) };
    if fewer.is_empty() {
        return false;
    }
    fewer
        .iter()
        .all(|&(from, to)| more.iter().any(|&(f, t)| to.min(t) > from.max(f)))
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
    let multi = rows.iter().filter(|row| row.len() >= MIN_COLUMNS).count();
    if (multi as f32) < rows.len() as f32 * MIN_MULTI_CELL_SHARE {
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
fn grid(rows: &[Vec<Candidate>], columns: &[(f32, f32)], gutter: f32, header: bool) -> Table {
    let extent = extent_of(rows).unwrap_or_default();
    let mut cells = Vec::new();
    for (row, row_cells) in rows.iter().enumerate() {
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
        rows: rows.len(),
        columns: columns.len(),
        cells,
    }
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
            .filter(|w| w.bbox.x0 >= part.bbox.x0 - 1.0 && w.bbox.x1 <= part.bbox.x1 + 1.0)
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
    use crate::doc::Word;
    use crate::geom::Quad;

    const TEXT_HEIGHT: f32 = 10.0;

    /// Every table in `lines`, with the gutter measured from them.
    fn find_here(lines: &[Line]) -> Vec<Found> {
        find(lines, TEXT_HEIGHT, gutter_width(lines, TEXT_HEIGHT))
    }

    /// The one table in `lines`, when there is exactly one.
    fn detect(lines: &[Line], text_height: f32) -> Option<Table> {
        let mut found = find(lines, text_height, gutter_width(lines, text_height));
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
        lines.insert(1, row(15.0, &[("Zubehoer", 0.0, 460.0)]));
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
