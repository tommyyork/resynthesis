// Minimal harmonic-stack resynthesis engine.
//
// This variant is intentionally much simpler than SimpleResynth:
// - It treats the analyzed spectrum as a static magnitude "envelope".
// - It synthesizes a harmonic stack at the requested fundamental and
//   multiplies each harmonic by the envelope at that bin.
// - There is no pitch-shift of the input spectrum, no sparsity,
//   diffusion, fluff, or feedback – just a clean, pitched, musical core.
//
// It is used only by the offline "harmonic" tests so we can listen to
// the simplified algorithm in isolation from the full Resynthesis.cpp
// firmware path.

#ifndef RESYNTH_ENGINE_HARMONIC_H
#define RESYNTH_ENGINE_HARMONIC_H

#include "ResynthEngine.h" // reuse FFT, Grain, constants, Clamp, etc.

namespace resynth_engine {

struct SimpleHarmonicResynth
{
    float window[kFftSize];
    float mag_env[kNumBins + 1];
    float mag_smooth_coeff;
    bool  primed;

    float fundamental_hz_;
    float sample_rate_;
    int   max_harmonics_;

    // Fixed random phase per harmonic so successive grains are not all
    // aligned, while still keeping the spectrum stable. We support up
    // to 128 harmonics for very rich stacks.
    static constexpr int kMaxStoredHarmonics = 128;
    float harmonic_phase_[kMaxStoredHarmonics];

    void Init()
    {
        // Hann window
        for(size_t n = 0; n < kFftSize; ++n)
        {
            window[n] = 0.5f
                        * (1.0f - cosf(kTwoPi * static_cast<float>(n)
                                       / static_cast<float>(kFftSize - 1)));
        }
        for(size_t k = 0; k <= kNumBins; ++k)
            mag_env[k] = 0.0f;

        // Modest default smoothing so envelope follows the input but
        // does not jitter frame-to-frame.
        mag_smooth_coeff = 0.35f;
        primed = false;

        sample_rate_   = 48000.0f;
        fundamental_hz_ = 65.4f; // ~C2
        // Default to a fairly rich stack; tests can override.
        max_harmonics_ = 24;

        // Random but deterministic phases per harmonic.
        uint32_t state = 1u;
        for(int h = 0; h < kMaxStoredHarmonics; ++h)
        {
            state = state * 1664525u + 1013904223u;
            float t = static_cast<float>(state & 0x00FFFFFFu)
                      / static_cast<float>(0x01000000u);
            harmonic_phase_[h] = t * kTwoPi;
        }
    }

    void SetSmoothing(float alpha)
    {
        mag_smooth_coeff = Clamp(alpha, 0.0f, 1.0f);
    }

    void SetFundamentalHz(float f0_hz, float sample_rate_hz)
    {
        sample_rate_   = (sample_rate_hz > 0.0f) ? sample_rate_hz : 48000.0f;
        fundamental_hz_ = (f0_hz > 0.0f) ? f0_hz : 65.4f;
    }

    void SetMaxHarmonics(int n)
    {
        if(n < 1)
            n = 1;
        if(n > kMaxStoredHarmonics)
            n = kMaxStoredHarmonics;
        max_harmonics_ = n;
    }

