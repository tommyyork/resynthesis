// Grain resynth for Daisy Patch SM.
//
// Inspired by the Resynthesis algorithm in the All Electric Smart Grid
// project by jvictor0:
//   https://github.com/jvictor0/theallelectricsmartgrid
//   (see private/src/Resynthesis.hpp)
//
// This implementation (FFT-based grains, phase propagation, spectral
// shaping, pitch shift, flatten, bright/dark) was written by GPT 5.1
// in Cursor in March 2026.

#include "daisy_patch_sm.h"
#include "daisysp.h"
#include "ResynthEngine.h"
#include "Compression.h"
#include "Shifting.h"
#include "ResynthParams.h"

#include <cmath>
#include <cstdint>

using namespace daisy;
using namespace daisysp;
using namespace patch_sm;
using namespace resynth_engine;
using namespace resynth_params;

// ----------------------------------------------------------------------
// Optional debug logging over JTAG/serial (enabled when DEBUG is set)
// ----------------------------------------------------------------------
#ifdef DEBUG
#define RES_DEBUG 1
#endif

#ifdef RES_DEBUG
#define RES_DEBUG_PRINTLN(...) patch.PrintLine(__VA_ARGS__)
#define RES_DEBUG_PRINT(...) patch.Print(__VA_ARGS__)
#else
#define RES_DEBUG_PRINTLN(...)
#define RES_DEBUG_PRINT(...)
#endif

// ----------------------------------------------------------------------
// Helpers for bipolar CV scaling (-5 V .. +5 V)
// ----------------------------------------------------------------------

static inline float CvToBipolar(float v)
{
    return v * 2.0f - 1.0f;
}

// ----------------------------------------------------------------------
// Daisy Patch SM integration
// ----------------------------------------------------------------------

DaisyPatchSM patch;
// B_8: Mode select — when pressed, the engine runs in partial‑based / spectral‑model
// mode; when released, grains are pitch‑locked to V/OCT.
Switch     mode_switch;
// B_7: MAX COMP — toggles a stronger Omnipressor-style compressor on the output.
Switch     max_comp_switch;

static SimpleResynth resynth;
static Grain         grains[kNumGrains];
static resynth_shifting::Shifting shifting_block;
static Svf                    input_hp;

// Input history ring buffer for analysis
static float  input_history[kFftSize];
static size_t history_write_pos  = 0;
static size_t total_samples_seen = 0;
static float  grain_phase        = 0.0f;
static float  time_scale         = 0.1f;
// One-pole smoothed spectral energy (0–1), used as a loudness component
// for the THOUGHTS CV but not exposed directly.
static float  cv_energy_smooth   = 0.1f;
static const float cv_energy_coeff = 0.01f;  // smoothing (~0.1s at 48kHz block rate)
// Chaotic "thoughts" CV state (logistic map with excitation-dependent parameter).
static float  thoughts_x         = 0.37f;    // state in (0,1)
static float  thoughts_r         = 2.8f;     // logistic parameter
static float  thoughts_r_drift   = 0.0f;     // slow random walk for r
static float  thoughts_smooth    = 0.0f;     // smoothed, bipolar output in [-1,1]
static const float thoughts_smooth_coeff = 0.02f; // slowish smoothing
// Smoothed wet gain to reduce level pumping when the number of active grains changes.
static float  wet_gain_state     = 1.0f;
// Simple loudness compensation for low smoothing: when spectral
// magnitudes track quickly (small smoothing), the perceived level
// tends to drop. This scalar gently boosts the wet path there.
static float  smoothing_gain_boost = 1.0f;

// Latched state for the MAX COMP software toggle driven by the momentary B_7
// switch, plus simple ~1.5 Hz blink timing for the status LED overlay on
// CV_OUT_2 (which otherwise follows grain energy).
static bool  max_comp_latched     = false;
static bool  max_comp_button_prev = false;
static float led_blink_timer      = 0.0f;
static bool  led_state            = false;
static float audio_sample_rate    = 48000.0f;
// Smoothed fundamental in Hz derived from the V/OCT CV so that
// discrete pitch steps are heard more like a VCO changing pitch
// (continuous but fast) instead of hard, clicky jumps.
static float last_fundamental_hz  = 0.0f;

