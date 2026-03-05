// Property / stability tests for the Resynthesis engine.
// These are intended for automated runs: they do not read or write WAVs
// and instead exercise the core engine on synthetic input, checking for
// NaNs/Infs and gross clipping.

#include "../ResynthEngine.h"

#include <cmath>
#include <cstdio>

using namespace resynth_engine;

static bool is_finite(float x)
{
    return std::isfinite(x);
}

// Simple helper: generate one frame of input (sine + small DC).
static float make_input_sample(size_t i, float sample_rate)
{
    float t      = static_cast<float>(i) / sample_rate;
    float freq   = 220.0f;
    float sine   = std::sin(2.0f * static_cast<float>(M_PI) * freq * t);
    float dc     = 0.05f;
    float signal = 0.5f * sine + dc;
    return signal;
}

static int run_simple_resynth_props()
{
    static constexpr unsigned kSampleRate = 48000;

    SimpleResynth resynth;
    Grain         grains[kNumGrains];
    resynth.Init();

    // Use a moderately animated but stable preset.
    resynth.SetSmoothing(0.6f);
    resynth.SetSpectralFlatten(0.3f);
    resynth.SetBrightDark(0.1f);
    resynth.SetSparsity(0.15f);
    resynth.SetPhaseDiffusion(0.1f);
    resynth.SetFluff(0.2f);
    resynth.SetPitchLockMode(true);
    resynth.SetPureResynthMode(false);
    resynth.SetFundamentalHz(110.0f, static_cast<float>(kSampleRate));

    for(size_t g = 0; g < kNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index   = 0;
    }

    float  input_history[kFftSize];
    size_t history_write_pos  = 0;
    size_t total_samples_seen = 0;
    float  grain_phase        = 0.0f;

    double sum_sq     = 0.0;
    double peak_abs   = 0.0;
    size_t sample_cnt = 0;

    auto startNextGrain = [&]() {
        size_t idx = 0;
        for(size_t g = 0; g < kNumGrains; ++g)
        {
            if(!grains[g].running)
            {
                idx = g;
                break;
            }
        }
        resynth.StartGrainFromHistory(input_history, history_write_pos, grains[idx]);
    };

    // Run for a few thousand samples to cover several FFT hops.
    static constexpr size_t kTotalFrames = kFftSize * 8;

    for(size_t i = 0; i < kTotalFrames; ++i)
    {
        float mono_in = make_input_sample(i, static_cast<float>(kSampleRate));
        input_history[history_write_pos] = mono_in;
        history_write_pos                = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if(total_samples_seen >= kFftSize)
        {
            grain_phase += 1.0f;
            while(grain_phase >= static_cast<float>(kHopSize))
            {
                startNextGrain();
                float hop       = static_cast<float>(kHopSize);
                float jitterMul = SimpleResynth::RandUniform(0.9f, 1.1f);
                grain_phase -= hop * jitterMul;
            }
        }

        float  wet          = 0.0f;
        size_t active_count = 0;
        for(size_t g = 0; g < kNumGrains; ++g)
        {
            if(grains[g].running)
            {
                wet += grains[g].Process();
                ++active_count;
            }
        }
        if(active_count > 0)
            wet *= 1.0f / (static_cast<float>(kHopDenom) * static_cast<float>(active_count));

        float out_mono = (active_count > 0) ? wet : mono_in;

        if(!is_finite(out_mono))
        {
            std::fprintf(stderr,
                         "FAIL: non-finite sample detected at frame %zu (value=%f)\n",
                         i,
                         out_mono);
            return 1;
        }

        double abs_v = std::fabs(static_cast<double>(out_mono));
        if(abs_v > peak_abs)
            peak_abs = abs_v;
        sum_sq += static_cast<double>(out_mono) * static_cast<double>(out_mono);
        ++sample_cnt;
    }

    if(sample_cnt == 0)
    {
        std::fprintf(stderr, "FAIL: no samples processed\n");
        return 1;
    }

    double rms = std::sqrt(sum_sq / static_cast<double>(sample_cnt));

    // Sanity bands: RMS should be comfortably non-zero and peak should not
    // explode far beyond a reasonable soft-clipped range.
    if(rms < 1e-4)
    {
        std::fprintf(stderr, "FAIL: RMS too low (%g)\n", rms);
        return 1;
    }
    if(peak_abs > 8.0)
    {
        std::fprintf(stderr, "FAIL: peak amplitude too large (%g)\n", peak_abs);
        return 1;
    }

    std::printf("SimpleResynth props OK (RMS=%g, peak=%g)\n", rms, peak_abs);
    return 0;
}

int main()
{
    int status = run_simple_resynth_props();
    if(status != 0)
        return status;
    return 0;
}

