// Pitch and frequency shifting front-end block for the Resynthesis engine.
//
// This lives immediately after the mono input mix, before any analysis /
// resynthesis, and is enabled whenever pitch-lock is active. It reuses the
// offline V/OCT pitch-detection logic (harmonic comb over note buckets) in a
// lightweight, streaming form and applies:
//   - time-domain pitch shifting via DaisySP's PitchShifter, to move the
//     detected input fundamental onto the V/OCT target note; and
//   - a simple single-sideband-style frequency shift that uses an analytic
//     signal approximation to create a Bode-like effect.
//
// COLOR controls a crossfade between these two behaviours near the top of its
// travel: up to ~3 o’clock the block behaves as a pure pitch shifter; from
// ~3 o’clock to fully CW it fades into the frequency shifter so that fully CW
// produces a Bode-style shift whose rate is set by V/OCT.

#ifndef RESYNTH_SHIFTING_H
#define RESYNTH_SHIFTING_H

#include <cmath>
#include <cstddef>
#include "daisysp.h"
#include "ResynthParams.h"

namespace resynth_shifting
{

// Lightweight harmonic-comb pitch detector adapted from test_resynth_voct.cpp:
// we maintain an STFT-based magnitude accumulator over a rolling window and
// estimate the fundamental as the note whose harmonics best explain the
// spectrum. This is intentionally coarse but musically stable.
class HarmonicPitchDetector
{
  public:
    HarmonicPitchDetector()
    {
        sample_rate_ = 48000.0f;
        Reset();
    }

    void Init(float sample_rate)
    {
        sample_rate_ = sample_rate > 0.0f ? sample_rate : 48000.0f;
        Reset();
    }

    // Push one mono sample into the detector. Call at audio rate.
    void PushSample(float s)
    {
        ring_[ring_write_] = s;
        ring_write_        = (ring_write_ + 1) % kFftSize;
        ++samples_since_update_;

        if(samples_since_update_ >= kHopSize)
        {
            samples_since_update_ = 0;
            RunAnalysisFrame();
        }
    }

    // Latest estimated fundamental frequency in Hz, or <= 0 if unknown.
    float GetEstimatedHz() const { return estimated_f0_hz_; }

  private:
    static constexpr size_t kFftSize  = 1024;
    static constexpr size_t kNumBins  = kFftSize / 2;
    static constexpr size_t kHopDenom = 4;
    static constexpr size_t kHopSize  = kFftSize / kHopDenom;

    struct Complex
    {
        float re;
        float im;
    };

    float   sample_rate_;
    float   window_[kFftSize];
    float   ring_[kFftSize];
    size_t  ring_write_;
    size_t  samples_since_update_;

    // Aggregated, decayed magnitude spectrum over recent frames.
    float mag_accum_[kNumBins + 1];

    float estimated_f0_hz_;

    void Reset()
    {
        ring_write_          = 0;
        samples_since_update_ = 0;
        estimated_f0_hz_     = 0.0f;
        for(size_t i = 0; i < kFftSize; ++i)
        {
            window_[i] = 0.5f
                         * (1.0f
                            - cosf(2.0f * static_cast<float>(M_PI)
                                   * static_cast<float>(i)
                                   / static_cast<float>(kFftSize - 1)));
            ring_[i] = 0.0f;
        }
        for(size_t k = 0; k <= kNumBins; ++k)
            mag_accum_[k] = 0.0f;
    }

    static void FftInPlace(Complex* data, size_t n)
    {
        // Simple radix-2 Cooley–Tukey, copied from ResynthEngine but localised.
        size_t j = 0;
        for(size_t i = 1; i < n; ++i)
        {
            size_t bit = n >> 1;
            while(j & bit)
            {
                j ^= bit;
                bit >>= 1;
            }
            j |= bit;
            if(i < j)
            {
                Complex tmp = data[i];
                data[i]     = data[j];
                data[j]     = tmp;
            }
        }

        for(size_t len = 2; len <= n; len <<= 1)
        {
            float ang    = -2.0f * static_cast<float>(M_PI) / static_cast<float>(len);
            float wlenRe = cosf(ang);
            float wlenIm = sinf(ang);
            for(size_t i = 0; i < n; i += len)
            {
                float wRe = 1.0f, wIm = 0.0f;
                for(size_t j2 = 0; j2 < len / 2; ++j2)
                {
                    Complex u = data[i + j2];
                    Complex v;
                    v.re = data[i + j2 + len / 2].re * wRe
                           - data[i + j2 + len / 2].im * wIm;
                    v.im = data[i + j2 + len / 2].re * wIm
                           + data[i + j2 + len / 2].im * wRe;
                    data[i + j2].re           = u.re + v.re;
                    data[i + j2].im           = u.im + v.im;
                    data[i + j2 + len / 2].re = u.re - v.re;
                    data[i + j2 + len / 2].im = u.im - v.im;
                    float nextWRe             = wRe * wlenRe - wIm * wlenIm;
                    float nextWIm             = wRe * wlenIm + wIm * wlenRe;
                    wRe                       = nextWRe;
                    wIm                       = nextWIm;
                }
            }
        }
    }