    void StartGrainFromHistory(const float *history,
                               size_t       history_write_pos,
                               Grain       &grain)
    {
        Complex spectrum[kFftSize];

        // Analysis: windowed FFT of the most recent kFftSize samples.
        size_t idx = history_write_pos;
        for(size_t n = 0; n < kFftSize; ++n)
        {
            float s = history[idx];
            spectrum[n].re = s * window[n];
            spectrum[n].im = 0.0f;
            idx            = (idx + 1) % kFftSize;
        }

        FftInPlace(spectrum, kFftSize, false);

        // Compute a simple magnitude envelope per bin, with one-pole
        // smoothing so the envelope moves smoothly over time.
        for(size_t k = 0; k <= kNumBins; ++k)
        {
            float re  = spectrum[k].re;
            float im  = spectrum[k].im;
            float mag = sqrtf(re * re + im * im);

            if(!primed)
            {
                mag_env[k] = mag;
            }
            else
            {
                float alpha = (mag_smooth_coeff > 0.02f) ? mag_smooth_coeff : 0.02f;
                mag_env[k]  = mag_env[k] + alpha * (mag - mag_env[k]);
            }
        }
        primed = true;

        // Compute a coarse average magnitude so we can ensure the
        // envelope never becomes too "holey" or vanishingly small.
        float sum_mag = 0.0f;
        for(size_t k = 1; k < kNumBins; ++k)
            sum_mag += mag_env[k];
        float mean_mag = (kNumBins > 1)
                             ? (sum_mag / static_cast<float>(kNumBins - 1))
                             : 0.0f;
        if(mean_mag > 1e-6f)
        {
            // Blend each bin toward the mean to fill spectral gaps and
            // avoid "empty" sounding stacks when the source spectrum is
            // very sparse.
            const float w_env  = 0.7f;
            const float w_mean = 1.0f - w_env;
            for(size_t k = 0; k <= kNumBins; ++k)
                mag_env[k] = w_env * mag_env[k] + w_mean * mean_mag;
        }

        // Synthesis: build a fresh harmonic spectrum driven purely by
        // the requested fundamental and multiplied by the magnitude
        // envelope sampled at each harmonic bin.
        for(size_t k = 0; k < kFftSize; ++k)
        {
            spectrum[k].re = 0.0f;
            spectrum[k].im = 0.0f;
        }

        float bins_per_hz   = static_cast<float>(kFftSize) / sample_rate_;
        int   max_h         = max_harmonics_;
        const float harm_gain = 1.5f; // overall richness / level factor

        for(int h = 1; h <= max_h; ++h)
        {
            float f = fundamental_hz_ * static_cast<float>(h);
            if(f >= 0.5f * sample_rate_)
                break;

            float bin_f = f * bins_per_hz;
            size_t lo   = static_cast<size_t>(bin_f);
            size_t hi   = lo + 1;
            float  frac = bin_f - static_cast<float>(lo);

            if(lo > kNumBins)
                break;

            float env_lo = mag_env[lo];
            float env_hi = (hi <= kNumBins) ? mag_env[hi] : env_lo;
            float env    = (1.0f - frac) * env_lo + frac * env_hi;

            // Harmonic rolloff: 1/sqrt(h) keeps upper harmonics
            // present and audible while still biasing toward the
            // lower partials. The harm_gain pushes the whole stack
            // toward a richer sound.
            float harm_amp = harm_gain / sqrtf(static_cast<float>(h));
            float mag      = env * harm_amp;

            // Use a fixed random phase per harmonic for stability with
            // a bit of natural "spread".
            float phase = harmonic_phase_[(h - 1) % kMaxStoredHarmonics];
            float c     = cosf(phase);
            float s     = sinf(phase);

            spectrum[lo].re += mag * (1.0f - frac) * c;
            spectrum[lo].im += mag * (1.0f - frac) * s;
            if(hi <= kNumBins)
            {
                spectrum[hi].re += mag * frac * c;
                spectrum[hi].im += mag * frac * s;
            }
        }

        // Mirror to negative frequencies for a real IFFT.
        for(size_t k = 1; k < kNumBins; ++k)
        {
            spectrum[kFftSize - k].re = spectrum[k].re;
            spectrum[kFftSize - k].im = -spectrum[k].im;
        }

        FftInPlace(spectrum, kFftSize, true);

        // Window the IFFT and write it into the grain buffer.
        for(size_t n = 0; n < kFftSize; ++n)
            grain.buffer[n] = spectrum[n].re * window[n];

        // RMS match grain to the analysis window so levels are
        // comparable to the input.
        float rms_window = 0.0f;
        idx              = history_write_pos;
        for(size_t n = 0; n < kFftSize; ++n)
        {
            float s = history[idx] * window[n];
            rms_window += s * s;
            idx = (idx + 1) % kFftSize;
        }
        rms_window = sqrtf(rms_window / static_cast<float>(kFftSize));

        float rms_grain = 0.0f;
        for(size_t n = 0; n < kFftSize; ++n)
            rms_grain += grain.buffer[n] * grain.buffer[n];
        rms_grain = sqrtf(rms_grain / static_cast<float>(kFftSize));

        if(rms_grain > 1e-8f && rms_window > 1e-8f)
        {
            float gain = rms_window / rms_grain;
            if(gain > 0.25f && gain < 4.0f)
            {
                for(size_t n = 0; n < kFftSize; ++n)
                    grain.buffer[n] *= gain;
            }
        }

        // Drive the rich harmonic stack a bit harder; offline tests
        // apply soft clipping afterwards, so a modest global boost is
        // safe and helps avoid the impression of a thin / quiet tone.
        const float post_drive = 1.5f;
        for(size_t n = 0; n < kFftSize; ++n)
            grain.buffer[n] *= post_drive;

        grain.Start();
    }
};

} // namespace resynth_engine

#endif // RESYNTH_ENGINE_HARMONIC_H

