#pragma once

#include <cstddef>
#include <cstdint>
#include <cmath>

namespace esphome::esp_audio_stack {

// Keep transport work bounded independently of DSP batch sizes (16 ms at
// 16 kHz). Processors assemble larger native frames through their staging.
inline constexpr size_t DEFAULT_AUDIO_FRAME_SAMPLES = 256;

// Post-processor boost is a constant multiplier, not an automatic level
// controller. Compute its Q16.16 coefficient only when the control changes.
inline uint32_t microphone_boost_to_q16(float gain) {
  if (!(gain > 1.0f) || !std::isfinite(gain))
    return 1U << 16;
  return static_cast<uint32_t>(std::lround(static_cast<double>(gain > 32.0f ? 32.0f : gain) * 65536.0));
}

// Operates on individual interleaved S16 samples, independently of channels,
// chunk boundaries and previous audio. Round magnitude to nearest, ties to
// even, then restore its sign so small negative samples have no DC bias.
inline void apply_microphone_boost_q16(int16_t *samples, size_t count, uint32_t gain_q16) {
  if (samples == nullptr || gain_q16 == (1U << 16))
    return;
  for (size_t i = 0; i < count; ++i) {
    const int64_t product = static_cast<int64_t>(samples[i]) * gain_q16;
    const uint64_t magnitude = product < 0 ? -product : product;
    uint64_t rounded = magnitude >> 16;
    const uint32_t fraction = magnitude & 0xffffU;
    rounded += fraction > 0x8000U || (fraction == 0x8000U && (rounded & 1U));
    const int64_t value = product < 0 ? -static_cast<int64_t>(rounded) : static_cast<int64_t>(rounded);
    samples[i] = static_cast<int16_t>(value > 32767 ? 32767 : value < -32768 ? -32768 : value);
  }
}

#ifndef ESPHOME_AUDIO_STACK_SCALE_SAMPLE_DEFINED
#define ESPHOME_AUDIO_STACK_SCALE_SAMPLE_DEFINED
// Scale a 16-bit PCM sample by a float gain with saturation clamping.
// Supports gains > 1.0 (amplification) unlike ESPHome's Q15 scale_audio_samples().
static inline int16_t scale_sample(int16_t sample, float gain) {
  int32_t s = static_cast<int32_t>(sample * gain);
  if (s > 32767)
    return 32767;
  if (s < -32768)
    return -32768;
  return static_cast<int16_t>(s);
}
#endif

#ifndef ESPHOME_AUDIO_STACK_COMPUTE_RMS_DBFS_DEFINED
#define ESPHOME_AUDIO_STACK_COMPUTE_RMS_DBFS_DEFINED
// 20*log10(32768): subtract from 10*log10(mean) to get dBFS without an
// extra sqrt() (more numerically stable for small means).
static constexpr float RMS_DBFS_OFFSET = 90.30899870f;
static constexpr float RMS_DBFS_SILENCE = -120.0f;

// RMS power in dBFS for int16 PCM samples. Returns -120 dBFS for silence.
// `stride` lets callers walk channel-interleaved buffers (e.g. TDM slots).
static inline float compute_rms_dbfs_i16(const int16_t *data, size_t samples, size_t stride = 1) {
  if (data == nullptr || samples == 0)
    return RMS_DBFS_SILENCE;
  uint64_t sumsq = 0;
  for (size_t i = 0; i < samples; i++) {
    int32_t s = data[i * stride];
    sumsq += static_cast<uint64_t>(s * s);
  }
  float mean = static_cast<float>(sumsq) / static_cast<float>(samples);
  if (mean <= 0.0f)
    return RMS_DBFS_SILENCE;
  return 10.0f * log10f(mean) - RMS_DBFS_OFFSET;
}

// Variant for int32 (e.g. TDM slot data with top 16 bits used). Each sample
// is right-shifted by 16 to land in int16 range before squaring.
static inline float compute_rms_dbfs_i32_top16(const int32_t *data, size_t samples, size_t stride = 1) {
  if (data == nullptr || samples == 0)
    return RMS_DBFS_SILENCE;
  uint64_t sumsq = 0;
  for (size_t i = 0; i < samples; i++) {
    int32_t s = data[i * stride] >> 16;
    sumsq += static_cast<uint64_t>(s * s);
  }
  float mean = static_cast<float>(sumsq) / static_cast<float>(samples);
  if (mean <= 0.0f)
    return RMS_DBFS_SILENCE;
  return 10.0f * log10f(mean) - RMS_DBFS_OFFSET;
}
#endif

#ifndef ESPHOME_AUDIO_STACK_DC_BLOCKER_STATE_DEFINED
#define ESPHOME_AUDIO_STACK_DC_BLOCKER_STATE_DEFINED
struct DcBlockerState {
  int32_t prev_input_q16{0};
  int32_t prev_output_q16{0};

  // y[n]=x[n]-x[n-1]+(1-2^-10)y[n-1]; cutoff is about fs/(2*pi*1024).
  inline int16_t process(int16_t sample) {
    int32_t input = static_cast<int32_t>(sample) << 16;
    int32_t output = input - this->prev_input_q16 + this->prev_output_q16 - (this->prev_output_q16 >> 10);
    this->prev_input_q16 = input;
    this->prev_output_q16 = output;
    return static_cast<int16_t>(output >> 16);
  }
};
#endif

}  // namespace esphome::esp_audio_stack
