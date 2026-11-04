"""Synthesizes the 60 s trailer soundtrack as a 16-bit stereo WAV file.

Every sound is generated here from sine, sawtooth and noise sources, so the track
contains no third-party audio. The arrangement follows the scene cuts in trailer.html.

Usage: python3 music.py OUT.wav
"""

import sys
import wave

import numpy as np

SR = 44100
DUR = 60.0
BPM = 120.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
N = int(SR * DUR)
RNG = np.random.default_rng(7)

# Scene starts in trailer.html (crossfade begins); a whoosh marks each cut.
CUTS = [5.6, 13.6, 19.9, 26.2, 32.5, 38.8, 45.1]
OUTRO = 51.6
GROOVE_START = 6.0  # beat-aligned start of the groove (bar 4)


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def t_axis(n):
    return np.arange(n) / SR


def env_adsr(n, a, d, s, r):
    t = t_axis(n)
    length = n / SR
    e = np.where(t < a, t / max(a, 1e-6), 1.0)
    e = np.where((t >= a) & (t < a + d), 1 - (1 - s) * (t - a) / max(d, 1e-6), e)
    e = np.where(t >= a + d, s, e)
    rel_start = max(length - r, a)
    e = np.where(t > rel_start, e * np.clip(1 - (t - rel_start) / max(r, 1e-6), 0, 1), e)
    return e


def saw(freq, n, phase=0.0, max_h=None):
    """Band-limited sawtooth by additive synthesis."""
    t = t_axis(n)
    out = np.zeros(n)
    h_max = max_h or int(min(40, 9000 / freq))
    for h in range(1, h_max + 1):
        out += np.sin(2 * np.pi * freq * h * t + phase * h) / h
    return out * (2 / np.pi)


def one_pole_lp(x, cutoff):
    """Simple low-pass; cutoff may be a scalar or an array (per sample)."""
    cutoff = np.broadcast_to(np.asarray(cutoff, dtype=float), x.shape)
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a[i]) * x[i] + a[i] * acc
        y[i] = acc
    return y


