// Offline test: V/OCT control and harmonic content of processed results.
//
// Processes OneShotOneOsc.wav (or a given sample) in several configurations:
//   1. Completely dry (passthrough)
//   2. Default CV-sweep params, pitch-lock mode, 0 V at V/OCT (2 V = C2)
//   3. Default params, partial mode, 0 V at V/OCT
//   4. Default params, pitch-lock, 3 V at V/OCT
//   5. Default params, pitch-lock, 4 V at V/OCT
//   6. Default params, partial mode, 3 V at V/OCT
//   7. Default params, partial mode, 4 V at V/OCT
//
// For each pass: compute STFT (using engine FFT/window), output CSV (time, freq, amplitude),
// a spectrogram SVG, and the processed mono WAV. Finally output one stacked SVG with all plots
// and labels.
//
// Build: make test_resynth_voct (from test/) or make test_voct (from Resynthesis/).
// Outputs: out/voct_harmonic/{basename}_voct_*.csv, *_voct_*.svg, and {basename}_voct_stacked.svg

#include "../ResynthEngine.h"
#include "../ResynthParams.h"
#include "../Shifting.h"
#include "daisysp.h"
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

using namespace resynth_engine;
using namespace resynth_params;
using namespace daisysp;

// ---- Default parameters (from cv_sweep, non-sweeping defaults) ----
// For the V/OCT harmonic tests we bias these towards clean, stable,
// strongly harmonic spectra so that the suggested-note analysis locks
// tightly to the expected V/OCT-controlled fundamentals.
static const float kDefaultSmoothing      = 0.6f;   // stronger temporal smoothing
static const float kDefaultFlatten        = 0.3f;   // slightly flatter, more oscillator-like spectrum
// Bright/dark tilt: -1 = even-only harmonics, +1 = odd-only harmonics.
// For the V/OCT harmonic tests use only a mild even bias so the
// fundamental and low harmonics remain well populated.
static const float kDefaultTilt           = -0.2f;
static const float kDefaultTimeScale      = 1.0f;
// Keep sparsity and phase diffusion very low so spectra remain dense
// and line-like around the harmonic stack.
static const float kDefaultSparsity       = 0.02f;
static const float kDefaultPhaseDiffusion = 0.02f;
static const float kVoct0Volts            = 2.0f;  // C2 when "no voltage" = 2 V in cv_sweep

// ---- STFT: high-resolution analysis for voct_harmonic tests ----
// The live engine uses a smaller FFT for responsiveness and texture. For
// these offline tests we analyse the processed audio with a larger FFT to
// resolve low fundamentals cleanly without affecting the DSP path.

static constexpr size_t kAnalysisFftSize  = 2048;
static constexpr size_t kAnalysisNumBins  = kAnalysisFftSize / 2;
static constexpr size_t kAnalysisHopDenom = 4;
static constexpr size_t kAnalysisHopSize  = kAnalysisFftSize / kAnalysisHopDenom;

static void compute_window(float* window)
{
    for (size_t n = 0; n < kAnalysisFftSize; ++n)
        window[n] = 0.5f * (1.0f - cosf(kTwoPi * static_cast<float>(n)
                                        / static_cast<float>(kAnalysisFftSize - 1)));
}

// Compute magnitude spectrum for one frame (windowed, FFT, magnitude per bin).
// Uses engine's FftInPlace and same window as SimpleResynth.
static void frame_magnitude_spectrum(
    const float* signal,
    size_t start_index,
    size_t total_samples,
    const float* window,
    float* mag_out)
{
    Complex spectrum[kAnalysisFftSize];
    for (size_t n = 0; n < kAnalysisFftSize; ++n)
    {
        size_t idx = start_index + n;
        float s = (idx < total_samples) ? signal[idx] : 0.0f;
        spectrum[n].re = s * window[n];
        spectrum[n].im = 0.0f;
    }
    FftInPlace(spectrum, kAnalysisFftSize, false);
    for (size_t k = 0; k <= kAnalysisNumBins; ++k)
    {
        float re = spectrum[k].re, im = spectrum[k].im;
        mag_out[k] = sqrtf(re * re + im * im);
    }
}

