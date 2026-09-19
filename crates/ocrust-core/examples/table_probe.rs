//! Prints what the table detector makes of a document, cell by cell.
//!
//! `cargo run --example table_probe --features pdf -- page.png`

use ocrust_core::{EngineConfig, Source};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    env_logger::builder()
        .filter_level(log::LevelFilter::Debug)
        .format_timestamp(None)
        .init();
    let path = std::env::args().nth(1).ok_or("usage: table_probe <file>")?;
    let engine = ocrust_core::Engine::new(EngineConfig::new())?;
    let doc = engine.scan(&Source::path(&path))?;
    for page in &doc.pages {
        for block in &page.blocks {
            println!("{:?} lines={}", block.kind, block.lines.len());
            if let Some(table) = &block.table {
                println!("  {}x{}", table.rows, table.columns);
                for cell in &table.cells {
                    println!(
                        "    r{} c{}+{} [{:.0}..{:.0}] {:?}",
                        cell.row,
                        cell.column,
                        cell.column_span,
                        cell.bbox.x0,
                        cell.bbox.x1,
                        cell.text
                    );
                }
            }
        }
    }
    Ok(())
}
