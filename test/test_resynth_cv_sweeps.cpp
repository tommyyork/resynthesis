// Offline test: process each sample in the samples folder with each of the 8 CV
// parameters swept in turn. Writes to separate output directories per test type.
// Output names: {input_basename}_{test_suffix}.wav
// Build: make cv_sweeps (from test/) or make test_cv_sweeps (from Resynthesis/).

#include "../ResynthEngine.h"
#include "../ResynthParams.h"
#include "wav_io.h"
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <algorithm>

#ifdef _WIN32
#include <direct.h>
#define mkdir(path, mode) _mkdir(path)
#include <io.h>
#else
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#endif

static constexpr unsigned kSampleRate = 48000;

// Separate output directories per test type and mode
static const char kOutCvSweepPitch[]            = "out/cv_sweep";
static const char kOutCvSweepMaxcompPitch[]     = "out/cv_sweep_maxcomp";
static const char kOutCvSweepPartial[]          = "out/cv_sweep_partial";
static const char kOutCvSweepMaxcompPartial[]   = "out/cv_sweep_maxcomp_partial";

// MAX COMP compressor (matches firmware when B_7 is on)
using namespace resynth_params;

static const float kMinAvgLevelDbFs = -60.0f;

// Very simple mono reverb to approximate having Plateau (B_7) engaged during tests.
// Uses a feedback delay with a one-pole lowpass in the feedback path.
struct SimplePlateauSim {
    static constexpr float kDelaySeconds = 0.7f;
    static constexpr float kFeedback = 0.75f;
    static constexpr float kDamp = 0.3f;

    std::vector<float> buffer;
    size_t index = 0;
    float lpState = 0.0f;

    void init(unsigned sampleRate) {
        size_t delaySamples = static_cast<size_t>(kDelaySeconds * sampleRate);
        if (delaySamples < 1)
            delaySamples = 1;
        buffer.assign(delaySamples, 0.0f);
        index = 0;
        lpState = 0.0f;
    }

    float process(float in) {
        float y = buffer[index];
        // Lowpass the feedback path
        lpState = (1.0f - kDamp) * y + kDamp * lpState;
        buffer[index] = in + kFeedback * lpState;
        index++;
        if (index >= buffer.size())
            index = 0;
        return y;
    }
};

struct CvSweepTest {
    const char* name;           // short name for filename
    const char* description;    // one-line description
};

static const CvSweepTest kCvTests[] = {
    { "cv1_offer_feed",     "OFFER send+mix: 0% dry (unshifted) -> 100% pitched granular/harmonic voice over full sample when V/OCT is active" },
    { "cv2_timestretch",    "Time stretch 0.5x -> 4x over full sample (avoids too-few-grains near-silence)" },
    { "cv3_flatten",        "Spectral flatten 0.10 -> 1 over full sample (original spectrum -> whitened / formant-rich)" },
    { "cv4_tilt",           "Bright/dark tilt -1 -> 1 over full sample (even vs odd harmonic emphasis via the harmonic scaffold in both modes)" },
    { "cv5_voct",           "V/OCT sweep 1 V -> 4 V over full sample (sample-driven, quantized over 3 octaves)" },
    { "cv6_smoothing",      "Magnitude smoothing 0.10 -> 0.95 over full sample (clear transients -> glassy pads)" },
    { "cv7_sparsity",       "Spectral sparsity 0 -> 0.9 over full sample (ring-mod / formant-like at high end)" },
    { "cv8_phase_diffusion","Phase diffusion 0 -> 1 over full sample (clear to noisy/metallic)" },
};
static const size_t kNumCvTests = sizeof(kCvTests) / sizeof(kCvTests[0]);

// Discover all .wav files in a directory. Returns paths like "samples/foo.wav".
static std::vector<std::string> discover_wav_files(const char* dir)
{
    std::vector<std::string> out;
#ifdef _WIN32
    std::string pattern = std::string(dir) + "\\*.wav";
    struct _finddata_t fd;
    intptr_t h = _findfirst(pattern.c_str(), &fd);
    if (h == -1) return out;
    do {
        if (!(fd.attrib & _A_SUBDIR) && strstr(fd.name, ".wav"))
            out.push_back(std::string(dir) + "/" + fd.name);
    } while (_findnext(h, &fd) == 0);
    _findclose(h);
#else
    DIR* d = opendir(dir);
    if (!d) return out;
    struct dirent* e;
    while ((e = readdir(d)) != nullptr) {
        size_t len = strlen(e->d_name);
        if (len > 4 && strcmp(e->d_name + len - 4, ".wav") == 0)
            out.push_back(std::string(dir) + "/" + e->d_name);
    }
    closedir(d);
#endif
    std::sort(out.begin(), out.end());
    return out;
}