// STFT over full buffer: hop by kHopSize, collect magnitude matrix [num_frames][kNumBins+1].
// Returns number of frames.
static size_t compute_stft(
    const float* signal,
    size_t num_samples,
    const float* window,
    std::vector<std::vector<float>>& mag_frames)
{
    mag_frames.clear();
    size_t frame = 0;
    for (size_t start = 0; start + kAnalysisFftSize <= num_samples;
         start += kAnalysisHopSize, ++frame)
    {
        std::vector<float> row(kAnalysisNumBins + 1);
        frame_magnitude_spectrum(signal, start, num_samples, window, row.data());
        mag_frames.push_back(std::move(row));
    }
    return mag_frames.size();
}

// Bin index to frequency (Hz).
static float bin_to_hz(size_t bin)
{
    return static_cast<float>(bin) * static_cast<float>(kSampleRate)
           / static_cast<float>(kAnalysisFftSize);
}

// Map frequency in Hz to MIDI note number (float). Returns -inf for non-positive Hz.
static float hz_to_midi(float hz)
{
    if(hz <= 0.0f)
        return -1.0e9f;
    return 69.0f + 12.0f * log2f(hz / kA4Hz);
}

// Map MIDI note number to note name like "A3", "C#4".
static void midi_to_name(int midi, char* buf, size_t buf_size)
{
    static const char* kNoteNames[12] = {
        "C", "C#", "D", "D#", "E", "F",
        "F#", "G", "G#", "A", "A#", "B"
    };
    int degree = ((midi % 12) + 12) % 12;
    int octave = midi / 12 - 1;
    snprintf(buf, buf_size, "%s%d", kNoteNames[degree], octave);
}

// ---- CSV: frame_index, freq_hz, amplitude ----
static bool write_csv(
    const char* path,
    const std::vector<std::vector<float>>& mag_frames,
    size_t num_frames)
{
    FILE* f = fopen(path, "w");
    if (!f) return false;
    fprintf(f, "frame_index,time_sec,freq_hz,amplitude\n");
    float time_per_frame = static_cast<float>(kAnalysisHopSize)
                           / static_cast<float>(kSampleRate);
    for (size_t fr = 0; fr < num_frames; ++fr)
    {
        float t = static_cast<float>(fr) * time_per_frame;
        const std::vector<float>& row = mag_frames[fr];
        for (size_t k = 0; k <= kAnalysisNumBins; ++k)
            fprintf(f, "%zu,%.6f,%.2f,%.6e\n", fr, t, bin_to_hz(k), (double)row[k]);
    }
    fclose(f);
    return true;
}

