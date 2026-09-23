//! Error type shared by the whole crate.

use std::path::PathBuf;

/// Result alias used throughout `ocrust-core`.
pub type Result<T> = std::result::Result<T, Error>;

/// Everything that can go wrong while scanning a document.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    #[error("i/o error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("i/o error: {0}")]
    PlainIo(#[from] std::io::Error),

    #[error("unsupported input: {0}")]
    Unsupported(String),

    #[error("could not decode image: {0}")]
    Image(#[from] image::ImageError),

    #[error("pdf error: {0}")]
    Pdf(String),

    #[error("model error: {0}")]
    Model(String),

    #[error("onnx runtime error: {0}")]
    Runtime(#[from] ort::Error),

    #[error(
        "the ONNX Runtime library could not be loaded: {0}\n\
             hint: install it with `pip install onnxruntime`, or point OCRUST_ORT_DYLIB / \
             ORT_DYLIB_PATH at libonnxruntime"
    )]
    RuntimeMissing(String),

    #[error("invalid configuration: {0}")]
    Config(String),

    #[error("character dictionary problem: {0}")]
    Dict(String),

    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("download failed: {0}")]
    Download(String),

    #[error("operation cancelled")]
    Cancelled,

    /// A bug in ocrust met on one document. Caught so a batch goes on with
    /// the next document instead of ending with this one.
    #[error("internal error while reading this document ({0}); this is a bug in ocrust — please report it, with the file if you can")]
    Internal(String),
}

impl Error {
    pub(crate) fn io(path: impl Into<PathBuf>, source: std::io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }

    pub(crate) fn model(msg: impl Into<String>) -> Self {
        Self::Model(msg.into())
    }

    pub(crate) fn config(msg: impl Into<String>) -> Self {
        Self::Config(msg.into())
    }
}
