// Offline test: V/OCT (diatonic) sweep over 14 quarter notes. Runs for every WAV in
// the samples folder. Output: out/voct_sweep/{basename}_voct_sweep.wav

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
static constexpr float kBpm = 120.0f;
static constexpr unsigned kQuarterNotes = 14;
static constexpr float kDurationSec = (kQuarterNotes * 60.0f) / kBpm;
static constexpr size_t kNumFrames = (size_t)(kSampleRate * kDurationSec + 0.5f);
static constexpr unsigned kSamplesPerStep = (unsigned)(kSampleRate * 60.0f / kBpm + 0.5f);

static const char kOutVoctSweepDir[] = "out/voct_sweep";
static const char kOutSuffix[] = "_voct_sweep.wav";

// Two diatonic octaves: 14 steps (one per quarter note), 0–24 semitones
static const int kDiatonicTwoOctaves[] = {
    0, 2, 4, 5, 7, 9, 11, 12,
    14, 16, 17, 19, 21, 23
};
static const size_t kNumSteps = sizeof(kDiatonicTwoOctaves) / sizeof(kDiatonicTwoOctaves[0]);

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

static bool process_one_voct_sweep(const char* inputPath)
{
    std::vector<float> inputSamples;
    WavInfo info;
    if (!LoadWav(inputPath, inputSamples, info)) {
        fprintf(stderr, "Skip %s (not a WAV or unreadable).\n", inputPath);
        return false;
    }
    if (info.sampleRate != kSampleRate) {
        fprintf(stderr, "Skip %s (expected %u Hz, got %u).\n", inputPath, kSampleRate, info.sampleRate);
        return false;
    }

    size_t numFramesIn = info.numFrames;
    std::vector<float> mono(kNumFrames);
    if (info.numChannels == 1)
    {
        for (size_t i = 0; i < kNumFrames; ++i)
            mono[i] = i < numFramesIn ? inputSamples[i] : 0.0f;
    }
    else
    {
        for (size_t i = 0; i < kNumFrames; ++i)
        {
            if (i < numFramesIn)
                mono[i] = 0.5f * (inputSamples[i * 2] + inputSamples[i * 2 + 1]);
            else
                mono[i] = 0.0f;
        }
    }

    using namespace resynth_engine;
    using namespace resynth_params;
    SimpleResynth resynth;
    Grain grains[kNumGrains];
    resynth.Init();
    for (size_t g = 0; g < kNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index = 0;
    }

    float drywet = 1.0f;  // 100% wet so output is resynthesized only (no dry mix)
    // V/OCT calibration-style preset: pitch‑locked grains, light shaping so that
    // the perceived pitch closely follows the diatonic V/OCT steps.
    float smoothing        = 0.20f;  // faster magnitude tracking for clear note steps
    float flatten          = 0.10f;  // mostly original spectral shape
    float tilt             = 0.10f;  // gently bright
    float sparsity         = 0.10f;  // keep spectrum relatively dense
    float phase_diffusion  = 0.10f;  // modest phase animation only
    float fluff            = 0.20f;  // light clouding without smearing pitch

    resynth.SetSmoothing(smoothing);
    resynth.SetSpectralFlatten(flatten);
    resynth.SetBrightDark(tilt);
    resynth.SetSparsity(sparsity);
    resynth.SetPhaseDiffusion(phase_diffusion);
    resynth.SetFluff(fluff);
    // Pitch‑locked grains for a scale‑like V/OCT response in this offline test.
    resynth.SetPureResynthMode(false);
    resynth.SetPitchLockMode(true);

    const float time_scale = 1.5f;  // slightly denser grains for quicker V/OCT response

    float input_history[kFftSize];
    size_t history_write_pos  = 0;
    size_t total_samples_seen = 0;
    float grain_phase         = 0.0f;
    std::vector<float> output(kNumFrames);
    // Smoothed wet gain to reduce pumping when the number of overlapping
    // grains changes, mirroring the realtime firmware behaviour.
    float wet_gain_state      = 1.0f;

    // Smoothed fundamental Hz for the diatonic V/OCT sweep so that
    // steps behave like a fast glide instead of a hard jump. This
    // reduces clicks at note boundaries.
    float fundamental_hz_smooth = 0.0f;
    // Short crossfade envelope applied when the diatonic step changes
    // so the transition is less abrupt, complementing the smoothed
    // fundamental without touching the MAX COMP parameters used
    // elsewhere in the test suite.
    float step_change_env = 1.0f;
    int   step_change_env_samples = 0;
    const int kStepChangeFadeSamples = 256; // ~5.3 ms at 48 kHz

    size_t stepIndex = 0;
    unsigned samplesInCurrentStep = 0;

    auto startNextGrain = [&]()
    {
        size_t idx = 0;
        for (size_t g = 0; g < kNumGrains; ++g)
        {
            if (!grains[g].running) { idx = g; break; }
        }
        resynth.StartGrainFromHistory(input_history, history_write_pos, grains[idx]);
    };

    for (size_t i = 0; i < kNumFrames; ++i)
    {
        if (samplesInCurrentStep >= kSamplesPerStep)
        {
            samplesInCurrentStep = 0;
            stepIndex = (stepIndex < kNumSteps - 1) ? (stepIndex + 1) : stepIndex;
            int semitones = kDiatonicTwoOctaves[stepIndex];
            float target_fundamental_hz = SemitonesToFundamentalHz(semitones);

            // Smooth the fundamental across diatonic steps instead of hard
            // jumping. This keeps the perceived scale intact but avoids the
            // sharp discontinuities that produced clicks at each note.
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
                const float tau   = 0.004f; // ~4 ms, similar to firmware
                float dt          = 1.0f / (float)kSampleRate;
                float alpha       = 1.0f - std::exp(-dt / tau);
                if (alpha > 1.0f)
                    alpha = 1.0f;
                fundamental_hz_smooth += alpha * (target_fundamental_hz - fundamental_hz_smooth);
            }
            resynth.SetFundamentalHz(fundamental_hz_smooth, kSampleRate);

            // Start a short fade-in envelope when the diatonic step advances so
            // that the new note ramps up smoothly instead of appearing as a
            // full-amplitude discontinuity. We no longer hard-reset grains here.
            step_change_env = 0.0f;
            step_change_env_samples = kStepChangeFadeSamples;
        }
        ++samplesInCurrentStep;

        float mono_in = mono[i];
        input_history[history_write_pos] = mono_in;
        history_write_pos = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if (total_samples_seen >= kFftSize)
        {
            grain_phase += time_scale;
            while (grain_phase >= (float)kHopSize)
            {
                startNextGrain();
                float hop        = (float)kHopSize;
                float fluff_now  = resynth.fluff;
                float jitter_amt = 0.02f + 0.18f * fluff_now; // ~±2%..±20%
                float lo         = 1.0f - jitter_amt;
                float hi         = 1.0f + jitter_amt;
                // Mild jitter around the nominal hop so we avoid a rigid
                // launch grid but tie the depth to FLUFF, mirroring the
                // firmware behaviour.
                float jitterMul
                    = resynth_engine::SimpleResynth::RandUniform(lo, hi);
                grain_phase -= hop * jitterMul;
            }
        }

        float wet = 0.0f;
        size_t active_count = 0;
        for (size_t g = 0; g < kNumGrains; ++g)
        {
            if (grains[g].running)
            {
                wet += grains[g].Process();
                ++active_count;
            }
        }
        if (active_count > 0)
        {
            float target_gain = 1.0f
                                / ((float)kHopDenom
                                   * (float)active_count);
            const float alpha = 0.01f; // ~100-frame smoothing
            wet_gain_state += alpha * (target_gain - wet_gain_state);
        }
        wet *= wet_gain_state;

        // Apply a short, gentle fade around diatonic step changes so the
        // transition between notes remains scale-like but less clicky.
        if (step_change_env_samples > 0)
        {
            float progress = 1.0f - (float)step_change_env_samples / (float)kStepChangeFadeSamples;
            if (progress < 0.0f) progress = 0.0f;
            if (progress > 1.0f) progress = 1.0f;
            step_change_env = 0.2f + 0.8f * progress; // brief attenuation
            --step_change_env_samples;
        }
        wet *= step_change_env;

        // When no grains are active yet (first kFftSize samples), pass dry to avoid leading silence
        float out_mono = (active_count > 0)
            ? ((1.0f - drywet) * mono_in + drywet * wet)
            : mono_in;
        // Soft clip to reduce peaks and level variation
        float lim = 0.95f;
        if (out_mono > lim)  out_mono = lim + (out_mono - lim) / (1.0f + (out_mono - lim));
        if (out_mono < -lim) out_mono = -lim + (out_mono + lim) / (1.0f - (out_mono + lim));
        output[i] = out_mono;
    }

    const char* suffix = kOutSuffix;
    const char* lastSlash = strrchr(inputPath, '/');
    const char* base = lastSlash ? (lastSlash + 1) : inputPath;
    const char* lastDot = strrchr(base, '.');
    size_t baseLen = lastDot ? (size_t)(lastDot - base) : strlen(base);
    size_t outPathLen = strlen(kOutVoctSweepDir) + 1 + baseLen + strlen(suffix) + 1;
    std::vector<char> outPath(outPathLen);
    snprintf(outPath.data(), outPathLen, "%s/%.*s%s", kOutVoctSweepDir, (int)baseLen, base, suffix);

    if (mkdir(kOutVoctSweepDir, 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create output directory %s\n", kOutVoctSweepDir);
        return 1;
    }

    if (!SaveWav(outPath.data(), output.data(), kNumFrames, kSampleRate, 1))
    {
        fprintf(stderr, "Failed to write %s\n", outPath.data());
        return 1;
    }

    FILE* verify = fopen(outPath.data(), "rb");
    if (!verify)
    {
        fprintf(stderr, "Failed to verify output file %s\n", outPath.data());
        return 1;
    }
    fseek(verify, 0, SEEK_END);
    long size = ftell(verify);
    fclose(verify);
    if (size <= 0)
    {
        fprintf(stderr, "Output file is empty or zero size: %s\n", outPath.data());
        return 1;
    }

    printf("Wrote %s (%ld bytes)\n", outPath.data(), (long)size);
    return true;
}

int main(int argc, char** argv)
{
    const char* samples_dir = (argc >= 2) ? argv[1] : "samples";
    std::vector<std::string> paths = discover_wav_files(samples_dir);
    if (paths.empty()) {
        fprintf(stderr, "No WAV files in %s. Add 48 kHz WAVs to run the V/OCT sweep test.\n", samples_dir);
        return 1;
    }
    if (mkdir("out", 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create out/\n");
        return 1;
    }
    if (mkdir(kOutVoctSweepDir, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "Failed to create %s\n", kOutVoctSweepDir);
        return 1;
    }
    printf("V/OCT sweep test: %zu sample(s) from %s -> %s/{basename}_voct_sweep.wav\n",
           paths.size(), samples_dir, kOutVoctSweepDir);
    int ok = 0;
    for (const std::string& p : paths) {
        if (process_one_voct_sweep(p.c_str()))
            ++ok;
    }
    printf("Done. Processed %d/%zu files. Outputs in %s/\n", ok, paths.size(), kOutVoctSweepDir);
    return (ok > 0) ? 0 : 1;
}