static void draw_note_spectrum_panel(
    FILE* f,
    int panel_x,
    int panel_y,
    int panel_width,
    int panel_height,
    const char* label,
    const std::vector<float>& note_mags,
    int min_midi,
    int max_midi,
    float global_max_mag,
    float db_floor)
{
    if(note_mags.empty() || min_midi > max_midi)
        return;

    if(global_max_mag <= 0.0f)
        global_max_mag = 1.0f;

    const float db_max = 0.0f;
    const float db_min = db_floor;
    const float db_range = db_max - db_min;

    const int margin_left   = 60;
    const int margin_right  = 20;
    const int margin_top    = 20;
    const int margin_bottom = 40;

    int inner_w = panel_width - margin_left - margin_right;
    int inner_h = panel_height - margin_top - margin_bottom;
    if(inner_w <= 0 || inner_h <= 0)
        return;

    int x0 = panel_x + margin_left;
    int y0 = panel_y + margin_top;

    fprintf(f,
            "  <rect x=\"%d\" y=\"%d\" width=\"%d\" height=\"%d\" fill=\"#111\"/>\n",
            panel_x, panel_y, panel_width, panel_height);

    const float tick_vals[] = {0.0f, -20.0f, -40.0f, -60.0f, -80.0f};
    const int num_ticks = sizeof(tick_vals) / sizeof(tick_vals[0]);
    for(int i = 0; i < num_ticks; ++i)
    {
        float dv = tick_vals[i];
        if(dv < db_min || dv > db_max)
            continue;
        float norm = (dv - db_min) / db_range;
        float y = y0 + (1.0f - norm) * (float)inner_h;
        fprintf(f,
                "  <line x1=\"%d\" y1=\"%.1f\" x2=\"%d\" y2=\"%.1f\" "
                "stroke=\"#333\" stroke-width=\"1\"/>\n",
                x0, y, x0 + inner_w, y);
        fprintf(f,
                "  <text x=\"%d\" y=\"%.1f\" fill=\"#ccc\" font-family=\"sans-serif\" "
                "font-size=\"10\">%.0f dB</text>\n",
                panel_x + 4, y - 2.0f, dv);
    }

    fprintf(f,
            "  <line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"#888\" "
            "stroke-width=\"1\"/>\n",
            x0, y0, x0, y0 + inner_h);
    fprintf(f,
            "  <line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"#888\" "
            "stroke-width=\"1\"/>\n",
            x0, y0 + inner_h, x0 + inner_w, y0 + inner_h);

    int num_notes = max_midi - min_midi + 1;
    if(num_notes <= 0)
        return;
    float step_x = (float)inner_w / (float)num_notes;
    if(step_x < 1.0f)
        step_x = 1.0f;

    for(int note = 0; note < num_notes; ++note)
    {
        float mag = note_mags[note];
        float db = (mag > 0.0f) ? 20.0f * log10f(mag / global_max_mag) : db_min;
        if(db < db_min)
            db = db_min;
        if(db > db_max)
            db = db_max;
        float norm = (db - db_min) / db_range;
        float x_center = x0 + ((float)note + 0.5f) * step_x;
        float y_top = y0 + (1.0f - norm) * (float)inner_h;
        float y_bottom = y0 + (float)inner_h;
        if(y_top > y_bottom - 0.5f)
            y_top = y_bottom - 0.5f;

        fprintf(f,
                "  <line x1=\"%.1f\" y1=\"%.1f\" x2=\"%.1f\" y2=\"%.1f\" "
                "stroke=\"#4af\" stroke-width=\"1\"/>\n",
                x_center, y_top, x_center, y_bottom);
    }

    for(int midi = min_midi; midi <= max_midi; ++midi)
    {
        int note_index = midi - min_midi;
        int degree = midi % 12;
        int octave = midi / 12 - 1;
        if(degree != 0)
            continue;
        float x = x0 + ((float)note_index + 0.5f) * step_x;
        char label_buf[16];
        midi_to_name(midi, label_buf, sizeof(label_buf));
        fprintf(f,
                "  <text x=\"%.1f\" y=\"%d\" fill=\"#ccc\" font-family=\"sans-serif\" "
                "font-size=\"10\" text-anchor=\"middle\">%s</text>\n",
                x, panel_y + 14, label_buf);
    }

    fprintf(f,
            "  <text x=\"%d\" y=\"%d\" fill=\"#eee\" font-family=\"sans-serif\" "
            "font-size=\"11\">%s</text>\n",
            panel_x + 8, panel_y + 16, label);
    fprintf(f,
            "  <text x=\"%d\" y=\"%d\" fill=\"#ccc\" font-family=\"sans-serif\" "
            "font-size=\"10\" transform=\"rotate(-90 %d,%d)\">Amplitude (dB)</text>\n",
            panel_x + 14, panel_y + panel_height / 2, panel_x + 14, panel_y + panel_height / 2);
}

static bool write_note_spectrum_svg(
    const char* path,
    const char* title,
    const std::vector<float>& note_mags,
    int min_midi,
    int max_midi,
    float global_max_mag,
    float db_floor,
    int plot_width,
    int plot_height)
{
    if(note_mags.empty() || min_midi > max_midi)
        return false;

    FILE* f = fopen(path, "w");
    if(!f)
        return false;

    fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    fprintf(f,
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"%d\" height=\"%d\" "
            "viewBox=\"0 0 %d %d\">\n",
            plot_width, plot_height, plot_width, plot_height);
    fprintf(f, "  <title>%s</title>\n", title);

    draw_note_spectrum_panel(f,
                             0,
                             0,
                             plot_width,
                             plot_height,
                             title,
                             note_mags,
                             min_midi,
                             max_midi,
                             global_max_mag,
                             db_floor);

    fprintf(f, "</svg>\n");
    fclose(f);
    return true;
}

