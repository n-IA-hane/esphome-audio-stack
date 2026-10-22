#include "esp_audio_stack.h"

#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_CAPTURE)
#include <esp_timer.h>

namespace esphome::esp_audio_stack {

void ESPAudioStack::capture_audio_(CapturePoint point, const void* data, size_t frames, uint32_t rate, uint8_t bits,
                                   uint8_t channels, uint8_t stride, uint8_t first, uint8_t second) {
  if (this->capture_observer_ == nullptr || !this->capture_observer_->capture_active(point) || data == nullptr) return;
  const CaptureFrame view{
      point, data,     frames, rate,           this->capture_generation(), static_cast<uint64_t>(esp_timer_get_time()),
      bits,  channels, stride, {first, second}};
  this->capture_observer_->on_capture_frame(view);
}

void ESPAudioStack::capture_raw_audio_(const AudioTaskCtx& ctx) {
  // Physical microphone slots, before downmix, DC filtering, digital gain or DSP.
  uint8_t stride = ctx.rx_slot_mode_stereo ? 2 : 1;
  uint8_t slots[2] = {static_cast<uint8_t>(this->mic_channel_right_ && stride == 2), 0};
  uint8_t channels = 1;
#ifdef USE_ESP_AUDIO_STACK_STD_DUAL_MIC
  if (ctx.std_second_mic_slot >= 0) {
    slots[0] = ctx.std_primary_mic_slot;
    slots[1] = ctx.std_second_mic_slot;
    channels = 2;
  }
#endif
#ifdef USE_ESP_AUDIO_STACK_TDM_BUS
  if (ctx.use_tdm_bus) {
    stride = ctx.rx_slot_count;
    slots[0] = ctx.tdm_rx_layout.index(ctx.tdm_mic_slot);
    if (ctx.tdm_second_mic_slot >= 0) {
      slots[1] = ctx.tdm_rx_layout.index(ctx.tdm_second_mic_slot);
      channels = 2;
    }
  }
#endif
  if (slots[0] >= stride || (channels == 2 && slots[1] >= stride)) return;
  this->capture_audio_(CapturePoint::RAW_MIC, ctx.rx_buffer, ctx.bus_frame_size, this->sample_rate_, ctx.i2s_bps * 8,
                       channels, stride, slots[0], slots[1]);
}

}  // namespace esphome::esp_audio_stack
#endif
