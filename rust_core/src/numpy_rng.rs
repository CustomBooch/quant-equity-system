//! NumPy-compatible `Generator(PCG64(seed))` for a non-negative integer seed: `random()`, `integers(n)` and `choice(arange(m), k)`.

const INIT_A: u32 = 0x43b0_d7e5;
const MULT_A: u32 = 0x931e_8875;
const INIT_B: u32 = 0x8b51_f9dd;
const MULT_B: u32 = 0x58f3_8ded;
const MIX_MULT_L: u32 = 0xca01_f9dd;
const MIX_MULT_R: u32 = 0x4973_f715;
const XSHIFT: u32 = 16;
const POOL: usize = 4;
const PCG_MULT: u128 = 0x2360_ED05_1FC6_5DA4_4385_DF64_9FCC_F645;

fn hashmix(mut v: u32, hc: &mut u32) -> u32 {
    v ^= *hc;
    *hc = hc.wrapping_mul(MULT_A);
    v = v.wrapping_mul(*hc);
    v ^ (v >> XSHIFT)
}

fn mix(x: u32, y: u32) -> u32 {
    let r = MIX_MULT_L.wrapping_mul(x).wrapping_sub(MIX_MULT_R.wrapping_mul(y));
    r ^ (r >> XSHIFT)
}

/// SeedSequence(seed).generate_state(4, uint64)
fn seed_sequence_state(seed: u64) -> [u64; 4] {
    let entropy: Vec<u32> = if seed >> 32 == 0 { vec![seed as u32] } else { vec![seed as u32, (seed >> 32) as u32] };
    let mut pool = [0u32; POOL];
    let mut hc = INIT_A;
    for (i, p) in pool.iter_mut().enumerate() {
        *p = hashmix(*entropy.get(i).unwrap_or(&0), &mut hc);
    }
    for i_src in 0..POOL {
        for i_dst in 0..POOL {
            if i_src != i_dst {
                let v = hashmix(pool[i_src], &mut hc);
                pool[i_dst] = mix(pool[i_dst], v);
            }
        }
    }
    let mut hc = INIT_B;
    let mut w32 = [0u32; 8];
    for (i, w) in w32.iter_mut().enumerate() {
        let mut d = pool[i % POOL] ^ hc;
        hc = hc.wrapping_mul(MULT_B);
        d = d.wrapping_mul(hc);
        *w = d ^ (d >> XSHIFT);
    }
    let mut out = [0u64; 4];
    for (j, o) in out.iter_mut().enumerate() {
        *o = w32[2 * j] as u64 | (w32[2 * j + 1] as u64) << 32;
    }
    out
}

pub struct Generator {
    state: u128,
    inc: u128,
    has_u32: bool,
    buf_u32: u32,
}

impl Generator {
    pub fn new(seed: u64) -> Self {
        let s = seed_sequence_state(seed);
        let initstate = (s[0] as u128) << 64 | s[1] as u128;
        let initseq = (s[2] as u128) << 64 | s[3] as u128;
        let mut g = Generator { state: 0, inc: (initseq << 1) | 1, has_u32: false, buf_u32: 0 };
        g.step();
        g.state = g.state.wrapping_add(initstate);
        g.step();
        g
    }

    fn step(&mut self) {
        self.state = self.state.wrapping_mul(PCG_MULT).wrapping_add(self.inc);
    }

    pub fn next_u64(&mut self) -> u64 {
        self.step();
        let x = (self.state >> 64) as u64 ^ self.state as u64;
        x.rotate_right((self.state >> 122) as u32)
    }

    fn next_u32(&mut self) -> u32 {
        if self.has_u32 {
            self.has_u32 = false;
            self.buf_u32
        } else {
            let n = self.next_u64();
            self.has_u32 = true;
            self.buf_u32 = (n >> 32) as u32;
            n as u32
        }
    }

    /// `rng.random()`: uniform double in [0, 1) from 53 bits.
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / 9007199254740992.0)
    }

    /// `rng.integers(n)` (low = 0, exclusive high = n), for 1 <= n <= 2^32: Lemire's method on buffered 32-bit words.
    pub fn integers(&mut self, n: u64) -> u64 {
        assert!((1..=1 << 32).contains(&n), "range outside the 32-bit path");
        let rng = (n - 1) as u32;
        if rng == 0 {
            return 0;
        }
        if rng == u32::MAX {
            return self.next_u32() as u64;
        }
        let excl = rng as u64 + 1;
        let mut m = self.next_u32() as u64 * excl;
        let mut leftover = m & 0xFFFF_FFFF;
        if leftover < excl {
            let threshold = (u32::MAX - rng) as u64 % excl;
            while leftover < threshold {
                m = self.next_u32() as u64 * excl;
                leftover = m & 0xFFFF_FFFF;
            }
        }
        m >> 32
    }
}
