// Offline test for the simplified harmonic-stack resynthesis engine.
//
// For each 48 kHz WAV in test/samples/, this test renders:
//   - A single held note at 2 V (C2) using the harmonic engine.
//   - A 14-step diatonic V/OCT sweep using the same scale as the
//     existing phase-vocoder offline test.
//
// Outputs are written to:
//   out/harmonic/{basename}_harmonic_C2.wav
//   out/harmonic/{basename}_harmonic_voct_sweep.wav
//
// This lets you A/B the minimal, musical core algorithm against the
// full Resynthesis engine.

#include "../ResynthEngineHarmonic.h"
#include "../ResynthParams.h"
#include "wav_io.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#include <direct.h>
#define mkdir(path, mode) _mkdir(path)
#include <io.h>
#else
#include <dirent.h>
#include <sys/stat.h>
#include <sys/types.h>
#endif

static constexpr unsigned kSampleRate   = 48000;
static constexpr float    kBpm          = 120.0f;
static constexpr unsigned kQuarterNotes = 14;
static constexpr float    kDurationSec  = (kQuarterNotes * 60.0f) / kBpm;
static constexpr size_t   kNumFrames    = (size_t)(kSampleRate * kDurationSec + 0.5f);
static constexpr unsigned kSamplesPerStep
    = (unsigned)(kSampleRate * 60.0f / kBpm + 0.5f);

static constexpr float kHeldNoteDurationSec = 8.0f;
static constexpr size_t kHeldNoteFrames
    = (size_t)(kSampleRate * kHeldNoteDurationSec + 0.5f);

static const char kOutHarmonicDir[] = "out/harmonic";

// For the harmonic tests we now use a moderately dense grain cloud
// compared to the real-time engine: 16 grains (realtime) -> 40 grains.
static constexpr size_t kHarmonicNumGrains = 40;

// Two diatonic octaves: 14 steps (one per quarter note), 0–24 semitones.
static const int kDiatonicTwoOctaves[] = {
    0, 2, 4, 5, 7, 9, 11, 12,
    14, 16, 17, 19, 21, 23,
};
static const size_t kNumSteps
    = sizeof(kDiatonicTwoOctaves) / sizeof(kDiatonicTwoOctaves[0]);

static std::vector<std::string> discover_wav_files(const char *dir)
{
    std::vector<std::string> out;
#ifdef _WIN32
    std::string   pattern = std::string(dir) + "\\*.wav";
    struct _finddata_t fd;
    intptr_t           h = _findfirst(pattern.c_str(), &fd);
    if(h == -1)
        return out;
    do
    {
        if(!(fd.attrib & _A_SUBDIR) && strstr(fd.name, ".wav"))
            out.push_back(std::string(dir) + "/" + fd.name);
    } while(_findnext(h, &fd) == 0);
    _findclose(h);
#else
    DIR *d = opendir(dir);
    if(!d)
        return out;
    struct dirent *e;
    while((e = readdir(d)) != nullptr)
    {
        size_t len = strlen(e->d_name);
        if(len > 4 && strcmp(e->d_name + len - 4, ".wav") == 0)
            out.push_back(std::string(dir) + "/" + e->d_name);
    }
    closedir(d);
#endif
    std::sort(out.begin(), out.end());
    return out;
}

static bool load_mono_padded(const char          *path,
                             std::vector<float>  &mono,
                             size_t               num_frames,
                             unsigned             sample_rate)
{
    std::vector<float> inputSamples;
    WavInfo            info;
    if(!LoadWav(path, inputSamples, info))
        return false;
    if(info.sampleRate != sample_rate)
        return false;

    size_t n = info.numFrames;
    mono.resize(num_frames);
    if(info.numChannels == 1)
    {
        for(size_t i = 0; i < num_frames; ++i)
            mono[i] = (i < n) ? inputSamples[i] : 0.0f;
    }
    else
    {
        for(size_t i = 0; i < num_frames; ++i)
        {
            if(i < n)
                mono[i] = 0.5f
                          * (inputSamples[i * 2] + inputSamples[i * 2 + 1]);
            else
                mono[i] = 0.0f;
        }
    }
    return true;
}

