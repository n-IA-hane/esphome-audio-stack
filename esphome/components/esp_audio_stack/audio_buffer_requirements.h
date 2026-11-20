#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome::esp_audio_stack {

// Cold, per-session sizes. Their lifetime ends before the frame processing loop.
struct AudioBufferRequirements {
  size_t rx_bytes, mic_bytes, processor_mic_bytes, spk_bytes;
  size_t tx_ref_mono_bytes, spk_ref_bytes, aec_output_bytes;
#ifdef USE_ESP_AUDIO_STACK_STEREO_TX
  size_t tx_interleave_bytes;
#endif
#ifdef USE_ESP_AUDIO_STACK_32BIT
  size_t tx_32_bytes;
#endif

  template<typename Frame>
  static AudioBufferRequirements for_frame(const Frame &ctx, bool have_rx, bool processor_buffers_needed,
                                          bool dual_mic_configured) {
#ifdef USE_ESP_AUDIO_STACK_MIC_DOWNMIX
    // Downmix happens before the processor, even for two physical microphone slots.
    const uint8_t worst_mic_ch = 1;
    (void) dual_mic_configured;
#else
    const uint8_t worst_mic_ch = dual_mic_configured ? 2 : 1;
#endif
    AudioBufferRequirements result{};
    result.rx_bytes = have_rx ? ctx.rx_frame_bytes : 0;
    result.mic_bytes = have_rx && ctx.mic_separate ? ctx.input_frame_bytes : 0;
    result.processor_mic_bytes =
        (processor_buffers_needed && worst_mic_ch > 1)
            ? ctx.input_frame_bytes * (ctx.processor_planar_input ? 1 : worst_mic_ch) : 0;
    result.spk_bytes = ctx.speaker_frame_bytes;
    result.tx_ref_mono_bytes = (processor_buffers_needed && !ctx.use_tdm_bus && ctx.speaker_channels > 1 &&
                                      !ctx.use_stereo_aec_ref && !ctx.use_tdm_ref)
                                         ? ctx.bus_frame_bytes
                                         : 0;
    result.spk_ref_bytes =
        (ctx.use_stereo_aec_ref || ctx.use_tdm_ref || processor_buffers_needed) ? ctx.input_frame_bytes : 0;
    result.aec_output_bytes = processor_buffers_needed ? ctx.output_frame_bytes : 0;
#ifdef USE_ESP_AUDIO_STACK_STEREO_TX
    result.tx_interleave_bytes = (!ctx.use_tdm_bus && ctx.num_ch > 1 && ctx.speaker_channels == 1)
                              ? ctx.bus_frame_size * ctx.num_ch * sizeof(int16_t)
                              : 0;
#endif
#ifdef USE_ESP_AUDIO_STACK_32BIT
    result.tx_32_bytes = ctx.i2s_bps == 4 ? (ctx.bus_frame_size * (ctx.use_tdm_bus ? 1 : ctx.num_ch) * ctx.i2s_bps) : 0;
#endif
    return result;
  }

  static bool fits(size_t bytes, const void *buffer, size_t capacity) {
    return bytes == 0 || (buffer != nullptr && capacity >= bytes);
  }
};

}  // namespace esphome::esp_audio_stack
