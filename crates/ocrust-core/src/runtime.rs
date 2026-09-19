//! ONNX Runtime plumbing: session creation, execution providers and a tiny
//! pool that lets several worker threads share one model.

use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use ndarray::{ArrayD, ArrayViewD, IxDyn};
use ort::session::builder::GraphOptimizationLevel;
use ort::session::Session;
use ort::value::Tensor;

use crate::error::{Error, Result};

/// Hardware backend used for inference.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Device {
    /// CPU only. Always available.
    #[default]
    Cpu,
    /// Use an accelerator if the runtime was built with support for it,
    /// otherwise silently fall back to the CPU.
    Auto,
    /// NVIDIA CUDA, device index.
    Cuda(i32),
    /// Apple CoreML (macOS / iOS).
    CoreMl,
    /// Windows DirectML, device index.
    DirectMl(i32),
}

impl Device {
    /// Parses `"cpu"`, `"auto"`, `"cuda"`, `"cuda:1"`, `"coreml"`, `"directml"`.
    pub fn parse(s: &str) -> Result<Self> {
        let lower = s.trim().to_ascii_lowercase();
        let (name, index) = match lower.split_once(':') {
            Some((n, i)) => (
                n,
                i.parse::<i32>()
                    .map_err(|_| Error::config(format!("bad device index in {s:?}")))?,
            ),
            None => (lower.as_str(), 0),
        };
        match name {
            "cpu" => Ok(Self::Cpu),
            "auto" => Ok(Self::Auto),
            "cuda" | "gpu" => Ok(Self::Cuda(index)),
            "coreml" | "mps" | "metal" => Ok(Self::CoreMl),
            "directml" | "dml" => Ok(Self::DirectMl(index)),
            other => Err(Error::config(format!(
                "unknown device {other:?} (use cpu, auto, cuda[:n], coreml or directml)"
            ))),
        }
    }
}

/// Knobs for every session this crate creates.
#[derive(Debug, Clone)]
pub struct SessionOptions {
    /// Threads used inside a single operator. `0` lets the runtime decide.
    pub intra_threads: usize,
    /// Threads used to run independent operators in parallel.
    pub inter_threads: usize,
    /// Number of independent sessions per model, so that page-level workers do
    /// not serialize on a single session lock.
    pub replicas: usize,
    /// Let ONNX Runtime keep an allocation arena and plan tensor reuse.
    ///
    /// Both are speed optimizations that assume the tensor shapes repeat. Pages
    /// do not: every scan is a different size, so the arena holds high-water
    /// blocks it will not reuse and the reuse plan is redrawn anyway. Measured
    /// over a 40-page PDF, turning both off costs 10% more time at one worker
    /// and nothing at four, and takes peak memory from 584 MB to 253 MB — from
    /// 2006 MB to 991 MB at four workers. Off by default for that reason; turn
    /// it on when the time matters more than the memory.
    pub cache_allocations: bool,
    pub device: Device,
}

impl Default for SessionOptions {
    fn default() -> Self {
        Self {
            intra_threads: 0,
            inter_threads: 0,
            replicas: 1,
            cache_allocations: false,
            device: Device::default(),
        }
    }
}

/// Checks that the ONNX Runtime library can be loaded and reports the API
/// level in use, or explains how to install the library.
///
/// `ort` dlopens the runtime lazily and panics when it is missing, so the probe
/// is wrapped to turn that into an actionable error.
pub fn runtime_version() -> Result<String> {
    let probe = std::panic::catch_unwind(|| {
        let _ = ort::api();
        format!("ONNX Runtime (API level {})", ort::MINOR_VERSION)
    });
    probe.map_err(|_| {
        Error::RuntimeMissing(
            std::env::var("ORT_DYLIB_PATH").unwrap_or_else(|_| "libonnxruntime (system)".into()),
        )
    })
}

