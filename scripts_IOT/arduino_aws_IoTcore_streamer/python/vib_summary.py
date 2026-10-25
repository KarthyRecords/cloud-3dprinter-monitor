# vib_summary.py - per-sensor vibration summary for the AWS stream
#
# Same maths as the local page (index.html): mean removed per axis,
# time-domain Grms / peak, one-sided Welch PSD (Hann, 50 % overlap,
# nfft 256 at 400 Hz), PSD-integrated Grms (should match), band Grms and
# the strongest spectral peaks per axis.
#
# Input: little-endian int16 x,y,z samples (ADXL345 full-res, 3.9 mg/LSB).

import math

try:
    import numpy as np
except Exception:          # summaries degrade to time-domain only
    np = None

G_PER_LSB = 0.0039
BANDS_HZ = [(0, 10), (10, 25), (25, 50), (50, 75), (75, 100), (100, 150), (150, 200)]
AXES = ("x", "y", "z")


def _r(v, n=5):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), n)


def _welch(x, fs, nfft):
    w = np.hanning(nfft)
    wss = float(np.sum(w * w))
    step = nfft // 2
    segs = [x[s:s + nfft] for s in range(0, len(x) - nfft + 1, step)]
    if not segs:
        return None, None
    acc = np.zeros(nfft // 2 + 1)
    for seg in segs:
        X = np.fft.rfft((seg - seg.mean()) * w)
        acc += (X.real ** 2 + X.imag ** 2)
    psd = acc / (fs * wss * len(segs))
    psd[1:-1] *= 2.0                              # one-sided (not DC / Nyquist)
    f = np.arange(len(psd)) * fs / nfft
    return f, psd


def _peaks(f, p, k=3, fmin=2.0, sep_bins=3):
    idx = [i for i in range(1, len(p) - 1) if f[i] >= fmin and p[i] >= p[i - 1] and p[i] >= p[i + 1]]
    idx.sort(key=lambda i: p[i], reverse=True)
    chosen = []
    for i in idx:
        if all(abs(i - j) > sep_bins for j in chosen):
            chosen.append(i)
        if len(chosen) == k:
            break
    return [[_r(f[i], 2), _r(p[i], 9)] for i in chosen]


def summarize(raw_bytes, odr, max_peaks=3):
    """raw_bytes: concatenated int16le x,y,z samples (oldest first)."""
    n = len(raw_bytes) // 6
    out = {"n": n, "odr": odr, "window_s": _r(n / odr if odr else 0, 3)}
    if n < 8:
        return out
    if np is None:                                 # pure-python fallback
        import struct
        v = struct.unpack("<%dh" % (3 * n), raw_bytes[:6 * n])
        cols = [[v[3 * i + a] * G_PER_LSB for i in range(n)] for a in range(3)]
        means, rms, pk = [], [], []
        for c in cols:
            m = sum(c) / n
            d = [q - m for q in c]
            means.append(_r(m, 4))
            rms.append(_r(math.sqrt(sum(q * q for q in d) / n)))
            pk.append(_r(max(abs(q) for q in d)))
        out.update(mean_g=means, grms_g=rms, peak_g=pk,
                   grms_total_g=_r(math.sqrt(sum(r * r for r in rms))))
        return out

    a = np.frombuffer(raw_bytes[:6 * n], dtype="<i2").reshape(n, 3).astype(np.float64) * G_PER_LSB
    mean = a.mean(axis=0)
    d = a - mean
    rms = np.sqrt((d ** 2).mean(axis=0))
    pk = np.abs(d).max(axis=0)
    out.update(mean_g=[_r(m, 4) for m in mean], grms_g=[_r(r) for r in rms],
               peak_g=[_r(p) for p in pk],
               crest=[_r(p / r if r > 0 else None, 2) for p, r in zip(pk, rms)],
               grms_total_g=_r(math.sqrt(float((rms ** 2).sum()))))
    nfft = 256 if odr >= 256 else 128
    if n >= nfft:
        psd_grms, bands, peaks = [], {}, {}
        for ai, ax in enumerate(AXES):
            f, p = _welch(d[:, ai], odr, nfft)
            df = odr / nfft
            psd_grms.append(_r(math.sqrt(float(p[1:].sum() * df))))
            bands[ax] = [_r(math.sqrt(float(p[(f >= lo) & (f < hi) & (f > 0)].sum() * df)))
                         for lo, hi in BANDS_HZ]
            peaks[ax] = _peaks(f, p, max_peaks)
        out.update(psd_grms_g=psd_grms, bands_hz=BANDS_HZ, bands_grms_g=bands,
                   peaks_hz_g2hz=peaks, df_hz=_r(odr / nfft, 4), nfft=nfft)
    return out