static void render_with_engine(const std::vector<float> &mono_in,
                               size_t                    num_frames,
                               float                     fundamental_hz,
                               bool                      do_voct_sweep,
                               std::vector<float>       &out)
{
    using namespace resynth_engine;
    using namespace resynth_params;

    SimpleHarmonicResynth resynth;
    Grain                 grains[kHarmonicNumGrains];

    resynth.Init();
    resynth.SetFundamentalHz(fundamental_hz, kSampleRate);
    // Use a very rich stack: up to 128 harmonics (clamped internally).
    resynth.SetMaxHarmonics(128);

    for(size_t g = 0; g < kHarmonicNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index   = 0;
    }

    float  input_history[kFftSize];
    size_t history_write_pos  = 0;
    size_t total_samples_seen = 0;
    float  grain_phase        = 0.0f;
    float  time_scale         = 1.0f;

    out.assign(num_frames, 0.0f);

    size_t   stepIndex            = 0;
    unsigned samplesInCurrentStep = 0;

    auto startNextGrain = [&]() {
        size_t idx = 0;
        for(size_t g = 0; g < kHarmonicNumGrains; ++g)
        {
            if(!grains[g].running)
            {
                idx = g;
                break;
            }
        }
        resynth.StartGrainFromHistory(input_history, history_write_pos, grains[idx]);
    };

    for(size_t i = 0; i < num_frames; ++i)
    {
        if(do_voct_sweep)
        {
            if(samplesInCurrentStep >= kSamplesPerStep)
            {
                samplesInCurrentStep = 0;
                stepIndex
                    = (stepIndex < kNumSteps - 1) ? (stepIndex + 1) : stepIndex;
                int   semitones = kDiatonicTwoOctaves[stepIndex];
                float f = SemitonesToFundamentalHz(semitones);
                resynth.SetFundamentalHz(f, kSampleRate);
            }
            ++samplesInCurrentStep;
        }

        float mono = mono_in[i];
        input_history[history_write_pos] = mono;
        history_write_pos                = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if(total_samples_seen >= kFftSize)
        {
            grain_phase += time_scale;
            while(grain_phase >= static_cast<float>(kHopSize))
            {
                startNextGrain();
                float hop       = static_cast<float>(kHopSize);
                // Strongly jitter grain spacing so the large grain pool
                // turns into a dense, random cloud rather than a
                // regular comb of overlapping windows.
                float jitterMul
                    = SimpleResynth::RandUniform(0.1f, 2.0f);
                grain_phase -= hop * jitterMul;
            }
        }

        float  wet          = 0.0f;
        size_t active_count = 0;
        for(size_t g = 0; g < kHarmonicNumGrains; ++g)
        {
            if(grains[g].running)
            {
                wet += grains[g].Process();
                ++active_count;
            }
        }
        if(active_count > 0)
            wet *= 1.0f
                   / (static_cast<float>(kHopDenom)
                      * static_cast<float>(active_count));

        // No dry mix here: this test is meant to isolate the simplified
        // algorithm. Keep level in check with a gentle soft clip.
        float out_mono = (active_count > 0) ? wet : mono;
        float lim      = 0.95f;
        if(out_mono > lim)
            out_mono = lim
                       + (out_mono - lim) / (1.0f + (out_mono - lim));
        if(out_mono < -lim)
            out_mono = -lim
                       + (out_mono + lim) / (1.0f - (out_mono + lim));

        out[i] = out_mono;
    }
}

static bool process_one_file(const char *inputPath)
{
    // Held note at 2 V (C2).
    std::vector<float> monoHeld;
    if(!load_mono_padded(inputPath, monoHeld, kHeldNoteFrames, kSampleRate))
    {
        fprintf(stderr, "Skip %s (wrong format or not 48 kHz).\n", inputPath);
        return false;
    }

    std::vector<float> monoSweep;
    if(!load_mono_padded(inputPath, monoSweep, kNumFrames, kSampleRate))
    {
        fprintf(stderr, "Skip %s (wrong format or not 48 kHz).\n", inputPath);
        return false;
    }

    const char *path      = inputPath;
    const char *lastSlash = strrchr(path, '/');
    const char *base      = lastSlash ? (lastSlash + 1) : path;
    const char *lastDot   = strrchr(base, '.');
    size_t      baseLen   = lastDot ? (size_t)(lastDot - base) : strlen(base);

    char basename[256];
    snprintf(basename, sizeof(basename), "%.*s", (int)baseLen, base);

    // Held C2: 2 V on a 1 V/oct scale.
    float fundamental_c2 = resynth_params::VoctVoltsToFundamentalHz(2.0f); // C2 ≈ 65.4 Hz

    std::vector<float> outHeld;
    render_with_engine(monoHeld, kHeldNoteFrames, fundamental_c2, false, outHeld);

    std::vector<float> outSweep;
    // Initial fundamental for the sweep is 0 semitones offset.
    float initial_f0 = resynth_params::SemitonesToFundamentalHz(0); // base C0-like reference
    render_with_engine(monoSweep, kNumFrames, initial_f0, true, outSweep);

    char pathHeld[512];
    char pathSweep[512];
    snprintf(pathHeld,
             sizeof(pathHeld),
             "%s/%s_harmonic_C2.wav",
             kOutHarmonicDir,
             basename);
    snprintf(pathSweep,
             sizeof(pathSweep),
             "%s/%s_harmonic_voct_sweep.wav",
             kOutHarmonicDir,
             basename);

    if(!SaveWav(pathHeld, outHeld.data(), outHeld.size(), kSampleRate, 1))
    {
        fprintf(stderr, "Failed to write %s\n", pathHeld);
        return false;
    }
    if(!SaveWav(pathSweep, outSweep.data(), outSweep.size(), kSampleRate, 1))
    {
        fprintf(stderr, "Failed to write %s\n", pathSweep);
        return false;
    }

    printf("  %s\n", pathHeld);
    printf("  %s\n", pathSweep);
    return true;
}

int main(int argc, char **argv)
{
    const char *samples_dir = (argc >= 2) ? argv[1] : "samples";
    std::vector<std::string> paths = discover_wav_files(samples_dir);
    if(paths.empty())
    {
        fprintf(stderr,
                "No WAV files in %s. Add 48 kHz WAVs (e.g. chromaplane, dryseq) "
                "to run the harmonic tests.\n",
                samples_dir);
        return 1;
    }

    if(mkdir("out", 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create out/\n");
        return 1;
    }
    if(mkdir(kOutHarmonicDir, 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create %s\n", kOutHarmonicDir);
        return 1;
    }

    printf("Harmonic resynthesis tests: %zu sample(s) from %s\n",
           paths.size(),
           samples_dir);
    printf("  Output dir: %s\n\n", kOutHarmonicDir);

    int ok = 0;
    for(const std::string &p : paths)
    {
        printf("[ %s ] -> *_harmonic_*.wav\n", p.c_str());
        if(process_one_file(p.c_str()))
            ++ok;
    }

    printf("\nDone. Processed %d/%zu files. Outputs in %s/\n",
           ok,
           paths.size(),
           kOutHarmonicDir);
    return (ok > 0) ? 0 : 1;
}