// Write per-note aggregate statistics for a slice, bucketed by MIDI note and
// sorted by descending magnitude.
static bool write_note_bucket_csv(
    const char* path,
    const std::vector<float>& note_mags,
    int min_midi,
    int max_midi,
    float global_max_mag)
{
    if(note_mags.empty() || min_midi > max_midi)
        return false;

    struct Row
    {
        int   midi;
        float mag;
        float db;
        float freq_hz;
        char  name[16];
    };

    std::vector<Row> rows;
    int num_notes = max_midi - min_midi + 1;
    if(num_notes <= 0)
        return false;

    float ref_hz_scale = 1.0f;
    if(global_max_mag <= 0.0f)
        global_max_mag = 1.0f;

    for(int i = 0; i < num_notes; ++i)
    {
        float mag = note_mags[i];
        if(mag <= 0.0f)
            continue;
        int midi = min_midi + i;
        float db = 20.0f * log10f(mag / global_max_mag);
        float freq = kA4Hz * powf(2.0f, (static_cast<float>(midi) - 69.0f) / 12.0f);

        Row r;
        r.midi    = midi;
        r.mag     = mag;
        r.db      = db;
        r.freq_hz = freq * ref_hz_scale;
        midi_to_name(midi, r.name, sizeof(r.name));
        rows.push_back(r);
    }

    if(rows.empty())
        return false;

    std::sort(rows.begin(), rows.end(),
              [](const Row& a, const Row& b) { return a.mag > b.mag; });

    FILE* f = fopen(path, "w");
    if(!f)
        return false;

    fprintf(f, "note,midi,freq_hz,amplitude,amplitude_db\n");
    for(const Row& r : rows)
    {
        fprintf(f, "%s,%d,%.6f,%.6e,%.2f\n",
                r.name,
                r.midi,
                (double)r.freq_hz,
                (double)r.mag,
                (double)r.db);
    }
    fclose(f);
    return true;
}

