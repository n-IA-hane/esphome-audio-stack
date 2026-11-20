#pragma once

#include <cstdint>

namespace esphome::esp_afe {

// The AFE configuration owns the channel roles; transport microphones are
// interleaved independently of the AFE layout. Unknown channels carry silence.
template<typename PcmLayout>
inline void stage_afe_input_frame(int16_t *dst, const int16_t *in_mic, const int16_t *in_ref, int samples,
                                         int transport_mic_channels, const PcmLayout &layout,
                                         const int16_t *in_secondary = nullptr) {
  if (dst == nullptr || in_mic == nullptr || samples <= 0 || transport_mic_channels <= 0) return;
  for (int channel = 0; channel < layout.total_ch_num; ++channel) {
    const int16_t *source = nullptr;
    int stride = 1;
    for (int mic = 0; mic < layout.mic_num; ++mic) {
      if (layout.mic_ids[mic] == channel && mic < transport_mic_channels) {
        source = in_secondary != nullptr ? (mic == 0 ? in_mic : in_secondary) : in_mic + mic;
        stride = in_secondary != nullptr ? 1 : transport_mic_channels;
        break;
      }
    }
    for (int ref = 0; ref < layout.ref_num; ++ref) {
      if (layout.ref_ids[ref] == channel) source = in_ref;
    }
    int16_t *output = dst + channel;
    if (source == nullptr) {
      for (int i = 0; i < samples; ++i, output += layout.total_ch_num) *output = 0;
    } else {
      for (int i = 0; i < samples; ++i, output += layout.total_ch_num, source += stride) *output = *source;
    }
  }
}

}  // namespace esphome::esp_afe