static void apply_max_comp(float* buf, size_t num_frames)
{
    float env = 0.0f;
    const float attack_coeff  = 1.0f - std::exp(-1.0f / (kCompAttack * (float)kSampleRate));
    const float release_coeff = 1.0f - std::exp(-1.0f / (kCompRelease * (float)kSampleRate));
    for (size_t i = 0; i < num_frames; ++i) {
        float x = buf[i];
        if (x > kSoftClipLim)  x = kSoftClipLim + (x - kSoftClipLim) / (1.0f + (x - kSoftClipLim));
        if (x < -kSoftClipLim) x = -kSoftClipLim + (x + kSoftClipLim) / (1.0f - (x + kSoftClipLim));
        float in_peak = std::fabs(x);
        float coeff = (in_peak > env) ? attack_coeff : release_coeff;
        env += coeff * (in_peak - env);
        float gain = 1.0f;
        if (env > 1e-6f) {
            if (env <= kCompThreshMax)
                gain = std::pow(kCompThreshMax / env, 0.5f);
            else
                gain = std::pow(kCompThreshMax / env, 1.0f - 1.0f / kCompRatioMax);
            gain *= kCompMakeupMax;
        }
        x *= gain;
        if (x > kSoftClipLim)  x = kSoftClipLim + (x - kSoftClipLim) / (1.0f + (x - kSoftClipLim));
        if (x < -kSoftClipLim) x = -kSoftClipLim + (x + kSoftClipLim) / (1.0f - (x + kSoftClipLim));
        buf[i] = x;
    }
}

static float compute_rms_dbfs(const float* buf, size_t n)
{
    double sum_sq = 0.0;
    for (size_t i = 0; i < n; ++i) { double x = (double)buf[i]; sum_sq += x * x; }
    double rms = (n > 0 && sum_sq > 0.0) ? std::sqrt(sum_sq / (double)n) : 1e-10;
    return 20.0f * (float)std::log10(rms > 1e-10 ? rms : 1e-10);
}

static bool load_input(const char* path,
                       std::vector<float>& mono,
                       unsigned sampleRate)
{
    std::vector<float> inputSamples;
    WavInfo info;
    if (!LoadWav(path, inputSamples, info))
        return false;
    if (info.sampleRate != sampleRate)
        return false;
    size_t n = info.numFrames;
    if (n == 0)
        return false;
    mono.resize(n);
    if (info.numChannels == 1) {
        for (size_t i = 0; i < n; ++i)
            mono[i] = inputSamples[i];
    } else {
        for (size_t i = 0; i < n; ++i)
        {
            size_t idx = i * 2;
            float l = inputSamples[idx];
            float r = (idx + 1 < inputSamples.size()) ? inputSamples[idx + 1] : l;
            mono[i] = 0.5f * (l + r);
        }
    }
    return true;
}

