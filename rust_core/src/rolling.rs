//! Fixed-window rolling sum, mean and max, bit-identical to pandas 2.x `Series.rolling(window, min_periods).sum() / .mean() / .max()` (not centred).
//!
//! pandas computes a rolling sum by adding each new value and removing the value that leaves the window, with Kahan compensation kept separately for additions
//! and removals; when every value in the window equals the last value added it returns `last * nobs` (GH#42064). The mean adds a count of negative values and
//! clips a result whose sign contradicts all of its inputs to zero. The result at a row therefore depends on the whole history before it, so the same sequence
//! of floating-point operations is replayed here. NaN values are skipped and not counted, and so are infinities: pandas converts +-inf to NaN before any
//! rolling aggregation (`Window._prep_values`), so an infinity never enters the sums and the window recovers once it has left.
//! `lab/kernels/tests` checks every bit against pandas.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum How {
    Sum,
    Mean,
    Max,
}

struct Acc {
    nobs: i64,
    sum: f64,
    comp_add: f64,
    comp_remove: f64,
    neg_ct: i64,
    same: i64,
    prev: f64,
}

impl Acc {
    fn new(first: f64) -> Self {
        Acc { nobs: 0, sum: 0.0, comp_add: 0.0, comp_remove: 0.0, neg_ct: 0, same: 0, prev: first }
    }

    fn add(&mut self, v: f64) {
        if v.is_nan() {
            return;
        }
        self.nobs += 1;
        let y = v - self.comp_add;
        let t = self.sum + y;
        self.comp_add = t - self.sum - y;
        self.sum = t;
        if v.is_sign_negative() {
            self.neg_ct += 1;
        }
        if v == self.prev {
            self.same += 1;
        } else {
            self.same = 1;
        }
        self.prev = v;
    }

    fn remove(&mut self, v: f64) {
        if v.is_nan() {
            return;
        }
        self.nobs -= 1;
        let y = -v - self.comp_remove;
        let t = self.sum + y;
        self.comp_remove = t - self.sum - y;
        self.sum = t;
        if v.is_sign_negative() {
            self.neg_ct -= 1;
        }
    }

    fn sum_result(&self, minp: i64) -> f64 {
        if self.nobs == 0 && minp == 0 {
            0.0
        } else if self.nobs >= minp {
            if self.same >= self.nobs {
                self.prev * self.nobs as f64
            } else {
                self.sum
            }
        } else {
            f64::NAN
        }
    }

    fn mean_result(&self, minp: i64) -> f64 {
        if self.nobs >= minp && self.nobs > 0 {
            let r = self.sum / self.nobs as f64;
            if self.same >= self.nobs {
                self.prev
            } else if self.neg_ct == 0 && r < 0.0 {
                0.0
            } else if self.neg_ct == self.nobs && r > 0.0 {
                0.0
            } else {
                r
            }
        } else {
            f64::NAN
        }
    }
}

/// Rolling statistic of one column; `out[i]` covers rows `max(0, i + 1 - window) .. i + 1`.
pub fn rolling(x: &[f64], window: usize, minp: usize, how: How, out: &mut [f64]) {
    assert_eq!(x.len(), out.len());
    assert!(window >= 1);
    let cleaned: Vec<f64> = x.iter().map(|&v| if v.is_infinite() { f64::NAN } else { v }).collect();
    let x = &cleaned[..];
    let n = x.len();
    let minp_i = minp as i64;
    if how == How::Max {
        for i in 0..n {
            let s = (i + 1).saturating_sub(window);
            let mut cnt = 0usize;
            let mut best = f64::NAN;
            for &v in &x[s..=i] {
                if !v.is_nan() {
                    // pandas keeps a monotonic deque and drops an older entry when a newer one is >= it, so among equal maxima the latest wins (matters for -0.0 / 0.0)
                    if cnt == 0 || v >= best {
                        best = v;
                    }
                    cnt += 1;
                }
            }
            out[i] = if cnt > 0 && cnt >= minp { best } else { f64::NAN };
        }
        return;
    }
    if n == 0 {
        return;
    }
    let mut acc = Acc::new(x[0]);
    for i in 0..n {
        let s = (i + 1).saturating_sub(window);
        let e = i + 1;
        if i == 0 {
            for &v in &x[s..e] {
                acc.add(v);
            }
        } else {
            let s_prev = i.saturating_sub(window);
            if s >= i {
                // window of one row starting after the previous window ended: pandas re-initialises
                acc = Acc::new(x[s]);
                acc.add(x[s]);
            } else {
                for &v in &x[s_prev..s] {
                    acc.remove(v);
                }
                acc.add(x[i]);
            }
        }
        out[i] = match how {
            How::Sum => acc.sum_result(minp_i),
            How::Mean => acc.mean_result(minp_i),
            How::Max => unreachable!(),
        };
    }
}