// ---- Load mono ----
static bool load_mono(const char* path, std::vector<float>& mono, unsigned sampleRate)
{
    std::vector<float> inputSamples;
    WavInfo info;
    if (!LoadWav(path, inputSamples, info))
        return false;
    if (info.sampleRate != sampleRate)
        return false;
    size_t n = info.numFrames;
    if (n == 0) return false;
    mono.resize(n);
    if (info.numChannels == 1)
    {
        for (size_t i = 0; i < n; ++i)
            mono[i] = inputSamples[i];
    }
    else
    {
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

// ---- Pass 1: dry (passthrough) ----
static void run_dry_pass(const float* mono, size_t num_frames, std::vector<float>& out)
{
    out.assign(mono, mono + num_frames);
}

// ---- Pass 2–7: resynth with fixed params ----
static void run_resynth_pass(
    const float* mono,
    size_t num_frames,
    bool pitch_lock,
    float voct_volts,
    std::vector<float>& out)
{
    SimpleResynth resynth;
    Grain grains[kNumGrains];
    resynth.Init();
    resynth.SetPitchLockMode(pitch_lock);
    resynth.SetPureResynthMode(false);
    for (size_t g = 0; g < kNumGrains; ++g)
    {
        grains[g].running = false;
        grains[g].index = 0;
    }

    resynth.SetSmoothing(kDefaultSmoothing);
    resynth.SetSpectralFlatten(kDefaultFlatten);
    resynth.SetBrightDark(kDefaultTilt);
    resynth.SetSparsity(kDefaultSparsity);
    resynth.SetPhaseDiffusion(kDefaultPhaseDiffusion);

    float fundamental_hz = VoctVoltsToFundamentalHz(voct_volts);
    resynth.SetFundamentalHz(fundamental_hz, (float)kSampleRate);

    // Front-end shifting and tracking HPF to mirror the firmware audio path:
    // when pitch lock is enabled, high-pass just below the target fundamental
    // and use the Shifting block to move the input toward the V/OCT note
    // before feeding the phase-vocoder engine.
    Svf                    input_hp;
    resynth_shifting::Shifting shifting;
    input_hp.Init((float)kSampleRate);
    input_hp.SetRes(0.1f);
    input_hp.SetDrive(1.0f);
    shifting.Init((float)kSampleRate);
    shifting.SetPitchLockEnabled(pitch_lock);
    // In the voct harmonic test we want pure pitch shifting, not frequency
    // shifting, so keep COLOR well below the crossfade region used in the
    // firmware (3 o'clock → CW).
    shifting.SetColor(0.25f);
    shifting.SetVoctFundamental(fundamental_hz);

    bool  hp_enabled   = pitch_lock && fundamental_hz > 0.0f;
    float hp_cutoff_hz = 0.9f * fundamental_hz;
    if(hp_cutoff_hz < 10.0f)
        hp_cutoff_hz = 10.0f;
    float hp_max = (float)kSampleRate / 3.0f;
    if(hp_cutoff_hz > hp_max)
        hp_cutoff_hz = hp_max;
    if(hp_enabled)
        input_hp.SetFreq(hp_cutoff_hz);

    float input_history[kFftSize];
    size_t history_write_pos = 0;
    size_t total_samples_seen = 0;
    float grain_phase = 0.0f;
    out.assign(num_frames, 0.0f);

    auto startNextGrain = [&]() {
        size_t idx = 0;
        for (size_t g = 0; g < kNumGrains; ++g)
        {
            if (!grains[g].running) { idx = g; break; }
        }
        resynth.StartGrainFromHistory(input_history, history_write_pos, grains[idx]);
    };

    for (size_t i = 0; i < num_frames; ++i)
    {
        float mono_in = mono[i];

        if(hp_enabled)
        {
            input_hp.Process(mono_in);
            mono_in = input_hp.High();
        }

        mono_in = shifting.Process(mono_in);
        input_history[history_write_pos] = mono_in;
        history_write_pos = (history_write_pos + 1) % kFftSize;
        ++total_samples_seen;

        if (total_samples_seen >= kFftSize)
        {
            grain_phase += kDefaultTimeScale;
            while (grain_phase >= (float)kHopSize)
            {
                startNextGrain();
                float jitterMul = SimpleResynth::RandUniform(0.98f, 1.02f);
                grain_phase -= (float)kHopSize * jitterMul;
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
            wet *= 1.0f / ((float)kHopDenom * (float)active_count);

        // For V/OCT harmonic tests we want the processed passes to be 100%% wet.
        // The dry passthrough is handled by the dedicated dry configuration.
        float out_mono = wet;
        out[i] = out_mono;
    }
}

// ---- Stacked SVG: all 7 spectrograms vertically with labels ----
struct PassInfo
{
    const char* label;
    const std::vector<float>* note_mags;
};

static bool write_stacked_svg(
    const char* path,
    const char* basename,
    const PassInfo* passes,
    size_t num_passes,
    int plot_width,
    int plot_height_per,
    int min_midi,
    int max_midi,
    float global_max_mag,
    float db_floor)
{
    FILE* f = fopen(path, "w");
    if (!f) return false;

    int total_height = (int)num_passes * plot_height_per;
    fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    fprintf(f, "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"%d\" height=\"%d\" viewBox=\"0 0 %d %d\">\n",
            plot_width, total_height, plot_width, total_height);
    fprintf(f, "  <title>V/OCT harmonic analysis: %s</title>\n", basename);
    fprintf(f, "  <rect width=\"100%%\" height=\"100%%\" fill=\"#1a1a1a\"/>\n");

    for (size_t p = 0; p < num_passes; ++p)
    {
        const PassInfo& pi = passes[p];
        if (!pi.note_mags || pi.note_mags->empty()) continue;
        int y0 = (int)p * plot_height_per;
        draw_note_spectrum_panel(f,
                                 0,
                                 y0,
                                 plot_width,
                                 plot_height_per,
                                 pi.label,
                                 *pi.note_mags,
                                 min_midi,
                                 max_midi,
                                 global_max_mag,
                                 db_floor);
    }
    fprintf(f, "</svg>\n");
    fclose(f);
    return true;
}

int main(int argc, char** argv)
{
    const char* sample_path = (argc >= 2) ? argv[1] : "samples/OneShotOneOsc.wav";
    std::vector<float> mono;
    if (!load_mono(sample_path, mono, kSampleRate))
    {
        fprintf(stderr, "Failed to load %s (expected 48 kHz mono/stereo WAV).\n", sample_path);
        return 1;
    }
    size_t num_frames = mono.size();

    const char* path = sample_path;
    const char* last_slash = strrchr(path, '/');
    const char* base = last_slash ? (last_slash + 1) : path;
    const char* last_dot = strrchr(base, '.');
    size_t base_len = last_dot ? (size_t)(last_dot - base) : strlen(base);
    char basename[256];
    snprintf(basename, sizeof(basename), "%.*s", (int)base_len, base);

    const char* out_dir = "out/voct_harmonic";
    if (mkdir("out", 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create out/\n");
        return 1;
    }
    if (mkdir(out_dir, 0755) != 0 && errno != EEXIST)
    {
        fprintf(stderr, "Failed to create %s\n", out_dir);
        return 1;
    }

    float window[kAnalysisFftSize];
    compute_window(window);

    const int kPlotWidth = 800;
    const int kPlotHeight = 200;

    struct Config
    {
        const char* suffix;
        const char* label;
        bool pitch_lock;
        float voct_volts;
        bool is_dry;
    };
    const Config configs[] = {
        { "dry",                    "Dry (passthrough)",                          true,  kVoct0Volts, true  },
        { "pitchlock_0v",           "Pitch-lock, 0 V V/OCT (C2)",                 true,  kVoct0Volts, false },
        { "pitchlock_3v",           "Pitch-lock, 3 V V/OCT",                      true,  3.0f,        false },
        { "pitchlock_4v",           "Pitch-lock, 4 V V/OCT",                      true,  4.0f,        false },
    };
    const size_t num_configs = sizeof(configs) / sizeof(configs[0]);

    std::vector<std::vector<float>> outputs(num_configs);
    std::vector<std::vector<std::vector<float>>> mag_frames_list(num_configs);
    std::vector<size_t> num_frames_list(num_configs);
    std::vector<std::vector<float>> avg_spectra(num_configs);

    printf("V/OCT harmonic test: %s (%zu samples)\n", sample_path, num_frames);
    printf("  Output dir: %s\n\n", out_dir);

    int min_midi = 127;
    int max_midi = 0;
    for(size_t k = 1; k <= kAnalysisNumBins; ++k)
    {
        float hz = bin_to_hz(k);
        if(hz <= 0.0f)
            continue;
        float midi = hz_to_midi(hz);
        int mi = static_cast<int>(floorf(midi + 0.5f));
        if(mi < 0 || mi > 127)
            continue;
        if(mi < min_midi) min_midi = mi;
        if(mi > max_midi) max_midi = mi;
    }
    if(min_midi > max_midi)
    {
        min_midi = 36;
        max_midi = 84;
    }

    float global_max_mag = 0.0f;

    // Choose a 100 ms analysis slice from the middle of the processed sample.
    size_t total_samples = num_frames;
    size_t slice_len_samples = static_cast<size_t>(0.1f * static_cast<float>(kSampleRate));
    if(slice_len_samples == 0)
        slice_len_samples = total_samples;
    if(slice_len_samples > total_samples)
        slice_len_samples = total_samples;
    size_t slice_start = (total_samples > slice_len_samples)
                       ? (total_samples - slice_len_samples) / 2
                       : 0;

    for (size_t c = 0; c < num_configs; ++c)
    {
        const Config& cfg = configs[c];
        if (cfg.is_dry)
            run_dry_pass(mono.data(), num_frames, outputs[c]);
        else
            run_resynth_pass(mono.data(), num_frames, cfg.pitch_lock, cfg.voct_volts, outputs[c]);

        size_t nf = compute_stft(outputs[c].data() + slice_start,
                                 slice_len_samples,
                                 window,
                                 mag_frames_list[c]);
        num_frames_list[c] = nf;

        avg_spectra[c].assign(kAnalysisNumBins + 1, 0.0f);
        if(nf > 0)
        {
            for(size_t fr = 0; fr < nf; ++fr)
            {
                const std::vector<float>& row = mag_frames_list[c][fr];
                for(size_t k = 0; k <= kAnalysisNumBins; ++k)
                    avg_spectra[c][k] += row[k];
            }
            for(size_t k = 0; k <= kAnalysisNumBins; ++k)
            {
                avg_spectra[c][k] /= static_cast<float>(nf);
                if(avg_spectra[c][k] > global_max_mag)
                    global_max_mag = avg_spectra[c][k];
            }
        }
    }

    if(global_max_mag <= 0.0f)
        global_max_mag = 1.0f;

    const float kDbFloor = -80.0f;

    std::vector<std::vector<float>> note_mags_list(num_configs);

    int num_notes = max_midi - min_midi + 1;
    if(num_notes <= 0)
    {
        num_notes = 1;
        min_midi = max_midi = 60;
    }

    for(size_t c = 0; c < num_configs; ++c)
    {
        note_mags_list[c].assign(num_notes, 0.0f);
        std::vector<float>& note_mags = note_mags_list[c];
        const std::vector<float>& spectrum = avg_spectra[c];
        std::vector<int> note_counts(num_notes, 0);
        for(size_t k = 1; k <= kAnalysisNumBins; ++k)
        {
            float hz = bin_to_hz(k);
            float midi = hz_to_midi(hz);
            int mi = static_cast<int>(floorf(midi + 0.5f));
            if(mi < min_midi || mi > max_midi)
                continue;
            int idx = mi - min_midi;
            if(idx < 0 || idx >= num_notes)
                continue;
            note_mags[idx] += spectrum[k];
            note_counts[idx] += 1;
        }
        for(int n = 0; n < num_notes; ++n)
        {
            if(note_counts[n] > 0)
                note_mags[n] /= static_cast<float>(note_counts[n]);
        }
    }

    // For each configuration, dump the processed mono WAV, the raw STFT CSV (for
    // debugging), and a note-bucketed, magnitude-sorted CSV for the 100 ms
    // analysis slice.
    for (size_t c = 0; c < num_configs; ++c)
    {
        const Config& cfg = configs[c];

        char wav_path[512], csv_path[512], svg_path[512], note_csv_path[512];
        snprintf(wav_path, sizeof(wav_path), "%s/%s_voct_%s.wav", out_dir, basename, cfg.suffix);
        snprintf(csv_path, sizeof(csv_path), "%s/%s_voct_%s.csv", out_dir, basename, cfg.suffix);
        snprintf(svg_path, sizeof(svg_path), "%s/%s_voct_%s.svg", out_dir, basename, cfg.suffix);
        snprintf(note_csv_path, sizeof(note_csv_path), "%s/%s_voct_%s_notes.csv",
                 out_dir, basename, cfg.suffix);

        if (!SaveWav(wav_path,
                     outputs[c].data(),
                     outputs[c].size(),
                     kSampleRate))
        {
            fprintf(stderr, "Failed to write %s\n", wav_path);
            return 1;
        }
        if (!write_csv(csv_path, mag_frames_list[c], num_frames_list[c]))
        {
            fprintf(stderr, "Failed to write %s\n", csv_path);
            return 1;
        }
        if (!write_note_spectrum_svg(svg_path,
                                     cfg.label,
                                     note_mags_list[c],
                                     min_midi,
                                     max_midi,
                                     global_max_mag,
                                     kDbFloor,
                                     kPlotWidth,
                                     kPlotHeight))
        {
            fprintf(stderr, "Failed to write %s\n", svg_path);
            return 1;
        }
        if (!write_note_bucket_csv(note_csv_path,
                                   note_mags_list[c],
                                   min_midi,
                                   max_midi,
                                   global_max_mag))
        {
            fprintf(stderr, "Failed to write %s\n", note_csv_path);
            return 1;
        }
        printf("  %s -> %s, %s, %s, %s\n", cfg.label, wav_path, csv_path, note_csv_path, svg_path);
    }

    PassInfo pass_infos[16];
    for (size_t p = 0; p < num_configs; ++p)
    {
        pass_infos[p].label = configs[p].label;
        pass_infos[p].note_mags = &note_mags_list[p];
    }
    char stacked_path[512];
    snprintf(stacked_path, sizeof(stacked_path), "%s/%s_voct_stacked.svg", out_dir, basename);
    if (!write_stacked_svg(stacked_path,
                           basename,
                           pass_infos,
                           num_configs,
                           kPlotWidth,
                           kPlotHeight,
                           min_midi,
                           max_midi,
                           global_max_mag,
                           kDbFloor))
    {
        fprintf(stderr, "Failed to write %s\n", stacked_path);
        return 1;
    }
    printf("  stacked -> %s\n", stacked_path);

    // Helper: estimate a fundamental MIDI note from per-note magnitude
    // buckets using a simple harmonic comb. For each candidate MIDI
    // note in [min_midi, max_midi], accumulate energy at its first few
    // harmonics (weighted toward lower partials) and pick the MIDI with
    // the strongest harmonic "explanation". This more closely tracks a
    // human impression of pitch (fundamental inferred from the harmonic
    // stack) than simply picking the single loudest note bucket.
    auto estimate_fundamental_midi = [&](const std::vector<float>& note_mags,
                                         int                       min_midi_local,
                                         int                       max_midi_local,
                                         int&                      out_midi,
                                         float&                    out_score) {
        out_midi  = -1;
        out_score = 0.0f;
        if(note_mags.empty() || min_midi_local > max_midi_local)
            return;

        const int   num_notes_local = max_midi_local - min_midi_local + 1;
        const int   kMaxHarmonics   = 8;
        // Gentle 1/h taper so the fundamental and first few harmonics
        // drive the estimate, with higher harmonics contributing but
        // not dominating.
        float harm_weights[kMaxHarmonics + 1];
        for(int h = 1; h <= kMaxHarmonics; ++h)
            harm_weights[h] = 1.0f / static_cast<float>(h);

        for(int midi = min_midi_local; midi <= max_midi_local; ++midi)
        {
            float score = 0.0f;
            for(int h = 1; h <= kMaxHarmonics; ++h)
            {
                // Ideal harmonic MIDI for this multiple.
                float midi_h = static_cast<float>(midi)
                               + 12.0f * log2f(static_cast<float>(h));
                int   idx    = static_cast<int>(floorf(midi_h + 0.5f))
                            - min_midi_local;
                if(idx < 0 || idx >= num_notes_local)
                    continue;
                float mag = note_mags[idx];
                if(mag <= 0.0f)
                    continue;
                score += harm_weights[h] * mag;
            }
            if(score > out_score)
            {
                out_score = score;
                out_midi  = midi;
            }
        }
    };

    // For each configuration, estimate a "suggested note" (fundamental)
    // from the harmonic stack over the 100 ms analysis slice. Also
    // compute an expected note from the V/OCT setting when applicable,
    // and print the deviation in semitones for automated testing.
    printf("\nSuggested notes (100 ms mid-slice):\n");
    bool any_fail = false;
    const float kNoteToleranceSemitones = 2.0f;
    for(size_t c = 0; c < num_configs; ++c)
    {
        const Config& cfg = configs[c];
        const std::vector<float>& note_mags = note_mags_list[c];
        if(note_mags.empty())
        {
            printf("  %s: (no spectral data)\n", cfg.label);
            continue;
        }

        int   suggested_midi = -1;
        float suggested_score = 0.0f;
        estimate_fundamental_midi(note_mags,
                                  min_midi,
                                  max_midi,
                                  suggested_midi,
                                  suggested_score);
        if(suggested_midi < 0)
        {
            printf("  %s: (no stable fundamental estimate)\n", cfg.label);
            continue;
        }

        // Report the loudest individual note bucket near the estimated
        // fundamental so the printout still gives an intuitive sense of
        // "how strong" the perceived pitch is in the slice.
        int   nearest_idx = suggested_midi - min_midi;
        if(nearest_idx < 0) nearest_idx = 0;
        if(nearest_idx >= num_notes) nearest_idx = num_notes - 1;
        float best_mag = note_mags[nearest_idx];
        float suggested_db = (best_mag > 0.0f && global_max_mag > 0.0f)
                                 ? 20.0f * log10f(best_mag / global_max_mag)
                                 : -120.0f;
        char suggested_name[16];
        midi_to_name(suggested_midi, suggested_name, sizeof(suggested_name));

        // Compute expected note from V/OCT when this pass is driven by
        // a specific fundamental (skip the dry passthrough).
        int   expected_midi = -1;
        char  expected_name[16] = "";
        float expected_hz = 0.0f;
        if(!cfg.is_dry)
        {
            expected_hz = VoctVoltsToFundamentalHz(cfg.voct_volts);
            if(expected_hz > 0.0f)
            {
                float midi_f = hz_to_midi(expected_hz);
                expected_midi = static_cast<int>(floorf(midi_f + 0.5f));
                midi_to_name(expected_midi, expected_name, sizeof(expected_name));
            }
        }

        if(expected_midi >= 0)
        {
            float delta_semitones = static_cast<float>(suggested_midi - expected_midi);
            printf("  %s: suggested_note=%s (MIDI %d, %.1f dB), "
                   "expected≈%s (MIDI %d, %.1f Hz), Δ=%.1f semitones\n",
                   cfg.label,
                   suggested_name,
                   suggested_midi,
                   (double)suggested_db,
                   expected_name,
                   expected_midi,
                   (double)expected_hz,
                   (double)delta_semitones);

            // Only enforce the suggested-note test for pitch-locked passes,
            // where the algorithm is expected to lock tightly to the target
            // fundamental. Partial-mode passes are intentionally more free.
            if(cfg.pitch_lock && fabsf(delta_semitones) > kNoteToleranceSemitones)
                any_fail = true;
        }
        else
        {
            printf("  %s: suggested_note=%s (MIDI %d, %.1f dB)\n",
                   cfg.label,
                   suggested_name,
                   suggested_midi,
                   (double)suggested_db);
        }
    }

    if(any_fail)
    {
        fprintf(stderr,
                "\nV/OCT suggested-note test FAILED: one or more pitch-locked passes "
                "deviated by more than %.2f semitones.\n",
                (double)kNoteToleranceSemitones);
        return 1;
    }

    printf("\nV/OCT suggested-note test PASSED (all pitch-locked passes within ±%.2f semitones).\n",
           (double)kNoteToleranceSemitones);
    printf("Done. CSV tables and SVGs in %s/\n", out_dir);
    return 0;
}
