//! Newey-West (Bartlett) inference for the mean: same convention as `lab.stats.hac`.

pub const DEFAULT_LAGS: usize = 6;
pub const MIN_OBS: usize = 12;

/// (mean, t, n) after dropping NaN. Lags are capped at n - 1. NaN mean and t when n < min_obs.
pub fn nw_mean_t(x: &[f64], lags: usize, min_obs: usize) -> (f64, f64, usize) {
    let v: Vec<f64> = x.iter().copied().filter(|a| !a.is_nan()).collect();
    let n = v.len();
    if n < min_obs {
        return (f64::NAN, f64::NAN, n);
    }
    let m = v.iter().sum::<f64>() / n as f64;
    let u: Vec<f64> = v.iter().map(|a| a - m).collect();
    let nf = n as f64;
    let mut s = dot(&u, &u) / nf;
    for k in 1..=lags.min(n - 1) {
        s += 2.0 * (1.0 - k as f64 / (lags as f64 + 1.0)) * dot(&u[k..], &u[..n - k]) / nf;
    }
    (m, if s > 0.0 { m / (s / nf).sqrt() } else { f64::NAN }, n)
}

pub fn nw_t(x: &[f64], lags: usize) -> f64 {
    nw_mean_t(x, lags, MIN_OBS).1
}

fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}
