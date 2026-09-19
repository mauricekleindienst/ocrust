//! How much of a page is likely to be right.
//!
//! The recognizer's own confidence answers a narrower question than people read
//! into it. A CTC softmax says how sure it was about each character it emitted —
//! and it is good at that: over the evaluation corpus a line's mean character
//! probability lands within 1.4% of the share of that line's text that is
//! actually correct. What it cannot see is everything that never reached it:
//! text the detector missed, a column read in the wrong order, a label split
//! into four fragments. Those are what drive a page's error rate, so confidence
//! ranks pages poorly — Spearman +0.50 against the measured character accuracy.
//!
//! The shape of the output does see them. A page that comes back as many short
//! fragments is a page whose layout went wrong; unusually large lines relative
//! to the page mean a drawing or a poster, where detection struggles; and the
//! *weakest* line matters more than the average one. Fitted against the corpus,
//! those signals rank pages at Spearman +0.76, and separate the worst quarter
//! from the best by 3.4 times the margin confidence manages.
//!
//! The weights come from `scripts/fit_quality.py`, which reports its own
//! held-out numbers. They are an estimate, not a promise.

/// What one recognized line contributes to the estimate.
#[derive(Debug, Clone, Copy)]
pub(crate) struct LineSignals {
    /// Characters in the line's text.
    pub chars: usize,
    /// Mean character probability, as the recognizer reported it.
    pub confidence: f32,
    /// Mean distance to the runner-up class, per character.
    pub margin: f32,
    /// Line height as a fraction of the page height.
    pub height_ratio: f32,
}

/// Bias and one weight per feature, in the order `features` builds them.
///
/// Fitted by `scripts/fit_quality.py` over the 100 ground-truth files of the
/// evaluation corpus. Held out a third of the files, twenty times: the estimate
/// ranked pages at Spearman +0.751 against +0.500 for the mean confidence, won
/// on 20 splits out of 20, and separated the worst quarter of pages from the
/// best by 0.057 where confidence manages 0.017.
const WEIGHTS: [f32; 6] = [
    0.85323,  // bias
    2.00461,  // characters per line
    0.91330,  // the tenth-percentile line confidence
    -1.42933, // mean line height, relative to the page
    0.53614,  // mean line confidence
    0.47998,  // the weakest line's margin
];

/// Smallest page the estimate will judge.
///
/// Every file in the corpus it was fitted on carries at least five lines, so a
/// shorter page is extrapolation: the features that matter most — how long the
/// lines are, how tall they stand relative to the page — read a two-line letter
/// in large print as a fragmented drawing and mark it down. Saying nothing is
/// the honest answer where there is no evidence.
const MIN_LINES: usize = 5;

/// Estimated share of the page's characters that are correct, in `0..=1`.
///
/// `None` when the page carries no text, or too little of it to judge: see
/// [`MIN_LINES`].
pub(crate) fn page_quality(lines: &[LineSignals]) -> Option<f32> {
    if lines.len() < MIN_LINES {
        return None;
    }
    let x = features(lines);
    let z = WEIGHTS[0] + x.iter().zip(&WEIGHTS[1..]).map(|(v, w)| v * w).sum::<f32>();
    Some((1.0 / (1.0 + (-z.clamp(-30.0, 30.0)).exp())).clamp(0.0, 1.0))
}

/// The five features, scaled the way they were fitted.
fn features(lines: &[LineSignals]) -> [f32; 5] {
    let n = lines.len() as f32;
    let mean_chars = lines.iter().map(|l| l.chars as f32).sum::<f32>() / n;
    let mean_confidence = lines.iter().map(|l| l.confidence).sum::<f32>() / n;
    let mean_height = lines.iter().map(|l| l.height_ratio).sum::<f32>() / n;
    let weakest_margin = lines.iter().map(|l| l.margin).fold(f32::INFINITY, f32::min);

    // The tenth percentile, not the minimum: one bad line on a good page is
    // ordinary, ten percent of them is not.
    let mut confidences: Vec<f32> = lines.iter().map(|l| l.confidence).collect();
    confidences.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let weak_confidence = confidences[confidences.len() / 10];

    [
        mean_chars.min(60.0) / 30.0,
        logit(weak_confidence) / 5.0,
        (mean_height * 40.0).min(3.0),
        logit(mean_confidence) / 5.0,
        weakest_margin.clamp(0.0, 1.0),
    ]
}

fn logit(p: f32) -> f32 {
    let p = p.clamp(1e-3, 1.0 - 1e-3);
    (p / (1.0 - p)).ln()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(chars: usize, confidence: f32, margin: f32, height_ratio: f32) -> LineSignals {
        LineSignals {
            chars,
            confidence,
            margin,
            height_ratio,
        }
    }

    #[test]
    fn a_page_with_too_little_text_is_not_judged() {
        // No file in the corpus the weights come from has fewer than five lines,
        // so anything shorter would be guesswork dressed up as a number.
        assert_eq!(page_quality(&[]), None);
        for count in 1..MIN_LINES {
            let page: Vec<_> = (0..count).map(|_| line(40, 0.99, 0.98, 0.02)).collect();
            assert_eq!(page_quality(&page), None, "{count} lines");
        }
        let enough: Vec<_> = (0..MIN_LINES).map(|_| line(40, 0.99, 0.98, 0.02)).collect();
        assert!(page_quality(&enough).is_some());
    }

    #[test]
    fn a_page_of_fragments_scores_below_a_page_of_sentences() {
        // The corpus says this is what separates a drawing from an invoice:
        // the same certainty, spread over many short pieces instead of lines.
        let clean: Vec<_> = (0..20).map(|_| line(48, 0.99, 0.98, 0.02)).collect();
        let fragmented: Vec<_> = (0..20).map(|_| line(4, 0.99, 0.98, 0.02)).collect();
        let clean_q = page_quality(&clean).unwrap();
        let fragmented_q = page_quality(&fragmented).unwrap();
        assert!(
            clean_q > fragmented_q + 0.05,
            "clean {clean_q:.3} vs fragmented {fragmented_q:.3}"
        );
    }

    #[test]
    fn one_weak_line_among_many_matters_less_than_a_tenth_of_them() {
        let mut one_bad: Vec<_> = (0..20).map(|_| line(40, 0.99, 0.98, 0.02)).collect();
        one_bad[0] = line(40, 0.70, 0.40, 0.02);
        let mut many_bad = one_bad.clone();
        for slot in many_bad.iter_mut().take(4) {
            *slot = line(40, 0.70, 0.40, 0.02);
        }
        assert!(page_quality(&one_bad) > page_quality(&many_bad));
    }

    #[test]
    fn the_estimate_stays_a_probability() {
        for lines in [
            vec![line(1, 0.0, 0.0, 1.0); MIN_LINES],
            vec![line(200, 1.0, 1.0, 0.0001); MIN_LINES],
            vec![line(0, 0.5, 0.5, 0.5); 40],
        ] {
            let q = page_quality(&lines).unwrap();
            assert!((0.0..=1.0).contains(&q), "{q}");
        }
    }
}