    void RunAnalysisFrame()
    {
        Complex spec[kFftSize];
        size_t  idx = ring_write_;
        for(size_t n = 0; n < kFftSize; ++n)
        {
            float s       = ring_[idx];
            spec[n].re    = s * window_[n];
            spec[n].im    = 0.0f;
            idx           = (idx + 1) % kFftSize;
        }

        FftInPlace(spec, kFftSize);

        const float decay = 0.9f;
        for(size_t k = 0; k <= kNumBins; ++k)
        {
            float re = spec[k].re;
            float im = spec[k].im;
            float m  = sqrtf(re * re + im * im);
            mag_accum_[k] = decay * mag_accum_[k] + (1.0f - decay) * m;
        }

        EstimateFundamental();
    }

    void EstimateFundamental()
    {
        // Map spectrum bins into MIDI note buckets, then run the same simple
        // harmonic comb as in the voct tests, but over a fixed, musical range.
        using namespace resynth_params;

        // Music-friendly range: ~C1..C7.
        const int min_midi = 24;
        const int max_midi = 96;
        const int num_notes
            = (max_midi >= min_midi) ? (max_midi - min_midi + 1) : 0;
        if(num_notes <= 0)
        {
            estimated_f0_hz_ = 0.0f;
            return;
        }

        float note_mags[96];
        int   note_counts[96];
        for(int i = 0; i < num_notes; ++i)
        {
            note_mags[i]   = 0.0f;
            note_counts[i] = 0;
        }

        const float bin_hz = sample_rate_ / static_cast<float>(kFftSize);
        for(size_t k = 1; k <= kNumBins; ++k)
        {
            float hz = static_cast<float>(k) * bin_hz;
            if(hz <= 0.0f)
                continue;
            float midi
                = 69.0f + 12.0f * log2f(hz / kA4Hz); // reuse kA4Hz from params
            int mi = static_cast<int>(floorf(midi + 0.5f));
            if(mi < min_midi || mi > max_midi)
                continue;
            int idx = mi - min_midi;
            if(idx < 0 || idx >= num_notes)
                continue;
            note_mags[idx] += mag_accum_[k];
            note_counts[idx] += 1;
        }
        for(int i = 0; i < num_notes; ++i)
        {
            if(note_counts[i] > 0)
                note_mags[i] /= static_cast<float>(note_counts[i]);
        }

        // Harmonic comb: pick MIDI note whose first few harmonics best
        // explain the bucketed spectrum.
        int   best_midi  = -1;
        float best_score = 0.0f;
        const int   kMaxHarmonics = 8;
        float       harm_weights[kMaxHarmonics + 1];
        for(int h = 1; h <= kMaxHarmonics; ++h)
            harm_weights[h] = 1.0f / static_cast<float>(h);

        for(int midi = min_midi; midi <= max_midi; ++midi)
        {
            float score = 0.0f;
            for(int h = 1; h <= kMaxHarmonics; ++h)
            {
                float midi_h = static_cast<float>(midi)
                               + 12.0f * log2f(static_cast<float>(h));
                int idx = static_cast<int>(floorf(midi_h + 0.5f))
                          - min_midi;
                if(idx < 0 || idx >= num_notes)
                    continue;
                float mag = note_mags[idx];
                if(mag <= 0.0f)
                    continue;
                score += harm_weights[h] * mag;
            }
            if(score > best_score)
            {
                best_score = score;
                best_midi  = midi;
            }
        }

        if(best_midi < 0 || best_score <= 0.0f)
        {
            estimated_f0_hz_ = 0.0f;
            return;
        }

        float hz = kA4Hz
                   * powf(2.0f, (static_cast<float>(best_midi) - 69.0f) / 12.0f);
        estimated_f0_hz_ = hz;
    }
};

// Shifting block: wraps a pitch detector, DaisySP pitch shifter, and a
// simple frequency shifter, and blends between them under COLOR.
class Shifting
{
  public:
    Shifting()
    {
        sample_rate_     = 48000.0f;
        pitch_lock_on_   = false;
        color_           = 0.0f;
        voct_f0_hz_      = 0.0f;
        freq_shift_phase_ = 0.0f;
    }

