#include "esp_audio_stack.h"

#ifdef USE_ESP32
#include <new>

namespace esphome::esp_audio_stack {
static const char *const TAG = "audio_stack.diagnostics";

void ESPAudioStack::publish_diagnostic_hardware_() {
  DiagnosticHardware next;
  next.state = this->i2s_hardware_state_.load(std::memory_order_relaxed);
  const auto state = static_cast<I2SHardwareState>(next.state);
  if (state == I2SHardwareState::READY || state == I2SHardwareState::RUNNING) {
    next.dma_frames = this->tx_completion_dma_frames_;
    next.dma_descriptors = this->tx_completion_queue_size_ / 2U;
#ifdef USE_ESP_AUDIO_STACK_HARDWARE_CODEC
    next.prepared = this->codec_backend_.is_prepared();
    next.open = this->codec_backend_.is_open();
    auto copy = [](const CodecDevBackend::StreamLayout &in, DiagnosticLayout &out) {
      if (!in.valid) return;
      out.rate = in.bus.sample_rate;
      out.data_bits = in.bus.data_bit;
      out.slot_bits = in.bus.slot_bit;
      out.slots = in.bus.total_slot;
      out.mask = in.bus.slot_mask;
      out.memory_map = in.memory.value;
      out.valid = true;
    };
    if (next.open) {
      copy(this->codec_backend_.rx_layout(), next.rx);
      copy(this->codec_backend_.tx_layout(), next.tx);
    }
#else
    next.prepared = true;
    next.open = state == I2SHardwareState::RUNNING;
#endif
  }
  portENTER_CRITICAL(&this->diagnostic_lock_);
  next.generation = this->diagnostic_hardware_.generation + 1;
  this->diagnostic_hardware_ = next;
  portEXIT_CRITICAL(&this->diagnostic_lock_);
}

void ESPAudioStack::dump_bus_configuration_() const {
  bool split = false;
  unsigned rx_port = this->i2s_num_, tx_port = this->i2s_num_;
#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
  split = this->dual_i2s_bus_;
  if (split) { rx_port = this->rx_bus_.i2s_num; tx_port = this->tx_bus_.i2s_num; }
#endif
  ESP_LOGI(TAG, "requested: bus=%s rx_port=%u tx_port=%u rate=%u mic_rate=%u bits=%u slot_bits=%u slot_width_auto=%s",
           split ? "split" : "shared", rx_port, tx_port, (unsigned) this->sample_rate_,
           (unsigned) (this->output_sample_rate_ ? this->output_sample_rate_ : this->sample_rate_),
           (unsigned) this->bits_per_sample_,
           (unsigned) (this->slot_bit_width_ ? this->slot_bit_width_ : this->bits_per_sample_),
           YESNO(this->slot_bit_width_ == 0));
  if (this->use_tdm_bus_) {
    ESP_LOGI(TAG, "tdm: total_slots=%u mic=%u mic2=%d tx_slot=%u reference=%s ref_slot=%u",
             (unsigned) this->tdm_total_slots_, (unsigned) this->tdm_mic_slot_,
             (int) this->tdm_second_mic_slot_, (unsigned) this->tdm_tx_slot_,
             this->use_tdm_ref_ ? "hardware" : "software", (unsigned) this->tdm_ref_slot_);
  } else {
    ESP_LOGI(TAG, "standard: mic_channel=%s rx_mode=%s reference=%s ref_channel=%s",
             this->mic_channel_right_ ? "right" : "left", this->rx_slot_mode_stereo_ ? "stereo" : "mono",
             this->use_stereo_aec_ref_ ? "codec_feedback" : "software",
             this->use_stereo_aec_ref_ ? (this->ref_channel_right_ ? "right" : "left") : "not_applicable");
#ifdef USE_ESP_AUDIO_STACK_STD_DUAL_MIC
    ESP_LOGI(TAG, "standard microphones: primary=%u secondary=%d",
             this->std_primary_mic_slot_, this->std_second_mic_slot_);
#endif
  }
#ifdef USE_AUDIO_PROCESSOR
  ESP_LOGI(TAG, "processor: present=%s enabled=%s", YESNO(this->processor_ != nullptr),
           YESNO(this->processor_enabled_.load(std::memory_order_relaxed)));
#else
  ESP_LOGI(TAG, "processor: absent");
#endif
}

void ESPAudioStack::dump_diagnostics() {
  const uint32_t now = millis();
  if (this->diagnostic_dump_ || (this->last_diagnostics_ms_ != 0 && now - this->last_diagnostics_ms_ < 1000)) return;
  if (this->is_failed()) {
    ESP_LOGI(TAG, "BEGIN v=1 uptime_ms=%u failed=YES runtime=unavailable", (unsigned) now);
    ESP_LOGI(TAG, "END v=1 uptime_ms=%u see_boot_error_log", (unsigned) now);
    return;
  }
  auto dump = std::unique_ptr<DiagnosticDump>(new (std::nothrow) DiagnosticDump());
  if (!dump) { ESP_LOGW(TAG, "Diagnostic snapshot allocation failed"); return; }
  this->last_diagnostics_ms_ = now;
  dump->requested_ms = now;
  portENTER_CRITICAL(&this->diagnostic_lock_);
  dump->hardware = this->diagnostic_hardware_;
  portEXIT_CRITICAL(&this->diagnostic_lock_);
  const auto state = this->i2s_hardware_state_.load(std::memory_order_relaxed);
  dump->transition = state != dump->hardware.state || state == static_cast<uint8_t>(I2SHardwareState::PREPARING) ||
                     state == static_cast<uint8_t>(I2SHardwareState::STOPPING);
  dump->mic = this->has_mic_consumers_.load(std::memory_order_relaxed);
  dump->speaker = this->speaker_running_.load(std::memory_order_relaxed);
  dump->paused = this->speaker_paused_.load(std::memory_order_relaxed);
  dump->runtime = static_cast<uint8_t>(this->compute_runtime_state_());
  dump->error = this->has_i2s_error_.load(std::memory_order_relaxed);
  dump->queued_bytes = this->get_speaker_buffer_available();
  dump->idle_drops = this->tx_completion_idle_event_drops_.load(std::memory_order_relaxed);
  dump->pending_tx = this->tx_completion_pending_real_records_.load(std::memory_order_relaxed);
  dump->stack_min_free = this->audio_task_handle_ != nullptr ? uxTaskGetStackHighWaterMark(this->audio_task_handle_) : 0;
  this->diagnostic_dump_ = std::move(dump);
  this->emit_diagnostic_section_();
}

void ESPAudioStack::emit_diagnostic_section_() {
  if (!this->diagnostic_dump_) return;
  auto &dump = *this->diagnostic_dump_;
  const auto &hw = dump.hardware;
  auto layout = [&dump](const char *direction, const DiagnosticLayout &value) {
    if (dump.transition || !value.valid) {
      ESP_LOGI(TAG, "%s effective layout: not available", direction);
    } else {
      ESP_LOGI(TAG, "%s effective: rate=%u bits=%u slot_bits=%u slots=%u mask=0x%x memory_map=0x%x",
               direction, (unsigned) value.rate, value.data_bits, value.slot_bits, value.slots,
               value.mask, (unsigned) value.memory_map);
    }
  };
  switch (dump.step++) {
    case 0:
      ESP_LOGI(TAG, "BEGIN v=1 uptime_ms=%u generation=%u transition=%s failed=NO",
               (unsigned) dump.requested_ms, (unsigned) hw.generation, YESNO(dump.transition));
      break;
    case 1:
      ESP_LOGI(TAG, "state=%s hardware=%s mic_consumers=%s speaker=%s paused=%s i2s_error=%s",
               runtime_state_to_string(static_cast<AudioStackRuntimeState>(dump.runtime)),
               i2s_hardware_state_to_string(static_cast<I2SHardwareState>(hw.state)),
               YESNO(dump.mic), YESNO(dump.speaker), YESNO(dump.paused), YESNO(dump.error));
      break;
    case 2:
      this->dump_bus_configuration_();
      break;
    case 3:
#ifdef USE_ESP_AUDIO_STACK_HARDWARE_CODEC
      ESP_LOGI(TAG, "codec: input=%s output=%s prepared=%s open=%s",
               this->codec_backend_.input_codec_name(), this->codec_backend_.output_codec_name(),
               YESNO(hw.prepared), YESNO(hw.open));
#else
      ESP_LOGI(TAG, "codec: absent");
#endif
      break;
    case 4: layout("RX", hw.rx); break;
    case 5: layout("TX", hw.tx); break;
    case 6:
      ESP_LOGI(TAG, "dma: frames=%u descriptors=%u speaker_capacity=%u speaker_queued_bytes=%u idle_eof_drops=%u pending_tx=%u",
               hw.dma_frames, hw.dma_descriptors, (unsigned) this->get_speaker_buffer_size(),
               (unsigned) dump.queued_bytes, (unsigned) dump.idle_drops, (unsigned) dump.pending_tx);
      break;
    case 7:
      ESP_LOGI(TAG, "task: core=%d priority=%u stack_bytes=%u stack_min_free=%u buffers_psram=%s stack_psram=%s",
               this->task_core_, (unsigned) this->task_priority_, (unsigned) this->task_stack_size_,
               (unsigned) dump.stack_min_free, YESNO(this->buffers_in_psram_), YESNO(this->audio_task_stack_in_psram_));
      break;
    default:
      ESP_LOGI(TAG, "END v=1 uptime_ms=%u detailed_telemetry=not_available", (unsigned) dump.requested_ms);
      this->diagnostic_dump_.reset();
      return;
  }
  // The existing ESPHome scheduler runs each section on a later loop turn.
  // UART output can block; never emit the entire report from an API callback.
  this->defer("audio-diagnostics", [this]() { this->emit_diagnostic_section_(); });
}
}  // namespace esphome::esp_audio_stack
#endif