static bool run_one_cv_test(
    size_t cv_index,
    const float* mono,
    size_t num_frames,
    const char* out_basename,
    const char* out_dir,
    bool max_comp_on,
    bool pitch_lock_on)
{
    using namespace resynth_engine;
    SimpleResynth resynth;
    Grain grains[kNumGrains];
    resynth.Init();
    // Run either pitch‑locked or partial‑based / spectral‑model mode depending
    // on the requested test pass.
    resynth.SetPitchLockMode(pitch_lock_on);
    for (size_t g = 0; g < kNumGrains; ++g) {
        grains[g].running = false;
        grains[g].index = 0;
    }

    float input_history[kFftSize];
    size_t history_write_pos = 0;
    size_t total_samples_seen = 0;
    float grain_phase = 0.0f;
    std::vector<float> output(num_frames);
    float active_smooth = 0.0f;

    // Smoothed fundamental Hz so V/OCT steps behave more like a fast glide
    // (reduces clicks when the target pitch jumps).
    float fundamental_hz_smooth = 0.0f;
    // Short fade-in envelope applied around V/OCT note changes in the
    // pitch-locked cv5 test to further soften transitions without
    // touching the MAX COMP aggressiveness itself.
    float voct_change_env = 1.0f;
    int   voct_change_env_samples = 0;
    const int kVoctChangeFadeSamples = 256; // ~5.3 ms at 48 kHz

    // Simulate B_7 (Plateau) toggled on: run output through a simple reverb and
    // mix 50/50 dry/wet, similar to the hardware path. For the V/OCT sweep in
    // pitch‑locked mode we optionally bypass this so the V/OCT scale is heard
    // as cleanly as possible.
    SimplePlateauSim reverb;
    reverb.init(kSampleRate);

    int last_voct_step = -1;  // for cv5_voct: detect quantized note changes

    auto startNextGrain = [&]() {
        size_t idx = 0;
        for (size_t g = 0; g < kNumGrains; ++g) {
            if (!grains[g].running) { idx = g; break; }
        }
        resynth.StartGrainFromHistory(input_history, history_write_pos, grains[idx]);
    };

    for (size_t i = 0; i < num_frames; ++i) {
        float t = (float)i / (float)num_frames;  // 0 .. 1 over this test's duration

        // Neutral "glassy" defaults; override the one under test. Default V/OCT = 2 V (C2).
        // Dry/wet 100% wet for sweeps 2–8 so the parameter under test is heard clearly.
        float drywet = 1.0f;
        float smoothing = 0.20f;       // default smoothing now matches firmware neutral
        float flatten = 0.15f;         // mostly original spectral shape
        float tilt = 0.1f;             // gently bright by default
        float fundamental_hz = VoctVoltsToFundamentalHz(2.0f);  // 2 V = C2 (~65.4 Hz)
        float time_scale = 1.0f;
        float sparsity = 0.15f;        // dense spectrum for glassy tones
        float phase_diffusion = 0.1f;  // mostly coherent phase for clarity

        // In pitch‑locked mode for the dedicated V/OCT sweep (cv5_voct), use a
        // cleaner, more calibration‑style preset so the perceived note follows
        // V/OCT steps closely.
        bool is_voct_test = (cv_index == 4);
        if (pitch_lock_on && is_voct_test)
        {
            smoothing       = 0.20f;
            flatten         = 0.10f;
            tilt            = 0.05f;
            sparsity        = 0.10f;
            phase_diffusion = 0.08f;
            time_scale      = 1.5f;  // denser grains for quicker tracking
        }

        switch (cv_index) {
            case 0: drywet = t; break;  // CV1 sweep: 0% -> 100% wet
            case 1: {
                // CV2 "timestretch": reuse the firmware mapping so rendered sweeps
                // match hardware behaviour (normal range near -1 V..+1 V, more
                // extreme slow/fast regions toward ±5 V).
                float v = -1.0f + 2.0f * t; // sweep -1..1
                const float center_band = 0.2f;
                if(v >= -center_band && v <= center_band)
                {
                    float e = v / center_band; // -1..1
                    float shaped = (e >= 0.0f) ? powf(e, 0.5f) : -powf(-e, 0.5f);
                    time_scale = powf(2.0f, shaped * 3.0f); // ~0.125x..8x within ±1 V
                }
                else if(v < -center_band)
                {
                    float tt = (v + 1.0f) / (1.0f - center_band); // v=-1 ->0, v=-0.2->1
                    if(tt < 0.0f) tt = 0.0f;
                    if(tt > 1.0f) tt = 1.0f;
                    const float min_extreme = 0.03125f; // 1/32x
                    const float min_normal  = 0.125f;
                    float ratio = min_normal / min_extreme;
                    time_scale = min_extreme * powf(ratio, tt);
                }
                else
                {
                    float tt = (v - center_band) / (1.0f - center_band); // v=0.2->0, v=1->1
                    if(tt < 0.0f) tt = 0.0f;
                    if(tt > 1.0f) tt = 1.0f;
                    const float max_normal   = 8.0f;
                    const float max_extreme  = 12.0f;
                    float ratio = max_extreme / max_normal;
                    time_scale  = max_normal * powf(ratio, tt);
                }
                break;
            }
            // CV3 "flatten" interpreted as a bipolar -5 V..+5 V: keep
            // the current neutral value for the entire negative half
            // (-5 V..0 V), then increase from there for 0..+5 V.
            case 2: {
                float baseline = 0.10f; // value currently used around 0 V
                if (t <= 0.5f) {
                    flatten = baseline;
                } else {
                    float u = (t - 0.5f) * 2.0f; // 0..1 for 0..+5 V
                    flatten = baseline + u * 0.90f;
                }
                break;
            }
            // CV4 "tilt" as -5 V..+5 V: map the most negative voltages
            // (-5 V) to the previous -2 V setting (≈ -0.4 in the old
            // -1..1 linear map), then fan out towards +1 across the
            // rest of the sweep.
            case 3: {
                float x = 2.0f * t - 1.0f; // -1..1
                if (x <= -0.6f) {
                    tilt = -0.4f;
                } else {
                    float u = (x + 0.6f) / 1.6f; // 0 at -0.6, 1 at +1
                    tilt = -0.4f + u * (1.0f + 0.4f); // -0.4 -> +1
                }
                break;
            }
            case 4: {
                // V/OCT quantized sweep 1 V -> 4 V over full duration.
                // Quantize to semitone steps over 3 octaves (36 steps).
                constexpr int kNumSemitoneSteps = 36;
                int step = (int)std::floor(t * (float)kNumSemitoneSteps);
                if (step >= kNumSemitoneSteps)
                    step = kNumSemitoneSteps - 1;
                // When the quantized step changes in pitch‑locked mode, start a
                // very short fade-in envelope on the wet signal so the spectral
                // change is slightly softened instead of fully abrupt. We no
                // longer hard-reset all grains here; the combination of this
                // envelope and fundamental smoothing below keeps clicks low.
                if (pitch_lock_on && step != last_voct_step && last_voct_step >= 0)
                {
                    voct_change_env = 0.0f;
                    voct_change_env_samples = kVoctChangeFadeSamples;
                }
                last_voct_step = step;
                float voct_volts = 1.0f + (float)step / 12.0f;  // 12 semitones per V
                fundamental_hz = VoctVoltsToFundamentalHz(voct_volts);
                break;
            }
            case 5: smoothing = 0.10f + t * 0.85f; break;  // CV6 smoothing: 0.10 -> 0.95 (clear transients -> glassy pads)
            case 6: sparsity = t; break;          // 0 -> 1 (full spectrum -> very sparse, formant-like clusters)
            case 7:
                phase_diffusion = t;             // 0..1 sweep (coherent -> noisy / diffused)
                break;
            default: break;
        }

        resynth.SetSmoothing(smoothing);
        resynth.SetSpectralFlatten(flatten);
        resynth.SetBrightDark(tilt);
        resynth.SetSparsity(sparsity);
        resynth.SetPhaseDiffusion(phase_diffusion);

        // Smooth fundamental in Hz so discrete V/OCT steps behave more like a
        // fast glide. This mirrors the firmware approach (short time constant)
        // and significantly reduces clicks at note changes.
        float target_fundamental_hz = fundamental_hz;
        if (target_fundamental_hz <= 0.0f)
        {
            fundamental_hz_smooth = 0.0f;
        }
        else if (fundamental_hz_smooth <= 0.0f)
        {
            fundamental_hz_smooth = target_fundamental_hz;
        }
        else
        {
            // Time constant of a few milliseconds: quick enough for envelopes
            // and gliss, long enough to soften hard diatonic steps.
            const float tau   = 0.004f; // ~4 ms
            float dt          = 1.0f / (float)kSampleRate;
            float alpha       = 1.0f - std::exp(-dt / tau);
            if (alpha > 1.0f)
                alpha = 1.0f;
            fundamental_hz_smooth += alpha * (target_fundamental_hz - fundamental_hz_smooth);
        }
        resynth.SetFundamentalHz(fundamental_hz_smooth, kSampleRate);

        float mono_in = mono[i];
        input_history[history_write_pos] = mono_in;
        history_write_pos = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if (total_samples_seen >= kFftSize) {
            grain_phase += time_scale;
            while (grain_phase >= (float)kHopSize) {
                startNextGrain();
                float hop        = (float)kHopSize;
                float fluff_now  = resynth.GetFluff();
                float jitter_amt = 0.02f + 0.18f * fluff_now; // ~±2%..±20%
                float lo         = 1.0f - jitter_amt;
                float hi         = 1.0f + jitter_amt;
                float jitterMul  = resynth_engine::SimpleResynth::RandUniform(lo, hi);
                grain_phase     -= hop * jitterMul;
            }
        }

        float wet = 0.0f;
        size_t active_count = 0;
        for (size_t g = 0; g < kNumGrains; ++g) {
            if (grains[g].running) {
                wet += grains[g].Process();
                ++active_count;
            }
        }
        if (active_count > 0)
        {
            // Normalise overlap-add level using a smoothed estimate of the
            // number of active grains, and with a sqrt() dependence so that
            // very dense grain clouds do not collapse in level.
            float target = (float)active_count;
            const float alpha_ac = 0.01f; // ~100-sample time constant
            if (active_smooth <= 0.0f)
                active_smooth = target;
            else
                active_smooth += alpha_ac * (target - active_smooth);

            float denom_count = fmaxf(active_smooth, 1.0f);
            float norm = (float)kHopDenom * sqrtf(denom_count);
            wet *= 1.0f / norm;
        }

        // Apply a mild time-scale-dependent makeup gain so that the CV6
        // timestretch sweep (and other extreme time_scales) maintain a
        // comparable perceived loudness while preserving the underlying
        // texture.
        if (cv_index == 5)
        {
            float gain_ts = powf(time_scale, 0.3f); // gentle curve
            if (gain_ts < 0.5f) gain_ts = 0.5f;
            if (gain_ts > 2.0f) gain_ts = 2.0f;
            wet *= gain_ts;
        }

        // Around V/OCT note changes in the pitch-locked cv5 test, apply a short,
        // gentle fade-in on the wet signal only. This further reduces the
        // audibility of discontinuities while leaving the MAX COMP path
        // unchanged (aggressiveness is preserved for *_maxcomp outputs).
        if (pitch_lock_on && is_voct_test && voct_change_env_samples > 0)
        {
            float progress = 1.0f - (float)voct_change_env_samples / (float)kVoctChangeFadeSamples;
            if (progress < 0.0f) progress = 0.0f;
            if (progress > 1.0f) progress = 1.0f;
            voct_change_env = 0.2f + 0.8f * progress; // start slightly attenuated
            --voct_change_env_samples;
        }
        wet *= voct_change_env;

        // When no grains are active yet (first kFftSize samples), pass dry to avoid leading silence
        float out_mono = (active_count > 0)
            ? ((1.0f - drywet) * mono_in + drywet * wet)
            : mono_in;

        // For the dedicated V/OCT sweep in pitch‑locked mode, bypass the
        // reverb so that the note steps are maximally clear.
        float out_with_plateau;
        if (pitch_lock_on && is_voct_test)
        {
            out_with_plateau = out_mono;
        }
        else
        {
            float rev = reverb.process(out_mono);
            out_with_plateau = 0.5f * (out_mono + rev);
        }

        output[i] = out_with_plateau;
    }

    if (max_comp_on)
        apply_max_comp(output.data(), num_frames);

    char path[512];
    snprintf(path, sizeof(path), "%s/%s_%s.wav", out_dir, out_basename, kCvTests[cv_index].name);
    if (!SaveWav(path, output.data(), num_frames, kSampleRate, 1)) {
        fprintf(stderr, "Failed to write %s\n", path);
        return false;
    }
    if (max_comp_on) {
        float rms_dbfs = compute_rms_dbfs(output.data(), num_frames);
        printf("  %s  %.1f dBFS\n", path, (double)rms_dbfs);
        if (rms_dbfs < kMinAvgLevelDbFs) {
            fprintf(stderr, "FAIL: %s avg level %.1f dBFS < %.1f dBFS\n",
                    path, (double)rms_dbfs, (double)kMinAvgLevelDbFs);
            return false;
        }
    } else {
        printf("  %s\n", path);
    }
    return true;
}