    void Init(float sample_rate)
    {
        sample_rate_ = sample_rate > 0.0f ? sample_rate : 48000.0f;
        detector_.Init(sample_rate_);
        pitch_shifter_.Init(sample_rate_);
        freq_shift_phase_ = 0.0f;
    }

    void SetPitchLockEnabled(bool enabled) { pitch_lock_on_ = enabled; }

    // COLOR knob (0..1). Above ~0.75 we begin crossfading from pitch shift
    // into frequency shift so that fully CW is pure frequency shift.
    void SetColor(float color)
    {
        if(color < 0.0f)
            color = 0.0f;
        if(color > 1.0f)
            color = 1.0f;
        color_ = color;
    }

    // V/OCT-derived fundamental target in Hz.
    void SetVoctFundamental(float f0_hz) { voct_f0_hz_ = f0_hz; }

    // Process one mono sample. When pitch-lock is disabled the block is
    // effectively bypassed (returns the input).
    float Process(float in)
    {
        detector_.PushSample(in);

        if(!pitch_lock_on_ || voct_f0_hz_ <= 0.0f)
            return in;

        float in_f0 = detector_.GetEstimatedHz();

        float pitch_out = in;
        if(in_f0 > 0.0f)
        {
            float ratio = voct_f0_hz_ / in_f0;
            if(ratio < 0.25f)
                ratio = 0.25f;
            if(ratio > 4.0f)
                ratio = 4.0f;
            float semitones = 12.0f * log2f(ratio);
            pitch_shifter_.SetTransposition(semitones);
            float temp = in;
            pitch_out  = pitch_shifter_.Process(temp);
        }

        // Simple single-sideband-like frequency shifter: approximate an
        // analytic signal with a fixed quadrature allpass pair and mix
        // upper sideband only. This is intentionally lightweight and
        // "Bode-ish" rather than mathematically perfect.
        float freq_out = ProcessFreqShift(in);

        // Crossfade region for COLOR near the top of its travel. Up to
        // ~3 o'clock (~0.75) we use pure pitch shift. From 0.75..1 we
        // fade into pure frequency shift.
        const float fade_start = 0.75f;
        float       x          = (color_ - fade_start) / (1.0f - fade_start);
        if(x < 0.0f)
            x = 0.0f;
        if(x > 1.0f)
            x = 1.0f;

        return (1.0f - x) * pitch_out + x * freq_out;
    }

  private:
    float                 sample_rate_;
    bool                  pitch_lock_on_;
    float                 color_;
    float                 voct_f0_hz_;
    HarmonicPitchDetector detector_;
    daisysp::PitchShifter pitch_shifter_;

    // Very small Hilbert-like quadrature approximation for the frequency
    // shifter: simple IIR allpass pair.
    float i1_[2] = {0.0f, 0.0f};
    float q1_[2] = {0.0f, 0.0f};
    float freq_shift_phase_;

    float ProcessFreqShift(float in)
    {
        // Fixed 1st-order allpass pair coefficients (two cascades per branch)
        // tuned around midband; these are borrowed from common Hilbert I/Q
        // approximations and give a broad ~90° phase offset.
        const float a1 = 0.6413f;
        const float a2 = 0.9260f;

        float xi = in;
        float yi = a1 * (xi - i1_[0]) + i1_[1];
        i1_[1]   = xi;
        i1_[0]   = yi;
        float zi = a2 * (yi - i1_[0]) + i1_[1];
        i1_[1]   = yi;
        i1_[0]   = zi;

        float xq = in;
        float yq = a2 * (xq - q1_[0]) + q1_[1];
        q1_[1]   = xq;
        q1_[0]   = yq;
        float zq = a1 * (yq - q1_[0]) + q1_[1];
        q1_[1]   = yq;
        q1_[0]   = zq;

        float shift_hz = voct_f0_hz_;
        if(shift_hz <= 0.0f)
            shift_hz = 1.0f;
        float phase_inc = 2.0f * static_cast<float>(M_PI) * shift_hz
                          / sample_rate_;
        freq_shift_phase_ += phase_inc;
        if(freq_shift_phase_ > 2.0f * static_cast<float>(M_PI))
            freq_shift_phase_ -= 2.0f * static_cast<float>(M_PI);

        float c = cosf(freq_shift_phase_);
        float s = sinf(freq_shift_phase_);

        float upper = xi * c - zq * s;
        return upper;
    }
};

} // namespace resynth_shifting

#endif // RESYNTH_SHIFTING_H

