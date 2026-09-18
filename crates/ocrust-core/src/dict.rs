//! Character dictionary handling for CTC recognition models.
//!
//! PP-OCR recognizers emit one logit per class, where class `0` is the CTC
//! blank, classes `1..=n` map to the dictionary and — when the model was
//! exported with `use_space_char` — the final class is a literal space.
//! Modern exports carry the dictionary inside the ONNX file, so a separate
//! `*_dict.txt` is optional.

use std::path::Path;

use crate::error::{Error, Result};

/// The class list of a recognition model, indexed exactly like its logits.
#[derive(Debug, Clone)]
pub struct CharDict {
    /// `classes[i]` is the text emitted for logit index `i`; index 0 is blank.
    classes: Vec<String>,
}

impl CharDict {
    /// Builds the class list from dictionary lines.
    ///
    /// `num_classes` is the recognizer's output width; it decides whether a
    /// trailing space class has to be appended.
    pub fn from_lines<I, S>(lines: I, num_classes: Option<usize>) -> Result<Self>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut classes: Vec<String> = Vec::with_capacity(8000);
        classes.push(String::new()); // CTC blank
        for line in lines {
            // Dictionary files are one character per line; only the trailing
            // newline is stripped so that a literal space line survives.
            let s = line.as_ref().trim_end_matches(['\r', '\n']);
            if s.is_empty() {
                continue;
            }
            classes.push(s.to_string());
        }
        if classes.len() < 2 {
            return Err(Error::Dict("dictionary is empty".into()));
        }
        if let Some(n) = num_classes {
            match n.checked_sub(classes.len()) {
                // Exported with `use_space_char`.
                Some(1) => classes.push(" ".to_string()),
                Some(0) => {}
                _ => {
                    return Err(Error::Dict(format!(
                        "dictionary has {} entries (+blank) but the model has {n} classes; \
                         the dictionary does not match the recognition model",
                        classes.len() - 1
                    )))
                }
            }
        }
        Ok(Self { classes })
    }

    /// Loads a PaddleOCR-style dictionary file.
    pub fn from_file(path: &Path, num_classes: Option<usize>) -> Result<Self> {
        let raw = std::fs::read_to_string(path).map_err(|e| Error::io(path, e))?;
        Self::from_lines(raw.split('\n'), num_classes)
    }

    /// Reads the dictionary embedded in an ONNX model's metadata.
    pub fn from_model_metadata(value: &str, num_classes: Option<usize>) -> Result<Self> {
        Self::from_lines(value.split('\n'), num_classes)
    }

    pub fn len(&self) -> usize {
        self.classes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.classes.len() <= 1
    }

    /// True when the model can emit `c` as a character of its own.
    ///
    /// Multi-character classes (a few PP-OCR exports contain ligature-like
    /// entries) count as containing each of their characters.
    pub fn contains(&self, c: char) -> bool {
        self.classes
            .iter()
            .skip(1)
            .any(|class| class.chars().any(|k| k == c))
    }

    /// Every character the model can emit, in class order.
    pub fn chars(&self) -> impl Iterator<Item = char> + '_ {
        self.classes.iter().skip(1).flat_map(|c| c.chars())
    }

    /// Text for a logit index, or `None` for blank / out-of-range.
    pub fn get(&self, index: usize) -> Option<&str> {
        match self.classes.get(index) {
            Some(s) if !s.is_empty() => Some(s.as_str()),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appends_space_class_when_model_is_one_wider() {
        let d = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        assert_eq!(d.len(), 5);
        assert_eq!(d.get(0), None, "index 0 is the CTC blank");
        assert_eq!(d.get(1), Some("a"));
        assert_eq!(d.get(4), Some(" "));
    }

    #[test]
    fn exact_match_needs_no_space() {
        let d = CharDict::from_lines(["a", "b"], Some(3)).unwrap();
        assert_eq!(d.len(), 3);
        assert_eq!(d.get(2), Some("b"));
    }

    #[test]
    fn mismatch_is_reported() {
        let err = CharDict::from_lines(["a", "b"], Some(99)).unwrap_err();
        assert!(err.to_string().contains("does not match"), "{err}");
    }

    #[test]
    fn contains_and_chars_expose_the_charset() {
        let d = CharDict::from_lines(["a", "ä", "ß"], None).unwrap();
        assert!(d.contains('ä'));
        assert!(d.contains('ß'));
        assert!(!d.contains('ö'));
        // The blank class is never a character.
        assert_eq!(d.chars().count(), 3);
    }

    #[test]
    fn blank_lines_are_skipped_but_content_kept() {
        let d = CharDict::from_lines("x\n\ny\n".split('\n'), None).unwrap();
        assert_eq!(d.len(), 3);
        assert_eq!(d.get(2), Some("y"));
    }
}