// Compressor: normal (2:1, make output consistent as a sound source)
static patch_sm::Compressor comp_normal;

#ifdef RES_DEBUG
// Debug-logging cadence in audio samples (set from runtime sample rate)
static float    g_sample_rate            = 48000.0f;
static uint32_t g_debug_interval_samples = 48000; // ~1s by default
static uint32_t g_debug_sample_accum     = 0;
// Latched copies of mode state so we can log front-panel changes.
static bool     g_prev_partial_mode_on   = false;
static bool     g_prev_pitch_lock_on     = true;
static bool     g_prev_max_comp_on       = false;
#endif

#ifdef RES_DEBUG
static void PrintStartupStatus()
{
    patch.ProcessAnalogControls();
    mode_switch.Debounce();
    max_comp_switch.Debounce();

    RES_DEBUG_PRINTLN("Resynthesis (Patch SM) debug build starting");

    RES_DEBUG_PRINTLN("CV inputs at startup:");
    RES_DEBUG_PRINT("CV_1: " FLT_FMT3 "\tCV_2: " FLT_FMT3 "\tCV_3: " FLT_FMT3 "\tCV_4: " FLT_FMT3 "\n",
                    FLT_VAR3(patch.GetAdcValue(CV_1)),
                    FLT_VAR3(patch.GetAdcValue(CV_2)),
                    FLT_VAR3(patch.GetAdcValue(CV_3)),
                    FLT_VAR3(patch.GetAdcValue(CV_4)));
    RES_DEBUG_PRINT("CV_5: " FLT_FMT3 "\tCV_6: " FLT_FMT3 "\tCV_7: " FLT_FMT3 "\tCV_8: " FLT_FMT3 "\n",
                    FLT_VAR3(patch.GetAdcValue(CV_5)),
                    FLT_VAR3(patch.GetAdcValue(CV_6)),
                    FLT_VAR3(patch.GetAdcValue(CV_7)),
                    FLT_VAR3(patch.GetAdcValue(CV_8)));

    RES_DEBUG_PRINTLN("Buttons / switches at startup:");
    RES_DEBUG_PRINTLN("B7 MAX COMP: %s", max_comp_switch.Pressed() ? "ON" : "OFF");
    RES_DEBUG_PRINTLN("B8 Partial-based mode: %s", mode_switch.Pressed() ? "ON" : "OFF");
}
#endif

void StartNextGrain()
{
    // Find an available grain (or reuse the first one)
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
}