/// `k` columns of `n` rows, column-major, spread over `threads` threads.
pub fn rolling_matrix(x: &[f64], n: usize, k: usize, window: usize, minp: usize, how: How, out: &mut [f64], threads: usize) {
    assert_eq!(x.len(), n * k);
    assert_eq!(out.len(), n * k);
    if n == 0 || k == 0 {
        return;
    }
    let threads = threads.clamp(1, k);
    let per = (k + threads - 1) / threads;
    std::thread::scope(|s| {
        for (xc, oc) in x.chunks(per * n).zip(out.chunks_mut(per * n)) {
            s.spawn(move || {
                for (xi, oi) in xc.chunks(n).zip(oc.chunks_mut(n)) {
                    rolling(xi, window, minp, how, oi);
                }
            });
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_skip_missing_and_respect_min_periods() {
        let x = [1.0, f64::NAN, 2.0, 3.0, 4.0];
        let mut o = [0.0; 5];
        rolling(&x, 3, 2, How::Sum, &mut o);
        assert!(o[0].is_nan());
        assert!(o[1].is_nan());
        assert_eq!(o[2], 3.0);
        assert_eq!(o[3], 5.0);
        assert_eq!(o[4], 9.0);
    }

    #[test]
    fn infinities_count_as_missing_and_the_window_recovers() {
        let x = [f64::INFINITY, 2.0, 3.0, 4.0, f64::NEG_INFINITY, 1.0];
        let mut o = [0.0; 6];
        rolling(&x, 2, 2, How::Sum, &mut o);
        assert!(o[0].is_nan() && o[1].is_nan());
        assert_eq!((o[2], o[3]), (5.0, 7.0));
        assert!(o[4].is_nan() && o[5].is_nan());
        rolling(&x, 3, 1, How::Max, &mut o);
        assert_eq!(o[0].is_nan(), true);
        assert_eq!(o[5], 4.0);
    }

    #[test]
    fn constant_runs_return_the_exact_multiple() {
        let x = [0.1, 0.1, 0.1, 0.1];
        let mut o = [0.0; 4];
        rolling(&x, 3, 3, How::Sum, &mut o);
        assert_eq!(o[3], 0.1 * 3.0);
        rolling(&x, 3, 3, How::Mean, &mut o);
        assert_eq!(o[3], 0.1);
    }

    #[test]
    fn max_and_threads() {
        let n = 50;
        let x: Vec<f64> = (0..3 * n).map(|i| if i % 7 == 0 { f64::NAN } else { ((i * 31) % 17) as f64 - 8.0 }).collect();
        let mut a = vec![0.0; 3 * n];
        let mut b = vec![0.0; 3 * n];
        for how in [How::Sum, How::Mean, How::Max] {
            rolling_matrix(&x, n, 3, 12, 9, how, &mut a, 1);
            rolling_matrix(&x, n, 3, 12, 9, how, &mut b, 4);
            assert_eq!(a.iter().map(|v| v.to_bits()).collect::<Vec<_>>(), b.iter().map(|v| v.to_bits()).collect::<Vec<_>>());
        }
    }
}
