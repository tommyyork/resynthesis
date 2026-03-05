// Offline test: FLUFF stages with diatonic V/OCT scale.
// For each input WAV in test/samples/, plays a diatonic scale from 2 V to 3 V
// (one octave, major scale) and renders one pass for each FLUFF combination:
// 0, 1, 1+2, 1+2+3, 1+2+3+4.
//
// Output: out/fluff/{basename}_fluff_combo{0..4}.wav
//
// Combo meaning (matches README description):
//   0: FLUFF off (baseline)
//   1: Stage 1 only: extra phase diffusion
//   2: Stages 1–2: + analysis-window jitter
//   3: Stages 1–3: + micro‑pitch jitter (pitch‑locked mode)
//   4: Stages 1–4: + per‑bin magnitude jitter (full granular cloud)

#include "../ResynthEngine.h"
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
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#endif

static constexpr unsigned kSampleRate = 48000;

// One diatonic octave (major scale) from 2 V to 3 V.
// The scale will be spread evenly across the second repetition of the sample.
static const int   kDiatonicOneOctave[] = {0, 2, 4, 5, 7, 9, 11, 12};
static const size_t kNumSteps           = sizeof(kDiatonicOneOctave) / sizeof(kDiatonicOneOctave[0]);

static const char kOutFluffDir[] = "out/fluff";

// Reuse the MAX COMP compressor shape from the CV sweep tests so FLUFF
// results are rendered at a similar loudness to firmware with MAX COMP on.
using namespace resynth_params;

static void apply_max_comp(float* buf, size_t num_frames)
{
    float env = 0.0f;
    const float attack_coeff
        = 1.0f - std::exp(-1.0f / (kCompAttack * (float)kSampleRate));
    const float release_coeff
        = 1.0f - std::exp(-1.0f / (kCompRelease * (float)kSampleRate));
    for(size_t i = 0; i < num_frames; ++i)
    {
        float x = buf[i];
        if(x > kSoftClipLim)
            x = kSoftClipLim
                + (x - kSoftClipLim) / (1.0f + (x - kSoftClipLim));
        if(x < -kSoftClipLim)
            x = -kSoftClipLim
                + (x + kSoftClipLim) / (1.0f - (x + kSoftClipLim));
        float in_peak = std::fabs(x);
        float coeff   = (in_peak > env) ? attack_coeff : release_coeff;
        env += coeff * (in_peak - env);
        float gain = 1.0f;
        if(env > 1e-6f)
        {
            if(env <= kCompThreshMax)
                gain = std::pow(kCompThreshMax / env, 0.5f);
            else
                gain = std::pow(
                           kCompThreshMax / env,
                           1.0f - 1.0f / kCompRatioMax);
            gain *= kCompMakeupMax;
        }
        x *= gain;
        if(x > kSoftClipLim)
            x = kSoftClipLim
                + (x - kSoftClipLim) / (1.0f + (x - kSoftClipLim));
        if(x < -kSoftClipLim)
            x = -kSoftClipLim
                + (x + kSoftClipLim) / (1.0f - (x + kSoftClipLim));
        buf[i] = x;
    }
}

