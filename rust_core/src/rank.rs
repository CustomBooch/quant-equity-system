//! Within-group centred ranks, bit-identical to the Python reference used by the live model inputs.
//!
//! Reference (`lab/run.py::crank`, frozen): `(s.groupby(g).rank(method="average") - 0.5) / s.groupby(g).transform("count") - 0.5`, and
//! `lab/core/c5_experts.py::design_all`, which applies it to each column cast to float32, fills missing ranks with 0, casts to float32 and appends a
//! missing-value flag for every column.
//!
//! pandas' average rank of a tie run starting at 0-based position `p` with `d` members is `(sum of (i + 1) for i in p..p+d) / d` in float64, and the count is the
//! number of non-missing values in the group; the same operations in the same order give the same bits here. Ties are exact equality, so -0.0 ties with 0.0
//! and infinities rank like any value. Missing (NaN) values get no rank and are not counted.

use std::cmp::Ordering;

/// Centred within-group ranks of one column as float64 (NaN where the value is NaN). `group[i]` is the group of row `i`.
pub fn crank(x: &[f32], group: &[u32]) -> Vec<f64> {
    assert_eq!(x.len(), group.len(), "x and group must have the same length");
    let mut out = vec![f64::NAN; x.len()];
    let mut idx: Vec<u32> = (0..x.len() as u32).filter(|&i| !x[i as usize].is_nan()).collect();
    idx.sort_unstable_by(|&a, &b| {
        let (a, b) = (a as usize, b as usize);
        group[a].cmp(&group[b]).then_with(|| x[a].partial_cmp(&x[b]).unwrap_or(Ordering::Equal))
    });
    let mut start = 0;
    while start < idx.len() {
        let g = group[idx[start] as usize];
        let mut end = start;
        while end < idx.len() && group[idx[end] as usize] == g {
            end += 1;
        }
        let count = (end - start) as f64;
        let mut i = start;
        while i < end {
            let v = x[idx[i] as usize];
            let mut j = i;
            let mut sum_ranks = 0.0f64;
            while j < end && x[idx[j] as usize] == v {
                sum_ranks += (j - start + 1) as f64;
                j += 1;
            }
            let avg = sum_ranks / (j - i) as f64;
            let c = (avg - 0.5) / count - 0.5;
            for &r in &idx[i..j] {
                out[r as usize] = c;
            }
            i = j;
        }
        start = end;
    }
    out
}

/// The design matrix of `design_all`: for `k` columns of `n` rows (`x` column-major, column j at `x[j*n..(j+1)*n]`), write the n x 2k row-major float32 matrix
/// `[crank(col_0) .. crank(col_{k-1}), isnan(col_0) .. isnan(col_{k-1})]` into `out`, missing ranks as 0. Columns are spread over `threads` threads.
pub fn design(x: &[f32], n: usize, k: usize, group: &[u32], out: &mut [f32], threads: usize) {
    assert_eq!(x.len(), n * k, "x must hold n * k values");
    assert_eq!(group.len(), n, "group must hold n values");
    assert_eq!(out.len(), n * 2 * k, "out must hold n * 2k values");
    let threads = threads.clamp(1, k.max(1));
    let cols: Vec<Vec<f32>> = std::thread::scope(|s| {
        let handles: Vec<_> = (0..threads)
            .map(|t| {
                s.spawn(move || {
                    (t..k).step_by(threads).map(|j| (j, crank(&x[j * n..(j + 1) * n], group))).collect::<Vec<_>>()
                })
            })
            .collect();
        let mut cols = vec![Vec::new(); k];
        for h in handles {
            for (j, c) in h.join().expect("rank thread panicked") {
                cols[j] = c.into_iter().map(|v| if v.is_nan() { 0.0f32 } else { v as f32 }).collect();
            }
        }
        cols
    });
    let w = 2 * k;
    for (j, c) in cols.iter().enumerate() {
        let col = &x[j * n..(j + 1) * n];
        for i in 0..n {
            out[i * w + j] = c[i];
            out[i * w + k + j] = if col[i].is_nan() { 1.0 } else { 0.0 };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ties_missing_and_groups() {
        // group 0: values 3, 1, 1, NaN -> ranks 3, 1.5, 1.5 of 3; group 1: 5 alone -> rank 1 of 1
        let x = [3.0f32, 1.0, 1.0, f32::NAN, 5.0];
        let g = [0u32, 0, 0, 0, 1];
        let r = crank(&x, &g);
        let c = |avg: f64, n: f64| (avg - 0.5) / n - 0.5;
        assert_eq!(r[0], c(3.0, 3.0));
        assert_eq!(r[1], c(1.5, 3.0));
        assert_eq!(r[2], c(1.5, 3.0));
        assert!(r[3].is_nan());
        assert_eq!(r[4], c(1.0, 1.0));
    }

    #[test]
    fn signed_zero_ties_and_infinities_rank() {
        let x = [0.0f32, -0.0, f32::INFINITY, f32::NEG_INFINITY];
        let r = crank(&x, &[0, 0, 0, 0]);
        assert_eq!(r[0], r[1]);
        assert!(r[3] < r[0] && r[0] < r[2]);
    }

    #[test]
    fn design_layout_and_threads_do_not_change_results() {
        let n = 7;
        let x: Vec<f32> = (0..3 * n).map(|i| if i % 5 == 0 { f32::NAN } else { ((i * 37) % 11) as f32 }).collect();
        let g: Vec<u32> = (0..n as u32).map(|i| i % 2).collect();
        let mut a = vec![0f32; n * 6];
        let mut b = vec![0f32; n * 6];
        design(&x, n, 3, &g, &mut a, 1);
        design(&x, n, 3, &g, &mut b, 8);
        assert_eq!(a, b);
        for j in 0..3 {
            let r = crank(&x[j * n..(j + 1) * n], &g);
            for i in 0..n {
                assert_eq!(a[i * 6 + j], if r[i].is_nan() { 0.0 } else { r[i] as f32 });
                assert_eq!(a[i * 6 + 3 + j], if x[j * n + i].is_nan() { 1.0 } else { 0.0 });
            }
        }
    }
}
