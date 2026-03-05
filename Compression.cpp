// Compressor implementation: feedforward envelope follower + gain computer.
// Supports standard (ratio > 0) and negative-ratio (Omnipressor-style) modes.

#include "Compression.h"
#include <cmath>

namespace daisy {
namespace patch_sm {

void Compressor::Init(float sample_rate_hz)
{
    sample_rate_ = sample_rate_hz > 0.0f ? sample_rate_hz : 48000.0f;
    env_         = 0.0f;
}

void Compressor::SetParams(float threshold, float ratio, float makeup,
                          float attack_s, float release_s)
{
    threshold_  = threshold > 0.0f ? threshold : 0.25f;
    ratio_      = ratio;
    makeup_     = makeup > 0.0f ? makeup : 1.0f;
    attack_s_   = attack_s >= 0.0f ? attack_s : 0.0003f;
    release_s_  = release_s > 0.0f ? release_s : 0.05f;
}

float Compressor::Process(float in)
{
    float in_peak = in >= 0.0f ? in : -in;

    float attack_coeff  = 1.0f - std::exp(-1.0f / (attack_s_ * sample_rate_));
    float release_coeff = 1.0f - std::exp(-1.0f / (release_s_ * sample_rate_));
    float coeff         = (in_peak > env_) ? attack_coeff : release_coeff;
    env_ += coeff * (in_peak - env_);

    float gain = 1.0f;
    if (env_ > 1e-6f)
    {
        if (ratio_ < 0.0f)
        {
            // Negative ratio (Omnipressor-style): boost below threshold, compress above
            if (env_ <= threshold_)
                gain = std::pow(threshold_ / env_, 0.5f);
            else
                gain = std::pow(threshold_ / env_, 1.0f - 1.0f / ratio_);
        }
        else
        {
            // Standard: compress above threshold
            if (env_ > threshold_)
                gain = std::pow(threshold_ / env_, 1.0f - 1.0f / ratio_);
        }
        gain *= makeup_;
    }
    return in * gain;
}

} // namespace patch_sm
} // namespace daisy
