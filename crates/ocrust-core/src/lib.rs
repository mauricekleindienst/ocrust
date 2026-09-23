//! # ocrust-core
//!
//! A fast, self-contained document OCR engine.
//!
//! * **Any input.** Images in every common raster format, multi-page TIFF, and
//!   PDF — rasterized by the pure-Rust [`hayro`](https://crates.io/crates/hayro)
//!   renderer, so there is no Poppler or PDFium to install.
//! * **Modern models.** PP-OCR family detection (DB), 180° line orientation and
//!   CTC recognition, executed through ONNX Runtime with optional CUDA, CoreML
//!   or DirectML acceleration.
//! * **Readable output.** Skew correction, column-aware reading order,
//!   paragraph grouping and de-hyphenation, exported as text, Markdown, JSON,
//!   hOCR, ALTO, CSV or a searchable PDF.
//!
//! ## Quick start
//!
//! ```no_run
//! use ocrust_core::{Engine, EngineConfig, Source};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let engine = Engine::new(EngineConfig::new())?;
//! let doc = engine.scan(&Source::path("invoice.pdf"))?;
//! println!("{}", doc.text());
//! # Ok(())
//! # }
//! ```
//!
//! ## ONNX Runtime
//!
//! The library loads `libonnxruntime` dynamically. Set `ORT_DYLIB_PATH` to pick
//! a specific build; the Python package points it at the `onnxruntime` wheel it
//! depends on, which is why `pip install ocrust` needs no system packages.

#![forbid(unsafe_code)]
#![warn(missing_debug_implementations)]

pub mod classify;
pub mod detect;
pub mod dict;
pub mod doc;
pub mod error;
pub mod export;
pub mod geom;
pub mod ingest;
pub mod lang;
pub mod layout;
pub mod models;
pub mod pipeline;
pub mod preprocess;
mod quality;
pub mod recognize;
pub mod runtime;
mod table;
pub mod tickbox;

pub use classify::OrientationConfig;
pub use detect::{DetectorConfig, LimitType};
pub use doc::{Block, BlockKind, Document, Line, Page, PageOrigin, Word};
pub use error::{Error, Result};
pub use export::{render, Format};
pub use geom::{Point, Quad, Rect};
pub use ingest::{IngestConfig, Source};
pub use lang::{Coverage, Language, Script};
pub use layout::LayoutConfig;
pub use models::{ModelPaths, ModelSet};
pub use pipeline::{plan_parallelism, scan_file, Engine, EngineConfig, Parallelism, Progress};
pub use preprocess::PreprocessConfig;
pub use recognize::RecognizerConfig;
pub use runtime::{runtime_version, Device, SessionOptions};

/// This crate's version.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