/// One loaded ONNX model plus a pool of sessions to run it with.
pub struct OnnxModel {
    sessions: Vec<Mutex<Session>>,
    next: AtomicUsize,
    input_name: String,
    output_names: Vec<String>,
    /// Model metadata, read once at load time (PP-OCR ships its character set
    /// in here, which saves users from hunting for a matching dict file).
    metadata: Vec<(String, String)>,
    label: String,
}

impl std::fmt::Debug for OnnxModel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OnnxModel")
            .field("label", &self.label)
            .field("replicas", &self.sessions.len())
            .field("input", &self.input_name)
            .field("outputs", &self.output_names)
            .finish()
    }
}

impl OnnxModel {
    /// Loads `path` into `opts.replicas` sessions.
    pub fn load(path: &Path, label: &str, opts: &SessionOptions) -> Result<Self> {
        runtime_version()?;
        if !path.exists() {
            return Err(Error::model(format!(
                "{label} model not found: {}",
                path.display()
            )));
        }
        let replicas = opts.replicas.max(1);
        let mut sessions = Vec::with_capacity(replicas);
        for _ in 0..replicas {
            sessions.push(Mutex::new(build_session(path, opts)?));
        }

        let (input_name, output_names, metadata) = {
            let s = sessions[0].lock().expect("fresh session mutex");
            let input_name = s
                .inputs()
                .first()
                .map(|i| i.name().to_string())
                .ok_or_else(|| Error::model(format!("{label} model has no inputs")))?;
            let output_names: Vec<String> =
                s.outputs().iter().map(|o| o.name().to_string()).collect();
            let metadata = match s.metadata() {
                Ok(md) => md
                    .custom_keys()
                    .unwrap_or_default()
                    .into_iter()
                    .filter_map(|k| md.custom(&k).map(|v| (k, v)))
                    .collect(),
                Err(_) => Vec::new(),
            };
            (input_name, output_names, metadata)
        };

        Ok(Self {
            sessions,
            next: AtomicUsize::new(0),
            input_name,
            output_names,
            metadata,
            label: label.to_string(),
        })
    }

