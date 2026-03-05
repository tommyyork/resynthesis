#include "wav_io.h"
#include <cstring>
#include <algorithm>

static bool ReadChunk(FILE* f, char id[4], uint32_t* size)
{
    if (fread(id, 1, 4, f) != 4) return false;
    if (fread(size, 1, 4, f) != 4) return false;
    return true;
}

static bool SeekPastChunk(FILE* f, uint32_t size)
{
    return fseek(f, size, SEEK_CUR) == 0;
}

bool LoadWav(const char* path, std::vector<float>& samples, WavInfo& info)
{
    FILE* f = fopen(path, "rb");
    if (!f) return false;

    char riff[4], fmtId[4];
    uint32_t fileSize, fmtSize, dataSize;
    uint16_t formatTag, numChannels, bitsPerSample;
    uint16_t blockAlign;  // 2 bytes in WAV fmt chunk
    uint32_t sampleRate, byteRate;

    if (fread(riff, 1, 4, f) != 4 || memcmp(riff, "RIFF", 4) != 0) { fclose(f); return false; }
    if (fread(&fileSize, 4, 1, f) != 1) { fclose(f); return false; }
    if (fread(fmtId, 1, 4, f) != 4 || memcmp(fmtId, "WAVE", 4) != 0) { fclose(f); return false; }

    for (;;)
    {
        if (!ReadChunk(f, fmtId, &fmtSize)) { fclose(f); return false; }
        if (memcmp(fmtId, "fmt ", 4) == 0)
        {
            if (fmtSize < 16) { fclose(f); return false; }
            if (fread(&formatTag, 2, 1, f) != 1) { fclose(f); return false; }
            if (fread(&numChannels, 2, 1, f) != 1) { fclose(f); return false; }
            if (fread(&sampleRate, 4, 1, f) != 1) { fclose(f); return false; }
            if (fread(&byteRate, 4, 1, f) != 1) { fclose(f); return false; }
            if (fread(&blockAlign, 2, 1, f) != 1) { fclose(f); return false; }
            if (fread(&bitsPerSample, 2, 1, f) != 1) { fclose(f); return false; }
            if (fmtSize > 16 && SeekPastChunk(f, fmtSize - 16)) {}
        }
        else if (memcmp(fmtId, "data", 4) == 0)
        {
            dataSize = fmtSize;
            break;
        }
        else
        {
            if (!SeekPastChunk(f, fmtSize)) { fclose(f); return false; }
        }
    }

    if (formatTag != 1 || (bitsPerSample != 16 && bitsPerSample != 24 && bitsPerSample != 32)) { fclose(f); return false; }

    uint32_t numSamples = dataSize / (numChannels * (bitsPerSample / 8));
    samples.resize(numChannels * numSamples);

    if (bitsPerSample == 16)
    {
        std::vector<int16_t> buf(numChannels * numSamples);
        if (fread(buf.data(), 2, buf.size(), f) != buf.size()) { fclose(f); return false; }
        for (size_t i = 0; i < buf.size(); ++i)
            samples[i] = buf[i] / 32768.0f;
    }
    else if (bitsPerSample == 24)
    {
        size_t n = numChannels * numSamples;
        for (size_t i = 0; i < n; ++i)
        {
            uint8_t b[3];
            if (fread(b, 1, 3, f) != 3) { fclose(f); return false; }
            int32_t v = (int32_t)(b[0]) | ((int32_t)(b[1]) << 8) | ((int32_t)(b[2]) << 16);
            if (v & 0x800000) v |= 0xFF000000;
            samples[i] = v / 8388608.0f;
        }
    }
    else
    {
        std::vector<int32_t> buf(numChannels * numSamples);
        if (fread(buf.data(), 4, buf.size(), f) != buf.size()) { fclose(f); return false; }
        for (size_t i = 0; i < buf.size(); ++i)
            samples[i] = buf[i] / 2147483648.0f;
    }

    fclose(f);
    info.sampleRate = sampleRate;
    info.numChannels = numChannels;
    info.numFrames = numSamples;
    return true;
}

bool SaveWav(const char* path, const float* samples, size_t numFrames, uint32_t sampleRate, uint16_t numChannels)
{
    FILE* f = fopen(path, "wb");
    if (!f) return false;

    size_t numSamples = numFrames * numChannels;
    uint32_t dataBytes = (uint32_t)(numSamples * 2);
    uint32_t fileSize = 36 + dataBytes;

    const char riff[] = "RIFF";
    const char wave[] = "WAVE";
    const char fmt[]  = "fmt ";
    const char data[] = "data";
    uint32_t fmtSize = 16;
    uint16_t formatTag = 1;
    uint16_t bitsPerSample = 16;
    uint32_t byteRate = sampleRate * numChannels * 2;
    uint16_t blockAlign = numChannels * 2;

    fwrite(riff, 1, 4, f);
    fwrite(&fileSize, 4, 1, f);
    fwrite(wave, 1, 4, f);
    fwrite(fmt, 1, 4, f);
    fwrite(&fmtSize, 4, 1, f);
    fwrite(&formatTag, 2, 1, f);
    fwrite(&numChannels, 2, 1, f);
    fwrite(&sampleRate, 4, 1, f);
    fwrite(&byteRate, 4, 1, f);
    fwrite(&blockAlign, 2, 1, f);
    fwrite(&bitsPerSample, 2, 1, f);
    fwrite(data, 1, 4, f);
    fwrite(&dataBytes, 4, 1, f);

    std::vector<int16_t> buf(numSamples);
    for (size_t i = 0; i < numSamples; ++i)
    {
        float s = samples[i];
        if (s < -1.0f) s = -1.0f;
        if (s > 1.0f) s = 1.0f;
        buf[i] = (int16_t)(s * 32767.0f);
    }
    fwrite(buf.data(), 2, buf.size(), f);
    fclose(f);
    return true;
}