static std::vector<std::string> discover_wav_files(const char* dir)
{
    std::vector<std::string> out;
#ifdef _WIN32
    std::string pattern = std::string(dir) + "\\*.wav";
    struct _finddata_t fd;
    intptr_t h = _findfirst(pattern.c_str(), &fd);
    if (h == -1)
        return out;
    do
    {
        if (!(fd.attrib & _A_SUBDIR) && strstr(fd.name, ".wav"))
            out.push_back(std::string(dir) + "/" + fd.name);
    } while (_findnext(h, &fd) == 0);
    _findclose(h);
#else
    DIR* d = opendir(dir);
    if(!d)
        return out;
    struct dirent* e;
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

static bool process_one_file_with_fluff(const char* inputPath)
{
    std::vector<float> inputSamples;
    WavInfo            info;
    if(!LoadWav(inputPath, inputSamples, info))
    {
        fprintf(stderr, "Skip %s (not a WAV or unreadable).\n", inputPath);
        return false;
    }
    if(info.sampleRate != kSampleRate)
    {
        fprintf(stderr, "Skip %s (expected %u Hz, got %u).\n", inputPath, kSampleRate, info.sampleRate);
        return false;
    }

    size_t             numFramesIn = info.numFrames;
    if(numFramesIn == 0)
    {
        fprintf(stderr, "Skip %s (empty file).\n", inputPath);
        return false;
    }

    // We render two repetitions of the input sample:
    //  - First repetition: FLUFF swept 0→1, no V/OCT movement (fixed fundamental).
    //  - Second repetition: FLUFF swept 0→1 again, with a diatonic 2–3 V scale
    //    spread across the full length of the second repetition.
    const size_t totalFrames = numFramesIn * 2;

    std::vector<float>  mono(numFramesIn);
    if(info.numChannels == 1)
    {
        for(size_t i = 0; i < numFramesIn; ++i)
            mono[i] = inputSamples[i];
    }
    else
    {
        for(size_t i = 0; i < numFramesIn; ++i)
        {
            mono[i] = 0.5f * (inputSamples[i * 2] + inputSamples[i * 2 + 1]);
        }
    }

    const char* path      = inputPath;
    const char* lastSlash = strrchr(path, '/');
    const char* base      = lastSlash ? (lastSlash + 1) : path;
    const char* lastDot   = strrchr(base, '.');
    size_t      baseLen   = lastDot ? (size_t)(lastDot - base) : strlen(base);
    char        out_basename[256];
    snprintf(out_basename, sizeof(out_basename), "%.*s", (int)baseLen, base);

    using namespace resynth_engine;

    SimpleResynth resynth;
    Grain         grains[kNumGrains];
    resynth.Init();
    for(size_t g = 0; g < kNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index   = 0;
    }

    float drywet = 1.0f; // 100% wet: hear resynth only
    // Use assertive defaults for smoothing/flatten/tilt, but keep sparsity and
    // diffusion near minimum so FLUFF is the main driver of texture.
    resynth.SetSmoothing(0.8f);        // heavy magnitude smoothing
    resynth.SetSpectralFlatten(0.65f); // strongly whiten / flatten spectrum
    resynth.SetBrightDark(0.45f);      // noticeably bright tilt
    resynth.SetSparsity(0.05f);        // minimal spectral carving by default
    resynth.SetPhaseDiffusion(0.05f);  // very low phase diffusion; FLUFF adds more
    // Exercise the partial‑based / spectral‑model mode used when B_8 is ON.
    resynth.SetPitchLockMode(false);

    float  input_history[kFftSize];
    size_t history_write_pos  = 0;
    size_t total_samples_seen = 0;
    float  grain_phase        = 0.0f;
    std::vector<float> output(totalFrames);

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

    for(size_t i = 0; i < totalFrames; ++i)
    {
        const bool   inFirstRepetition = (i < numFramesIn);
        const size_t posInSample       = inFirstRepetition ? i : (i - numFramesIn);

        // Sweep FLUFF from 0 → 1 over the length of each repetition.
        float sweep_phase = (float)posInSample / (float)numFramesIn;
        if(sweep_phase < 0.0f)
            sweep_phase = 0.0f;
        if(sweep_phase > 1.0f)
            sweep_phase = 1.0f;
        resynth.SetFluff(sweep_phase);

        // V/OCT behaviour:
        //  - First repetition: fixed fundamental (no V/OCT movement).
        //  - Second repetition: diatonic 2–3 V scale spread across the repetition.
        float voct_volts;
        if(inFirstRepetition)
        {
            voct_volts = 2.0f; // treat as a fixed note when V/OCT is "unpatched"
        }
        else
        {
            float  frac      = (float)posInSample / (float)numFramesIn;
            size_t stepIndex = (size_t)(frac * (float)kNumSteps);
            if(stepIndex >= kNumSteps)
                stepIndex = kNumSteps - 1;
            int semitones = kDiatonicOneOctave[stepIndex];
            voct_volts    = 2.0f + (float)semitones / 12.0f; // 2 V .. 3 V over one octave
        }
        float fundamental = resynth_params::VoctVoltsToFundamentalHz(voct_volts);
        resynth.SetFundamentalHz(fundamental, kSampleRate);

        float mono_in = mono[posInSample];
        input_history[history_write_pos] = mono_in;
        history_write_pos                = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if(total_samples_seen >= kFftSize)
        {
            const float time_scale = 1.0f;
            grain_phase += time_scale;
            while(grain_phase >= (float)kHopSize)
            {
                startNextGrain();
                float hop       = (float)kHopSize;
                // Mild jitter as in the firmware path so grains form a dense,
                // but not wildly varying, cloud.
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
            wet *= 1.0f / ((float)kHopDenom * (float)active_count);

        float out_mono = (active_count > 0)
                             ? ((1.0f - drywet) * mono_in + drywet * wet)
                             : mono_in;

        // Soft clip to reduce peaks
        float lim = 0.95f;
        if(out_mono > lim)
            out_mono = lim + (out_mono - lim) / (1.0f + (out_mono - lim));
        if(out_mono < -lim)
            out_mono = -lim + (out_mono + lim) / (1.0f - (out_mono + lim));

        output[i] = out_mono;
    }

    char outPath[512];
    snprintf(outPath,
             sizeof(outPath),
             "%s/%s_fluff_sweep.wav",
             kOutFluffDir,
             out_basename);

    // Apply offline MAX COMP so FLUFF renders match the firmware path
    // with the MAX COMP switch engaged.
    apply_max_comp(output.data(), totalFrames);

    if(!SaveWav(outPath, output.data(), totalFrames, kSampleRate, 1))
    {
        fprintf(stderr, "Failed to write %s\n", outPath);
        return false;
    }
    printf("  %s\n", outPath);

    return true;
}

int main(int argc, char** argv)
{
    const char* samples_dir = (argc >= 2) ? argv[1] : "samples";
    std::vector<std::string> paths       = discover_wav_files(samples_dir);
    if(paths.empty())
    {
        fprintf(stderr,
                "No WAV files in %s. Add 48 kHz WAVs to run the FLUFF test.\n",
                samples_dir);
        return 1;
    }

    if(mkdir("out", 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create out/\n");
        return 1;
    }
    if(mkdir(kOutFluffDir, 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create %s\n", kOutFluffDir);
        return 1;
    }

    printf("FLUFF test: %zu sample(s) from %s -> %s/{basename}_fluff_combo{0..4}.wav\n",
           paths.size(),
           samples_dir,
           kOutFluffDir);

    int ok = 0;
    for(const std::string& p : paths)
    {
        printf("[ %s ]\n", p.c_str());
        if(process_one_file_with_fluff(p.c_str()))
            ++ok;
    }

    printf("Done. Processed %d/%zu files. Outputs in %s/\n", ok, paths.size(), kOutFluffDir);
    return (ok > 0) ? 0 : 1;
}