int main(int argc, char** argv)
{
    const char* samples_dir = (argc >= 2) ? argv[1] : "samples";
    std::vector<std::string> sample_paths = discover_wav_files(samples_dir);
    if (sample_paths.empty()) {
        fprintf(stderr, "No WAV files in %s. Add 48 kHz WAVs to run tests.\n", samples_dir);
        return 1;
    }

    if (mkdir("out", 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create out/\n");
        return 1;
    }
    if (mkdir(kOutCvSweepPitch, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create %s\n", kOutCvSweepPitch);
        return 1;
    }
    if (mkdir(kOutCvSweepMaxcompPitch, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create %s\n", kOutCvSweepMaxcompPitch);
        return 1;
    }
    if (mkdir(kOutCvSweepPartial, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create %s\n", kOutCvSweepPartial);
        return 1;
    }
    if (mkdir(kOutCvSweepMaxcompPartial, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create %s\n", kOutCvSweepMaxcompPartial);
        return 1;
    }

    printf("CV sweep tests: %zu sample(s) from %s\n", sample_paths.size(), samples_dir);
    printf("  Output dirs (pitch‑locked): %s (no MAX COMP), %s (MAX COMP, avg > %.1f dBFS)\n",
           kOutCvSweepPitch, kOutCvSweepMaxcompPitch, (double)kMinAvgLevelDbFs);
    printf("  Output dirs (partial‑based): %s (no MAX COMP), %s (MAX COMP, avg > %.1f dBFS)\n",
           kOutCvSweepPartial, kOutCvSweepMaxcompPartial, (double)kMinAvgLevelDbFs);
    printf("  All CV sweeps span the full length of each input sample.\n\n");

    for (const std::string& inputPath : sample_paths) {
        std::vector<float> mono;
        if (!load_input(inputPath.c_str(), mono, kSampleRate)) {
            fprintf(stderr, "Skip %s (wrong format or not 48 kHz).\n", inputPath.c_str());
            continue;
        }

        const char* path = inputPath.c_str();
        const char* lastSlash = strrchr(path, '/');
        const char* base = lastSlash ? (lastSlash + 1) : path;
        const char* lastDot = strrchr(base, '.');
        size_t baseLen = lastDot ? (size_t)(lastDot - base) : strlen(base);
        char out_basename[256];
        snprintf(out_basename, sizeof(out_basename), "%.*s", (int)baseLen, base);

        printf("[ %s ] -> %s_*.wav\n", inputPath.c_str(), out_basename);

        // Two passes over engine modes: pitch‑locked first, then partial‑based.
        for (int mode = 0; mode < 2; ++mode) {
            bool pitch_lock_on = (mode == 0);
            const char* mode_label = pitch_lock_on ? "  pitch‑locked mode:\n"
                                                   : "  partial‑based mode:\n";
            printf("%s", mode_label);

            for (int pass = 0; pass < 2; ++pass) {
                bool max_comp_on = (pass == 1);
                const char* out_dir =
                    pitch_lock_on
                        ? (max_comp_on ? kOutCvSweepMaxcompPitch : kOutCvSweepPitch)
                        : (max_comp_on ? kOutCvSweepMaxcompPartial : kOutCvSweepPartial);
                if (max_comp_on) printf("    MAX COMP:\n");
                else             printf("    no MAX COMP:\n");

                for (size_t c = 0; c < kNumCvTests; ++c) {
                    const float* src = mono.data();
                    size_t frames     = mono.size();
                    if (!run_one_cv_test(c, src, frames, out_basename, out_dir, max_comp_on, pitch_lock_on)) {
                        fprintf(stderr, "CV sweep test %zu failed (cv%zu, %s, %s mode)\n",
                                c + 1,
                                c + 1,
                                max_comp_on ? "MAX COMP" : "no MAX COMP",
                                pitch_lock_on ? "pitch‑locked" : "partial‑based");
                        return 1;
                    }
                }
            }
        }
    }
    printf("\nDone. Outputs in %s/, %s/, %s/ and %s/\n",
           kOutCvSweepPitch,
           kOutCvSweepMaxcompPitch,
           kOutCvSweepPartial,
           kOutCvSweepMaxcompPartial);
    return 0;
}