    /// Value of a custom metadata entry stored in the ONNX file.
    pub fn metadata(&self, key: &str) -> Option<&str> {
        self.metadata
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// Runs the model on one NCHW batch and returns the first output.
    pub fn run(&self, input: ArrayD<f32>) -> Result<ArrayD<f32>> {
        let shape: Vec<i64> = input.shape().iter().map(|&d| d as i64).collect();
        let (data, offset) = input.into_raw_vec_and_offset();
        debug_assert!(offset.unwrap_or(0) == 0, "expected a contiguous array");
        let tensor = Tensor::from_array((shape, data))?;

        let mut guard = self.acquire();
        let outputs = guard.run(ort::inputs![self.input_name.as_str() => tensor])?;
        let view: ArrayViewD<f32> = outputs[0].try_extract_array::<f32>()?;
        Ok(view.to_owned())
    }

    /// Takes the least contended session in the pool.
    fn acquire(&self) -> std::sync::MutexGuard<'_, Session> {
        let n = self.sessions.len();
        if n > 1 {
            for i in 0..n {
                let idx = (self.next.fetch_add(1, Ordering::Relaxed) + i) % n;
                if let Ok(guard) = self.sessions[idx].try_lock() {
                    return guard;
                }
            }
        }
        let idx = self.next.fetch_add(1, Ordering::Relaxed) % n;
        self.sessions[idx]
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn output_names(&self) -> &[String] {
        &self.output_names
    }
}

/// Session-builder calls carry the builder inside their error type; drop it so
/// the error fits our own type.
fn builder_error(e: ort::Error<ort::session::builder::SessionBuilder>) -> Error {
    Error::Runtime(e.into())
}

fn build_session(path: &Path, opts: &SessionOptions) -> Result<Session> {
    let mut builder = Session::builder()?;
    if !opts.cache_allocations {
        // See `SessionOptions::cache_allocations`: pages are all different sizes,
        // so neither the arena nor the reuse plan pays for the memory it holds.
        builder = builder.with_memory_pattern(false).map_err(builder_error)?;
        builder = builder
            .with_execution_providers([ort::ep::CPU::default().with_arena_allocator(false).build()])
            .map_err(builder_error)?;
    }

    match opts.device {
        Device::Cpu => {}
        Device::Auto => {
            builder = register_available_accelerators(builder);
        }
        Device::Cuda(_device_id) => {
            #[cfg(feature = "cuda")]
            {
                builder = builder
                    .with_execution_providers([ort::ep::CUDA::default()
                        .with_device_id(_device_id)
                        .build()])
                    .map_err(builder_error)?;
            }
            #[cfg(not(feature = "cuda"))]
            log::warn!(
                "device=cuda requested but ocrust was built without the `cuda` feature; using CPU"
            );
        }
        Device::CoreMl => {
            #[cfg(feature = "coreml")]
            {
                builder = builder
                    .with_execution_providers([ort::ep::CoreML::default().build()])
                    .map_err(builder_error)?;
            }
            #[cfg(not(feature = "coreml"))]
            log::warn!("device=coreml requested but ocrust was built without the `coreml` feature; using CPU");
        }
        Device::DirectMl(_device_id) => {
            #[cfg(feature = "directml")]
            {
                builder = builder
                    .with_execution_providers([ort::ep::DirectML::default()
                        .with_device_id(_device_id)
                        .build()])
                    .map_err(builder_error)?;
            }
            #[cfg(not(feature = "directml"))]
            log::warn!("device=directml requested but ocrust was built without the `directml` feature; using CPU");
        }
    }

    builder = builder
        .with_optimization_level(GraphOptimizationLevel::All)
        .map_err(builder_error)?;
    if opts.intra_threads > 0 {
        builder = builder
            .with_intra_threads(opts.intra_threads)
            .map_err(builder_error)?;
    }
    if opts.inter_threads > 0 {
        builder = builder
            .with_inter_threads(opts.inter_threads)
            .map_err(builder_error)?;
    }
    Ok(builder.commit_from_file(path)?)
}

/// Best-effort accelerator registration for `Device::Auto`.
///
/// Unavailable providers are skipped by ONNX Runtime, so the session always
/// ends up usable even on a plain CPU box.
#[allow(unused_mut, unused_variables)]
fn register_available_accelerators(
    builder: ort::session::builder::SessionBuilder,
) -> ort::session::builder::SessionBuilder {
    // Each provider is tried in turn; a failed registration hands the builder
    // back in the error, so nothing is lost and the CPU remains the fallback.
    // TensorRT goes first because it falls back to CUDA on its own.
    #[cfg(feature = "tensorrt")]
    let builder = match builder.with_execution_providers([ort::ep::TensorRT::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "cuda")]
    let builder = match builder.with_execution_providers([ort::ep::CUDA::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "rocm")]
    let builder = match builder.with_execution_providers([ort::ep::ROCm::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "coreml")]
    let builder = match builder.with_execution_providers([ort::ep::CoreML::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "directml")]
    let builder = match builder.with_execution_providers([ort::ep::DirectML::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "openvino")]
    let builder = match builder.with_execution_providers([ort::ep::OpenVINO::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    #[cfg(feature = "webgpu")]
    let builder = match builder.with_execution_providers([ort::ep::WebGPU::default().build()]) {
        Ok(b) => return b,
        Err(e) => e.recover(),
    };
    builder
}

/// Helper to build an NCHW array from per-image planar data.
pub(crate) fn nchw(
    batch: usize,
    channels: usize,
    height: usize,
    width: usize,
    data: Vec<f32>,
) -> ArrayD<f32> {
    debug_assert_eq!(data.len(), batch * channels * height * width);
    ArrayD::from_shape_vec(IxDyn(&[batch, channels, height, width]), data)
        .expect("nchw shape matches data length")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn device_parsing() {
        assert_eq!(Device::parse("cpu").unwrap(), Device::Cpu);
        assert_eq!(Device::parse("CUDA:2").unwrap(), Device::Cuda(2));
        assert_eq!(Device::parse("auto").unwrap(), Device::Auto);
        assert!(Device::parse("tpu").is_err());
    }
}