void AudioCallback(AudioHandle::InputBuffer  in,
                   AudioHandle::OutputBuffer out,
                   size_t                    size)
{
    patch.ProcessAnalogControls();
    mode_switch.Debounce();
    max_comp_switch.Debounce();

    bool partial_mode_on = mode_switch.Pressed();  // B_8: partial‑based / spectral model when ON
    bool pitch_lock_on   = !partial_mode_on;       // OFF = classic pitch‑lock mode
    // B_7 is wired as a momentary switch on the hardware, but the firmware
    // exposes it as a software toggle: each press toggles the MAX COMP state.
    bool max_comp_pressed = max_comp_switch.Pressed();
    if(max_comp_pressed && !max_comp_button_prev)
    {
        max_comp_latched = !max_comp_latched;
    }
    max_comp_button_prev = max_comp_pressed;
    bool max_comp_on     = max_comp_latched;

#ifdef RES_DEBUG
    // Log front‑panel mode / switch changes at the moment they toggle.
    if(partial_mode_on != g_prev_partial_mode_on)
    {
        RES_DEBUG_PRINTLN("MODE: B8 %s -> %s (partial_mode_on=%s, pitch_lock_on=%s)",
                          g_prev_partial_mode_on ? "ON" : "OFF",
                          partial_mode_on ? "ON" : "OFF",
                          partial_mode_on ? "true" : "false",
                          pitch_lock_on ? "true" : "false");
        g_prev_partial_mode_on = partial_mode_on;
        g_prev_pitch_lock_on   = pitch_lock_on;
    }
    if(max_comp_on != g_prev_max_comp_on)
    {
        RES_DEBUG_PRINTLN("MODE: MAX COMP %s",
                          max_comp_on ? "ON (Omnipressor-style)" : "OFF (normal 2:1)");
        g_prev_max_comp_on = max_comp_on;
    }
#endif

    // Read all 8 CVs (CV_1..CV_8).
    float v1 = patch.GetAdcValue(CV_1);
    float v2 = patch.GetAdcValue(CV_2);
    float v3 = patch.GetAdcValue(CV_3);
    float v4 = patch.GetAdcValue(CV_4);
    float v5 = patch.GetAdcValue(CV_5);
    float v6 = patch.GetAdcValue(CV_6);
    float v7 = patch.GetAdcValue(CV_7);
    float v8 = patch.GetAdcValue(CV_8);

    float drywet_knob   = v1;
    float time_cv       = v2;
    float fluff_knob    = v3;
    float tilt_knob     = v4;
    float voct_cv       = v5;
    float smooth_knob   = v6;
    float sparsity_cv   = v7;
    float diffusion_cv  = v8;

    // V/OCT: 0–10 V, 1 V/oct. 0 V = C0 (~16.35 Hz), 1 V = C1 (~32.7 Hz), 2 V = C2 (~65.4 Hz).
    float voct_volts = voct_cv * 10.0f;
    float target_fundamental_hz = VoctVoltsToFundamentalHz(voct_volts);
    // Smooth V/OCT‑driven pitch changes so that discrete CV steps feel
    // more like a fast glide on a VCO instead of an instantaneous jump
    // that can excite clicks in the granular engine.
    if(target_fundamental_hz <= 0.0f)
    {
        last_fundamental_hz = 0.0f;
    }
    else if(last_fundamental_hz <= 0.0f)
    {
        // First valid reading: jump directly to the requested pitch.
        last_fundamental_hz = target_fundamental_hz;
    }
    else
    {
        float block_dt = static_cast<float>(size) / audio_sample_rate;
        // Time constant of a few milliseconds: quick enough to track
        // envelopes and pitch CVs tightly, but long enough to soften
        // hard diatonic steps into clickless transitions.
        const float tau   = 0.004f; // ~4 ms
        float alpha       = 1.0f - expf(-block_dt / tau);
        if(alpha > 1.0f)
            alpha = 1.0f;
        last_fundamental_hz += alpha * (target_fundamental_hz - last_fundamental_hz);
    }
    float fundamental_hz = (last_fundamental_hz > 0.0f)
                               ? last_fundamental_hz
                               : target_fundamental_hz;
    resynth.SetFundamentalHz(fundamental_hz, patch.AudioSampleRate());
    // In hardware, B_8 OFF = pitch‑locked grains; B_8 ON = partial‑based model.
    resynth.SetPitchLockMode(pitch_lock_on);
    // Shifting front‑end: treat pitch‑lock as the condition under which the
    // input is snapped onto the V/OCT fundamental (or its frequency‑shifted
    // variant) before any further processing. COLOR controls crossfade between
    // pure pitch shifting and a Bode‑style frequency shifter near the top of
    // its range, while the target shift is always derived from V/OCT.
    shifting_block.SetPitchLockEnabled(pitch_lock_on);
    shifting_block.SetColor(fmap(tilt_knob, 0.0f, 1.0f));
    shifting_block.SetVoctFundamental(fundamental_hz);

    // When pitch lock is enabled, apply a two‑pole high‑pass filter whose
    // cutoff tracks just below the target fundamental implied by V/OCT.
    // This keeps DC and sub‑fundamental rumble out of the subsequent
    // pitch/frequency shifting and resynthesis stages while preserving
    // the perceived note.
    bool  hp_enabled   = pitch_lock_on && fundamental_hz > 0.0f;
    float hp_cutoff_hz = 0.9f * fundamental_hz; // just below the target note
    if(hp_cutoff_hz < 10.0f)
        hp_cutoff_hz = 10.0f;
    float hp_max = audio_sample_rate / 3.0f;
    if(hp_cutoff_hz > hp_max)
        hp_cutoff_hz = hp_max;
    if(hp_enabled)
        input_hp.SetFreq(hp_cutoff_hz);

    // Normalize time/sparsity/diffusion (CV_6–CV_8) to bipolar -1..1 for -5 V .. +5 V
    float time_bi      = CvToBipolar(time_cv);
    float sparsity_bi  = CvToBipolar(sparsity_cv);
    float diffusion_bi = CvToBipolar(diffusion_cv);

    float drywet = fmap(drywet_knob, 0.0f, 1.0f);
    // FLUFF: keep 0..1 but use a slightly curved response so higher values
    // ramp up more quickly from mid travel.
    float fluff = fmap(fluff_knob, 0.0f, 1.0f);
    resynth.SetFluff(powf(fluff, 1.2f));
    // Bright/dark tilt: extend slightly beyond -1..1 for a more obvious tonal shift.
    resynth.SetBrightDark(fmap(tilt_knob, -1.5f, 1.5f));

    // Time-stretch / grain density: <1 = slower, >1 = denser.
    // Map -5 V..+5 V (time_bi -1..1) so that:
    //   - The "normal" range of behaviour occupies roughly -1 V..+1 V.
    //   - -5 V yields an even more extreme slow-down than before.
    //   - +1 V..+5 V rapidly and exponentially approach the longest
    //     feasible time-stretch factor.
    {
        const float v = time_bi; // -1..1 for -5..+5 V
        const float center_band = 0.2f; // ±1 V region
        if(v >= -center_band && v <= center_band)
        {
            // Compress -1..+1 V into the full "normal" range that used
            // to live across the entire sweep: reuse the older mapping
            // on an internal -1..+1 helper variable.
            float e = v / center_band; // -1..1
            float shaped = (e >= 0.0f) ? powf(e, 0.5f) : -powf(-e, 0.5f);
            time_scale = powf(2.0f, shaped * 3.0f); // ~0.125x..8x within ±1 V
        }
        else if(v < -center_band)
        {
            // Super-slow region (-5 V..-1 V): exponentially extend
            // below the old minimum so -5 V reaches a much longer
            // stretch while -1 V meets the old "slow" edge smoothly.
            float t = (v + 1.0f) / (1.0f - center_band); // v=-1 ->0, v=-0.2->1
            if(t < 0.0f) t = 0.0f;
            if(t > 1.0f) t = 1.0f;
            const float min_extreme = 0.03125f; // 1/32x at -5 V
            const float min_normal  = 0.125f;   // ≈ old minimum
            float ratio = min_normal / min_extreme;
            time_scale = min_extreme * powf(ratio, t);
        }
        else // v > center_band
        {
            // Fast / dense region (+1 V..+5 V): start from the old
            // maximum at +1 V and then asymptotically approach a
            // "longest feasible" factor at +5 V.
            float t = (v - center_band) / (1.0f - center_band); // v=0.2->0, v=1->1
            if(t < 0.0f) t = 0.0f;
            if(t > 1.0f) t = 1.0f;
            const float max_normal   = 8.0f;   // ≈ old maximum at +1 V
            const float max_extreme  = 12.0f;  // target at +5 V
            float ratio = max_extreme / max_normal;
            time_scale  = max_normal * powf(ratio, t);
        }
    }

    // Sparsity and phase diffusion: map -5 V..+5 V (bipolar) back to 0..1 with a
    // squaring curve so most of the travel produces pronounced changes.
    float sparsity_norm  = 0.5f * (sparsity_bi + 1.0f);       // -1..1 -> 0..1
    float diffusion_norm = 0.5f * (diffusion_bi + 1.0f);      // -1..1 -> 0..1
    resynth.SetSparsity(sparsity_norm * sparsity_norm);       // emphasise high values
    resynth.SetPhaseDiffusion(diffusion_norm * diffusion_norm);

    // Update the front-panel status LED (CV_OUT_2) blink state when MAX COMP
    // is active. The LED overlay blinks at approximately 1.5 Hz as a visual
    // indicator but does not change the underlying energy-follow behaviour.
    if(max_comp_on)
    {
        float block_dt = static_cast<float>(size) / audio_sample_rate;
        led_blink_timer += block_dt;
        // Full period ≈ 0.67 s -> ~1.5 Hz blink, so half-period ≈ 0.33 s.
        const float half_period = 1.0f / (2.0f * 1.5f);
        while(led_blink_timer >= half_period)
        {
            led_blink_timer -= half_period;
            led_state        = !led_state;
        }
    }
    else
    {
        led_blink_timer = 0.0f;
        led_state       = false;
    }

#ifdef RES_DEBUG
    // Periodic debug dump of control, spectral and CV state.
    g_debug_sample_accum += size;
    if(g_debug_interval_samples == 0)
        g_debug_interval_samples = 48000;
    if(g_debug_sample_accum >= g_debug_interval_samples)
    {
        g_debug_sample_accum = 0;

        size_t active_grains = 0;
        for(size_t g = 0; g < kNumGrains; ++g)
        {
            if(grains[g].running)
                ++active_grains;
        }

        float smooth_amt    = smoothing;
        float fluff_amt     = fluff;
        float tilt_amt      = fmap(tilt_knob, -1.5f, 1.5f);
        float sparsity_amt  = 0.5f * (sparsity_bi + 1.0f);
        float diffusion_amt = 0.5f * (diffusion_bi + 1.0f);
        float thoughts_cv   = 2.5f * (thoughts_smooth + 1.0f);

        RES_DEBUG_PRINTLN("DBG: drywet=" FLT_FMT3 ", smooth=" FLT_FMT3
                          ", fluff=" FLT_FMT3 ", tilt=" FLT_FMT3,
                          FLT_VAR3(drywet),
                          FLT_VAR3(smooth_amt),
                          FLT_VAR3(fluff_amt),
                          FLT_VAR3(tilt_amt));

        RES_DEBUG_PRINTLN("DBG: V/OCT voct_volts=" FLT_FMT3 ", f_target=" FLT_FMT3
                          ", f_smooth=" FLT_FMT3 ", time_scale=" FLT_FMT3,
                          FLT_VAR3(voct_volts),
                          FLT_VAR3(target_fundamental_hz),
                          FLT_VAR3(fundamental_hz),
                          FLT_VAR3(time_scale));

        RES_DEBUG_PRINTLN("DBG: sparsity=" FLT_FMT3 ", phase_diff=" FLT_FMT3
                          ", spectral_energy=" FLT_FMT3 ", spectral_peakiness=" FLT_FMT3,
                          FLT_VAR3(sparsity_amt),
                          FLT_VAR3(diffusion_amt),
                          FLT_VAR3(resynth.last_frame_spectral_energy),
                          FLT_VAR3(resynth.last_frame_spectral_peakiness));

        RES_DEBUG_PRINTLN("DBG: grains=%u, wet_gain=" FLT_FMT3 ", smoothing_gain_boost=" FLT_FMT3,
                          static_cast<unsigned>(active_grains),
                          FLT_VAR3(wet_gain_state),
                          FLT_VAR3(smoothing_gain_boost));

        RES_DEBUG_PRINTLN("DBG: THOUGHTS r=" FLT_FMT3 ", x=" FLT_FMT3 ", cv=" FLT_FMT3,
                          FLT_VAR3(thoughts_r),
                          FLT_VAR3(thoughts_smooth),
                          FLT_VAR3(thoughts_cv));
    }
#endif

    for(size_t i = 0; i < size; i++)
    {
        float inL  = IN_L[i];
        float inR  = IN_R[i];
        float mono = 0.5f * (inL + inR);

        if(hp_enabled)
        {
            input_hp.Process(mono);
            mono = input_hp.High();
        }

        // Optional front-end pitch/frequency shifting block: when pitch-lock
        // is enabled, first determine the input's approximate fundamental and
        // shift it toward the V/OCT target note. COLOR near the top of its
        // range fades this behaviour into a Bode-style frequency shifter whose
        // shift rate also follows V/OCT. When pitch-lock is off the block is
        // effectively bypassed.
        mono = shifting_block.Process(mono);

        // Push into input history ring buffer
        input_history[history_write_pos] = mono;
        history_write_pos                = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        // Launch new grains once we have a full buffer.
        // Grain launch rate is controlled by time_scale, with a bit of random
        // jitter in the effective hop size so launches are not strictly on a
        // grid (more alien / scattered texture).
        if(total_samples_seen >= kFftSize)
        {
            grain_phase += time_scale;
            while(grain_phase >= static_cast<float>(kHopSize))
            {
                StartNextGrain();
                // Jittered hop: depth now follows FLUFF so low‑FLUFF
                // sounds are smoother and less gritty, while high‑FLUFF
                // patches get a more scattered cloud.
                float hop        = static_cast<float>(kHopSize);
                float fluff_now  = resynth.GetFluff();
                float jitter_amt = 0.02f + 0.18f * fluff_now; // ~±2%..±20%
                float lo         = 1.0f - jitter_amt;
                float hi         = 1.0f + jitter_amt;
                float jitterMul
                    = resynth_engine::SimpleResynth::RandUniform(lo, hi);
                grain_phase -= hop * jitterMul;
        }
    }

    // Smoothing: bias the panel so the centre position is already fairly
    // smooth, reserve the fastest/grittiest behaviour for the top of the
    // control, and increase smoothing automatically for extreme time‑stretch
    // factors.
    float smoothing_knob01 = fmap(smooth_knob, 0.0f, 1.0f);
    // Sweet‑spot curve: compress the lower half of the travel and keep most
    // of the musically useful, smoother region around 12 o'clock.
    float base_alpha_min = 0.08f;  // floor: never fully frame‑by‑frame
    float base_alpha_max = 0.6f;   // top end: intentionally fast / gritty
    float shaped = smoothing_knob01 * smoothing_knob01 * smoothing_knob01; // t^3
    float smoothing_alpha = base_alpha_min
                            + (base_alpha_max - base_alpha_min) * shaped;

    // Time‑stretch‑aware smoothing: when the engine is stretched very slow
    // or very dense, automatically increase smoothing so the texture becomes
    // more cloud‑like instead of a chattering grid of grains.
    float stretch_excess = 0.0f;
    if(time_scale > 0.0f)
    {
        float log2_ts = log2f(time_scale);
        stretch_excess = fabsf(log2_ts) / 3.0f; // ~0 at 1x, ->1 near 0.125x or 8x
        if(stretch_excess > 1.0f)
            stretch_excess = 1.0f;
    }
    float stretch_scale = 1.0f - 0.6f * stretch_excess; // 0.4..1.0
    if(stretch_scale < 0.4f)
        stretch_scale = 0.4f;
    smoothing_alpha *= stretch_scale;

    float smoothing = smoothing_alpha;
    resynth.SetSmoothing(smoothing);

    // Loudness compensation for low smoothing values: when smoothing is
    // small the spectrum follows transients closely and can feel quieter.
    // Give up to ~+4 dB boost at the lowest settings, tapering back to
    // unity as smoothing approaches 1.
    {
        float gain = 1.0f + 0.7f * (1.0f - smoothing); // 1.0 .. 1.7
        if(gain < 1.0f)
            gain = 1.0f;
        if(gain > 1.7f)
            gain = 1.7f;
        smoothing_gain_boost = gain;
    }

        // Sum all active grains (overlap-add), then normalize by active count so level is
        // consistent whether 1 or several grains are playing (reduces volume variation).
        float wet = 0.0f;
        size_t active_count = 0;
        for(size_t g = 0; g < kNumGrains; ++g)
        {
            if(grains[g].running)
            {
                wet += grains[g].Process();
                ++active_count;
            }
        }
        // Smooth the effective gain so changes in the number of overlapping
        // grains do not immediately translate into audible pumping.
        if(active_count > 0)
        {
            float target_gain = 1.0f
                                / (static_cast<float>(kHopDenom)
                                   * static_cast<float>(active_count));
            const float alpha = 0.01f; // ~100-frame time constant
            wet_gain_state += alpha * (target_gain - wet_gain_state);
        }
        wet *= (wet_gain_state * smoothing_gain_boost);

        // When no grains are active yet (first kFftSize samples), pass dry to avoid leading silence
        float out_mono = (active_count > 0)
            ? ((1.0f - drywet) * mono + drywet * wet)
            : mono;

        // Soft clip to reduce harsh peaks and further smooth level variation
        float lim = 0.95f;
        if (out_mono > lim)  out_mono = lim + (out_mono - lim) / (1.0f + (out_mono - lim));
        if (out_mono < -lim) out_mono = -lim + (out_mono + lim) / (1.0f - (out_mono + lim));

        // Compressor after soft clip to give a stable level. When B_7 (MAX COMP)
        // is on, use a stronger Omnipressor-style setting; otherwise use the
        // gentler "normal" 2:1 compression.
        if(max_comp_on)
        {
            comp_normal.SetParams(
                kCompThreshMax,
                kCompRatioMax,
                kCompMakeupMax,
                kCompAttack,
                kCompRelease);
        }
        else
        {
            comp_normal.SetParams(
                kCompThreshNormal,
                kCompRatioNormal,
                kCompMakeupNormal,
                kCompAttack,
                kCompRelease);
        }
        out_mono = comp_normal.Process(out_mono);

        // Final soft clip after compressor.
        if (out_mono > lim)  out_mono = lim + (out_mono - lim) / (1.0f + (out_mono - lim));
        if (out_mono < -lim) out_mono = -lim + (out_mono + lim) / (1.0f - (out_mono + lim));

        // Final output: currently purely the compressed mono resynth signal
        // without any additional reverb processing in the audio loop.
        float outL = out_mono;
        float outR = out_mono;

        OUT_L[i] = outL;
        OUT_R[i] = outR;
    }

    // Update internal loudness measure for THOUGHTS: a smoothed version of the
    // resynthesized spectral energy (RMS per frame), staying in 0–1.
    {
        float energy_in = fminf(1.0f, resynth.last_frame_spectral_energy * 5.0f);
        cv_energy_smooth += cv_energy_coeff * (energy_in - cv_energy_smooth);
    }

    // CV_OUT_1 "THOUGHTS": a chaotic, Brownian‑like CV derived tangentially
    // from the input's loudness and harmonic peakiness. It uses a logistic
    // map whose parameter r and effective step rate both increase with
    // "excitement".
    //
    // Excitement combines smoothed spectral energy (loudness) with a crude
    // harmonic‑peakiness measure from the resynth engine.
    {
        float energy_norm = cv_energy_smooth; // already ~0..1
        float peakiness   = resynth.last_frame_spectral_peakiness; // 0..1
        // Weight loudness slightly more than harmonic peakiness.
        float excitement  = 0.7f * energy_norm + 0.3f * peakiness;
        if (excitement < 0.0f) excitement = 0.0f;
        if (excitement > 1.0f) excitement = 1.0f;

        // Base logistic parameter r: low excitement -> near fixed/slow,
        // high excitement -> chaotic regime.
        const float r_min = 2.5f;
        const float r_max = 3.9f;

        // Very slow random drift of r so "thoughts" is only loosely tied to the input.
        float drift_range = 0.001f;
        thoughts_r_drift += resynth_engine::SimpleResynth::RandUniform(-drift_range, drift_range);
        if (thoughts_r_drift < -0.05f) thoughts_r_drift = -0.05f;
        if (thoughts_r_drift >  0.05f) thoughts_r_drift =  0.05f;

        thoughts_r = r_min + (r_max - r_min) * excitement + thoughts_r_drift;
        if (thoughts_r < r_min) thoughts_r = r_min;
        if (thoughts_r > r_max) thoughts_r = r_max;

        // More excited frames iterate the map more times per audio block, which
        // makes the motion quicker. Keep iteration count small to avoid numerical
        // issues and alias‑like harshness.
        int iters = 1 + static_cast<int>(excitement * 4.0f); // 1..5 iterations per block
        if (iters < 1) iters = 1;
        if (iters > 6) iters = 6;

        if (thoughts_x <= 0.0f || thoughts_x >= 1.0f || !(thoughts_x == thoughts_x))
        {
            thoughts_x = 0.37f;
        }
        for (int n = 0; n < iters; ++n)
        {
            thoughts_x = thoughts_r * thoughts_x * (1.0f - thoughts_x);
        }

        // Center to bipolar and scale range with excitement: quiet/simple input
        // -> small, slow wander; loud/harmonic input -> wider, quicker motion.
        float centered = (thoughts_x - 0.5f) * 2.0f; // nominally in (-1,1)
        if (centered < -1.3f) centered = -1.3f;
        if (centered >  1.3f) centered =  1.3f;

        float range = 0.25f + 0.75f * excitement; // 0.25..1.0
        float target_thoughts = centered * range;

        thoughts_smooth += thoughts_smooth_coeff * (target_thoughts - thoughts_smooth);

        // Map bipolar [-1,1] to 0..5 V.
        float thoughts_cv = 2.5f * (thoughts_smooth + 1.0f);
        if (thoughts_cv < 0.0f) thoughts_cv = 0.0f;
        if (thoughts_cv > 5.0f) thoughts_cv = 5.0f;
        patch.WriteCvOut(CV_OUT_1, thoughts_cv);
    }

    // CV_OUT_2: grain‑energy meter with MAX COMP status overlay.
    //
    // Base behaviour: CV_OUT_2 follows the current grain / spectral energy in
    // 0–5 V, using the same internal loudness measure that feeds THOUGHTS but
    // without the chaotic mapping. Quiet inputs yield low LED brightness and
    // CV level; louder, denser inputs push CV_OUT_2 towards 5 V.
    //
    // When MAX COMP is latched on, a logic‑style overlay blinks the output at
    // full intensity (~1.5 Hz). During the "on" phase of the blink the output
    // is forced to 5 V; during the "off" phase it returns to the underlying
    // grain‑energy level so the LED still reflects the current activity.
    {
        float energy_norm = cv_energy_smooth; // already ~0..1
        if(energy_norm < 0.0f)
            energy_norm = 0.0f;
        if(energy_norm > 1.0f)
            energy_norm = 1.0f;

        float energy_voltage = 5.0f * energy_norm;
        if(energy_voltage < 0.0f)
            energy_voltage = 0.0f;
        if(energy_voltage > 5.0f)
            energy_voltage = 5.0f;

        float led_voltage = (max_comp_on && led_state) ? 5.0f : energy_voltage;
        patch.WriteCvOut(CV_OUT_2, led_voltage);
    }
}

int main(void)
{
    patch.Init();
    audio_sample_rate = patch.AudioSampleRate();
    mode_switch.Init(patch.B8);
    max_comp_switch.Init(patch.B7);
    resynth.Init();
    shifting_block.Init(audio_sample_rate);
    input_hp.Init(audio_sample_rate);
    input_hp.SetRes(0.1f);
    input_hp.SetDrive(1.0f);

    // Single compressor: defaults to "normal" 2:1; B_7 (MAX COMP) engages a
    // stronger Omnipressor-style setting.
    comp_normal.Init(patch.AudioSampleRate());
    comp_normal.SetParams(
        kCompThreshNormal,
        kCompRatioNormal,
        kCompMakeupNormal,
        kCompAttack,
        kCompRelease);
#ifdef RES_DEBUG
    g_sample_rate            = patch.AudioSampleRate();
    g_debug_interval_samples = static_cast<uint32_t>(g_sample_rate);
    patch.StartLog(true);
    PrintStartupStatus();
#endif
    for(size_t g = 0; g < kNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index   = 0;
    }
    grain_phase        = 0.1f;
    time_scale         = 0.1f;
    total_samples_seen = 0;
    history_write_pos  = 0;

    patch.StartAudio(AudioCallback);
    while(1) {}
}

