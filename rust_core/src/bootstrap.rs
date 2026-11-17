//! Moving-block and stationary bootstraps with the same draw sequence as `lab.stats.bootstrap` for the same seed.

use crate::numpy_rng::Generator;

pub fn mean(x: &[f64]) -> f64 {
    x.iter().sum::<f64>() / x.len() as f64
}

/// `reps` values of stat(v[idx]), idx from non-circular blocks (`rng.choice(starts, k)` per replicate). Requires len(v) >= block.
pub fn moving_block_draws(v: &[f64], stat: &dyn Fn(&[f64]) -> f64, reps: usize, block: usize, seed: u64) -> Vec<f64> {
    let n = v.len();
    let mut rng = Generator::new(seed);
    let n_starts = (n - block + 1) as u64;
    let k = n.div_ceil(block);
    let mut sample = vec![0.0; n];
    (0..reps)
        .map(|_| {
            let mut pos = 0;
            for _ in 0..k {
                let s = rng.integers(n_starts) as usize;
                for j in 0..block {
                    if pos < n {
                        sample[pos] = v[s + j];
                        pos += 1;
                    }
                }
            }
            stat(&sample)
        })
        .collect()
}

/// Stationary bootstrap: restart with probability 1/block, else step to the next row (wrapping).
pub fn stationary_draws(v: &[f64], stat: &dyn Fn(&[f64]) -> f64, reps: usize, block: usize, seed: u64) -> Vec<f64> {
    let n = v.len();
    let mut rng = Generator::new(seed);
    let p = 1.0 / block as f64;
    let mut sample = vec![0.0; n];
    (0..reps)
        .map(|_| {
            let mut i = rng.integers(n as u64) as usize;
            for (t, slot) in sample.iter_mut().enumerate() {
                if t > 0 {
                    if rng.random() < p {
                        i = rng.integers(n as u64) as usize;
                    } else {
                        i = (i + 1) % n;
                    }
                }
                *slot = v[i];
            }
            stat(&sample)
        })
        .collect()
}

/// numpy.percentile (linear interpolation, numpy's `_lerp` form) of unsorted data.
pub fn percentile(data: &[f64], q: f64) -> f64 {
    let mut s = data.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let pos = q / 100.0 * (s.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = (lo + 1).min(s.len() - 1);
    let t = pos - lo as f64;
    let (a, b) = (s[lo], s[hi]);
    let d = b - a;
    if t >= 0.5 {
        b - d * (1.0 - t)
    } else {
        a + d * t
    }
}

fn ci(d: &[f64]) -> [f64; 2] {
    [percentile(d, 2.5), percentile(d, 97.5)]
}

fn finite(x: &[f64]) -> Vec<f64> {
    x.iter().copied().filter(|a| !a.is_nan()).collect()
}

/// 95% percentile interval under the moving-block bootstrap; NaN when the sample is shorter than two blocks.
pub fn moving_block_ci(x: &[f64], stat: &dyn Fn(&[f64]) -> f64, reps: usize, block: usize, seed: u64) -> [f64; 2] {
    let v = finite(x);
    if v.len() < 2 * block {
        return [f64::NAN, f64::NAN];
    }
    ci(&moving_block_draws(&v, stat, reps, block, seed))
}

pub fn stationary_ci(x: &[f64], stat: &dyn Fn(&[f64]) -> f64, reps: usize, block: usize, seed: u64) -> [f64; 2] {
    ci(&stationary_draws(&finite(x), stat, reps, block, seed))
}