def lp_fast(x, cutoff):
    """Static low-pass via FFT brick-wall with a soft knee (fast for long buffers)."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / np.sqrt(1 + (f / cutoff) ** 4)
    return np.fft.irfft(spec, len(x))


def hp_fast(x, cutoff):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / np.sqrt(1 + (cutoff / np.maximum(f, 1e-3)) ** 4)
    return np.fft.irfft(spec, len(x))


def place(buf, sig, start, gain=1.0):
    i = int(start * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i] * gain


def reverb(x, seconds=2.6, mix=0.3, seed=1):
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    ir = rng.standard_normal(n) * np.exp(-np.linspace(0, 7, n))
    ir = lp_fast(ir, 6000)
    ir /= np.sqrt(np.sum(ir**2))
    size = len(x) + n
    nfft = 1 << (size - 1).bit_length()
    wet = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[: len(x)]
    return x * (1 - mix) + wet * mix


# Chord progression (one chord per bar): Am9 - Fmaj7 - C(add9) - G6
PROG = [
    [57, 60, 64, 67, 71],  # A C E G B
    [53, 57, 60, 64, 67],  # F A C E G
    [48, 55, 60, 62, 64],  # C G C D E
    [55, 59, 62, 64, 67],  # G B D E G
]
BASS = [45, 41, 36, 43]
ARP_PATTERN = [0, 2, 3, 4, 3, 2, 1, 2]


def chord_at(t):
    return int(t // BAR) % 4


def build():
    L = np.zeros(N)
    R = np.zeros(N)
    pad = np.zeros(N)
    keys = np.zeros(N)
    drums = np.zeros(N)
    bass = np.zeros(N)
    fx = np.zeros(N)

    # --- Pads: whole track, one chord per bar, cutoff opens over time ---
    bars = int(np.ceil(DUR / BAR))
    for b in range(bars):
        start = b * BAR
        length = BAR + 0.6
        if start >= OUTRO:
            break
        n = int(length * SR)
        notes = PROG[b % 4]
        sig = np.zeros(n)
        for k, note in enumerate(notes):
            for det in (-0.07, 0.07):
                sig += saw(midi(note) * 2 ** (det / 12), n, phase=k + det, max_h=14)
        sig *= env_adsr(n, 0.5, 0.3, 0.85, 0.6)
        place(pad, sig, start, 0.05)
    # Outro chord: Am9 voiced wide, long tail
    n = int((DUR - OUTRO + 0.2) * SR)
    sig = np.zeros(n)
    for k, note in enumerate([45, 57, 64, 67, 71, 76]):
        for det in (-0.08, 0.0, 0.08):
            sig += saw(midi(note) * 2 ** (det / 12), n, phase=k * 0.7 + det, max_h=14)
    sig *= env_adsr(n, 0.05, 1.5, 0.6, 5.0)
    place(pad, sig, OUTRO, 0.045)
    t = t_axis(N)
    cutoff = 500 + 2600 * np.clip((t - 0.5) / 12, 0, 1)
    cutoff = np.where(t > OUTRO, 3200 - 2000 * np.clip((t - OUTRO) / 8, 0, 1), cutoff)
    pad = one_pole_lp(pad, cutoff)

    # --- Pluck arpeggio: 16th notes from the API scene on ---
    step = BEAT / 2
    s = 4 * BAR - 4 * step  # short pickup before the groove
    i = 0
    while s < OUTRO - 0.05:
        notes = PROG[chord_at(s + 0.01)]
        note = notes[ARP_PATTERN[i % len(ARP_PATTERN)]] + 12
        n = int(0.5 * SR)
        tt = t_axis(n)
        f = midi(note)
        sig = np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * 2 * f * tt) + 0.12 * np.sin(2 * np.pi * 3 * f * tt)
        sig *= np.exp(-tt * 9) * np.minimum(1, tt / 0.003)
        vel = 0.9 if i % 2 == 0 else 0.65
        place(keys, sig, s, 0.07 * vel)
        s += step
        i += 1

    # --- Drums: from the groove start to the outro ---
    def kick():
        n = int(0.45 * SR)
        tt = t_axis(n)
        f = 45 + 95 * np.exp(-tt * 28)
        ph = 2 * np.pi * np.cumsum(f) / SR
        return np.sin(ph) * np.exp(-tt * 7.5) + 0.3 * np.exp(-tt * 120) * RNG.standard_normal(n) * 0.2

    def hat(open_=False):
        n = int((0.18 if open_ else 0.05) * SR)
        tt = t_axis(n)
        noise = hp_fast(RNG.standard_normal(n), 7000)
        return noise * np.exp(-tt * (18 if open_ else 70))

    def clap():
        n = int(0.25 * SR)
        tt = t_axis(n)
        noise = lp_fast(hp_fast(RNG.standard_normal(n), 900), 5000)
        e = np.exp(-tt * 22) + 0.6 * np.exp(-np.maximum(tt - 0.012, 0) * 30) * (tt > 0.012)
        return noise * e

    duck = np.ones(N)
    beat_t = GROOVE_START
    b = 0
    full_drums = 13.6  # hats and claps join with the first feature scene
    while beat_t < OUTRO - 0.4:
        place(drums, kick(), beat_t, 0.55)
        i0 = int(beat_t * SR)
        dn = int(0.3 * SR)
        j = min(N, i0 + dn)
        duck[i0:j] = np.minimum(duck[i0:j], 1 - 0.45 * np.exp(-t_axis(j - i0) * 12))
        if beat_t >= full_drums:
            if b % 2 == 1:
                place(drums, clap(), beat_t, 0.12)
            place(drums, hat(open_=True), beat_t + BEAT / 2, 0.03)
        else:
            place(drums, hat(), beat_t + BEAT / 2, 0.025)
        beat_t += BEAT
        b += 1

    # --- Bass: 8th-note pulse on the root, from the first feature scene ---
    s = 13.6 - (13.6 % BEAT)
    while s < OUTRO - 0.2:
        root = BASS[chord_at(s + 0.01)]
        n = int(BEAT / 2 * SR)
        tt = t_axis(n)
        f = midi(root)
        sig = np.tanh(1.6 * (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * 2 * f * tt)))
        sig *= env_adsr(n, 0.005, 0.08, 0.7, 0.04)
        place(bass, sig, s, 0.16)
        s += BEAT / 2

    # --- FX: whoosh at every cut, riser into the outro, impact at the logo ---
    for c in CUTS:
        n = int(1.2 * SR)
        tt = t_axis(n)
        noise = RNG.standard_normal(n)
        shape = np.sin(np.pi * np.clip(tt / 1.2, 0, 1)) ** 2
        sweep = 400 + 5000 * shape
        place(fx, one_pole_lp(noise, sweep) * shape, c - 0.6, 0.06)
    n = int(4.0 * SR)
    tt = t_axis(n)
    noise = RNG.standard_normal(n)
    rise = (tt / 4.0) ** 2
    place(fx, one_pole_lp(noise, 300 + 9000 * rise) * rise, OUTRO - 4.0, 0.09)
    # Impacts: low boom with a short noise crack
    for at, g in ((0.2, 0.5), (OUTRO, 0.6)):
        n = int(2.5 * SR)
        tt = t_axis(n)
        f = 38 + 60 * np.exp(-tt * 10)
        boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 2.2)
        crack = lp_fast(RNG.standard_normal(n), 3000) * np.exp(-tt * 18) * 0.3
        place(fx, boom + crack, at, g)
    # Bell sparkle on the logo and the outro
    for at in (0.25, OUTRO + 0.05):
        for k, note in enumerate([81, 88, 93]):
            n = int(3.0 * SR)
            tt = t_axis(n)
            f = midi(note)
            sig = (np.sin(2 * np.pi * f * tt) + 0.4 * np.sin(2 * np.pi * f * 2.76 * tt)) * np.exp(-tt * 1.6)
            place(keys, sig, at + k * 0.09, 0.05)

    # --- Mix ---
    pad *= duck
    keys_wet = reverb(keys, 2.2, 0.35, seed=2)
    pad_wet = reverb(pad, 3.0, 0.4, seed=3)
    fx_wet = reverb(fx, 2.5, 0.25, seed=4)
    drums_wet = reverb(drums, 1.2, 0.12, seed=5)

    # Stereo: pads and keys get a slight Haas spread
    d = int(0.012 * SR)
    L += pad_wet + keys_wet * 0.8 + drums_wet + bass + fx_wet
    R += np.roll(pad_wet, d) + np.roll(keys_wet, d // 2) * 1.0 + drums_wet + bass + np.roll(fx_wet, d // 3)

    # Fade-in, final fade-out
    fade = np.clip(t / 0.05, 0, 1) * np.clip((DUR - t) / 2.0, 0, 1)
    L *= fade
    R *= fade
    st = np.stack([L, R], axis=1)
    st = np.tanh(st * 1.3) / np.tanh(1.3)
    rms = np.sqrt(np.mean(st**2))
    st *= 10 ** (-16 / 20) / rms
    peak = np.max(np.abs(st))
    if peak > 0.97:
        st *= 0.97 / peak
    return st


def main():
    out = sys.argv[1]
    st = build()
    data = (st * 32767).astype("<i2")
    with wave.open(out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


if __name__ == "__main__":
    main()
