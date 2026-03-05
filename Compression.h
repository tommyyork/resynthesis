// Reusable feedforward compressor for Daisy Patch SM and other modules.
// Supports standard compression (ratio > 0) and "negative ratio" (Omnipressor-style)
// for boosting below threshold and compressing above (near-full volume, dynamics preserved).
// No dependency on libDaisy; use with any sample rate.
// Lives in daisy::patch_sm so it can be used alongside Patch SM hardware types.

#ifndef COMPRESSION_H
#define COMPRESSION_H

namespace daisy {
namespace patch_sm {

class Compressor
{
public:
    Compressor() = default;

    /** Initialize with sample rate (for attack/release smoothing). */
    void Init(float sample_rate_hz);

    /**
     * Set compressor parameters.
     * @param threshold Linear level above which compression applies (e.g. 0.25f ~ -12 dB).
     * @param ratio     Compression ratio (e.g. 2.f = 2:1). If negative (e.g. -2.f),
     *                  acts Omnipressor-style: below threshold signal is boosted,
     *                  above threshold compressed for near-full volume while preserving dynamics.
     * @param makeup    Make-up gain applied after compression (linear).
     * @param attack_s  Attack time in seconds (e.g. 0.0003f = 0.3 ms).
     * @param release_s Release time in seconds (e.g. 0.05f = 50 ms).
     */
    void SetParams(float threshold, float ratio, float makeup,
                   float attack_s, float release_s);

    /** Process one sample; returns compressed output. */
    float Process(float in);

private:
    float sample_rate_  = 48000.0f;
    float threshold_    = 0.25f;
    float ratio_        = 2.0f;
    float makeup_       = 1.8f;
    float attack_s_     = 0.0003f;
    float release_s_    = 0.05f;
    float env_          = 0.0f;
};

} // namespace patch_sm
} // namespace daisy

#endif
