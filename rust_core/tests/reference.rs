//! Compares the Rust code with fixtures written by the Python reference (tools/make_fixtures.py): NumPy random streams, Newey-West t, both bootstraps.
//! Fixture format: one record per line group; floats are IEEE-754 bit patterns in hex ("f" + 16 digits), integers are "i" + decimal.

use rust_core::bootstrap::{mean, moving_block_ci, stationary_ci};
use rust_core::hac::nw_mean_t;
use rust_core::numpy_rng::Generator;

fn lines() -> Vec<Vec<String>> {
    std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/reference.txt"))
        .unwrap()
        .lines()
        .map(|l| l.split_whitespace().map(String::from).collect())
        .collect()
}

fn fl(tok: &str) -> f64 {
    f64::from_bits(u64::from_str_radix(tok.strip_prefix('f').expect("float token"), 16).unwrap())
}

fn int(tok: &str) -> u64 {
    tok.strip_prefix('i').expect("int token").parse().unwrap()
}

fn floats(l: &[String]) -> Vec<f64> {
    l[1..].iter().map(|t| fl(t)).collect()
}

fn same(a: f64, b: f64, tol: f64) -> bool {
    (a.is_nan() && b.is_nan()) || (a - b).abs() <= tol * b.abs().max(1.0)
}

#[test]
fn numpy_random_streams_are_reproduced_exactly() {
    let ls = lines();
    let mut n_seeds = 0;
    for (k, l) in ls.iter().enumerate() {
        if l[0] != "RNG" {
            continue;
        }
        n_seeds += 1;
        let seed: u64 = l[1].parse().unwrap();
        let (random, int204, mixed, choice) = (&ls[k + 1], &ls[k + 2], &ls[k + 3], &ls[k + 4]);
        let mut g = Generator::new(seed);
        for (i, t) in random[1..].iter().enumerate() {
            assert_eq!(g.random().to_bits(), fl(t).to_bits(), "seed {seed} random #{i}");
        }
        let mut g = Generator::new(seed);
        for (i, t) in int204[1..].iter().enumerate() {
            assert_eq!(g.integers(204), int(t), "seed {seed} integers #{i}");
        }
        // interleaved buffered 32-bit and 64-bit draws, as the stationary bootstrap does
        let mut g = Generator::new(seed);
        for (i, t) in mixed[1..].iter().enumerate() {
            if i % 3 == 0 {
                assert_eq!(g.integers(204), int(t), "seed {seed} mixed #{i}");
            } else {
                assert_eq!(g.random().to_bits(), fl(t).to_bits(), "seed {seed} mixed #{i}");
            }
        }
        let mut g = Generator::new(seed);
        for (i, t) in choice[1..18].iter().enumerate() {
            assert_eq!(g.integers(193), int(t), "seed {seed} choice #{i}");
        }
        assert_eq!(g.random().to_bits(), fl(&choice[18]).to_bits(), "seed {seed} draw after choice");
    }
    assert_eq!(n_seeds, 4);
}

#[test]
fn newey_west_matches_python() {
    let ls = lines();
    let mut checked = 0;
    for (k, l) in ls.iter().enumerate() {
        if l[0] != "NW" {
            continue;
        }
        let x = floats(&ls[k + 1]);
        for lag_line in &ls[k + 2..k + 5] {
            let lag: usize = lag_line[1].parse().unwrap();
            let (m, t, n) = nw_mean_t(&x, lag, 12);
            let (wm, wt, wn) = (fl(&lag_line[2]), fl(&lag_line[3]), int(&lag_line[4]) as usize);
            assert!(same(m, wm, 1e-12) && same(t, wt, 1e-12) && n == wn, "lag {lag}: got ({m},{t},{n}) want ({wm},{wt},{wn})");
            checked += 1;
        }
    }
    assert_eq!(checked, 18);
}

fn bootstrap_cases(tag: &str, run: impl Fn(&[f64], usize, usize, u64) -> [f64; 2]) {
    let ls = lines();
    let mut checked = 0;
    for (k, l) in ls.iter().enumerate() {
        if l[0] != tag {
            continue;
        }
        let (reps, block, seed): (usize, usize, u64) = (l[1].parse().unwrap(), l[2].parse().unwrap(), l[3].parse().unwrap());
        let got = run(&floats(&ls[k + 1]), reps, block, seed);
        let want = floats(&ls[k + 2]);
        assert!(same(got[0], want[0], 1e-12) && same(got[1], want[1], 1e-12), "{tag}: got {got:?} want {want:?}");
        checked += 1;
    }
    assert_eq!(checked, 3);
}

#[test]
fn stationary_bootstrap_matches_python() {
    bootstrap_cases("STATIONARY", |x, reps, block, seed| stationary_ci(x, &mean, reps, block, seed));
}

#[test]
fn moving_block_bootstrap_matches_python() {
    bootstrap_cases("MOVING", |x, reps, block, seed| moving_block_ci(x, &mean, reps, block, seed));
}

#[test]
fn short_samples_give_nan() {
    assert!(nw_mean_t(&[1.0; 11], 6, 12).1.is_nan());
    assert!(moving_block_ci(&[0.0; 20], &mean, 10, 12, 1)[0].is_nan());
}
