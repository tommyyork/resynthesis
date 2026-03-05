// Shared constants and helpers so that offline tests and firmware
// agree on core DSP behaviours (V/OCT mapping, compressor shapes).
//
// This header is deliberately lightweight and has no Daisy hardware
// dependencies so it can be used from both the embedded build and
// the host-side offline tests.

#ifndef RESYNTH_PARAMS_H
#define RESYNTH_PARAMS_H

#include <cmath>

namespace resynth_params {

// Reference tuning for all V/OCT mappings.
static constexpr float kA4Hz = 440.0f;

// 1 V/oct: 0 V = C0 (~16.35 Hz), 1 V = C1 (~32.7 Hz), 2 V = C2 (~65.4 Hz), etc.
// This matches the mapping used in the firmware audio callback.
inline float VoctVoltsToFundamentalHz(float voct_volts)
{
    return kA4Hz * std::pow(2.0f, voct_volts - 4.75f);
}

// Map a semitone offset (0 = C0) to a fundamental in Hz using the same
// reference as the V/OCT mapping above.
inline float SemitonesToFundamentalHz(int semitones)
{
    return kA4Hz
           * std::pow(2.0f,
                      static_cast<float>(semitones) / 12.0f - 4.75f);
}

// Compressor parameter sets shared between firmware and offline tests.
// "Normal" corresponds to the gentle 2:1 setting; "MAX" matches the
// Omnipressor-style shape used when MAX COMP is engaged.
static constexpr float kCompThreshNormal = 0.25f;
static constexpr float kCompRatioNormal  = 2.0f;
static constexpr float kCompMakeupNormal = 1.8f;

static constexpr float kCompThreshMax = 0.2f;
static constexpr float kCompRatioMax  = -2.0f;
static constexpr float kCompMakeupMax = 2.6f;

static constexpr float kCompAttack  = 0.0003f;
static constexpr float kCompRelease = 0.05f;

// Soft-clip knee used around the compressor to keep peaks musical.
static constexpr float kSoftClipLim = 0.95f;

} // namespace resynth_params

#endif // RESYNTH_PARAMS_H

