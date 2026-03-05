#ifndef RESYNTH_TEST_WAV_IO_H
#define RESYNTH_TEST_WAV_IO_H

#include <cstdint>
#include <cstdio>
#include <vector>

struct WavInfo {
    uint32_t sampleRate;
    uint16_t numChannels;
    uint32_t numFrames;
};

bool LoadWav(const char* path, std::vector<float>& samples, WavInfo& info);
bool SaveWav(const char* path, const float* samples, size_t numFrames, uint32_t sampleRate, uint16_t numChannels = 1);

#endif
