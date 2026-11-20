#include "esp_audio_stack.h"

#ifdef USE_ESP32
#include <algorithm>
#include <driver/i2s_common.h>
#include <esp_idf_version.h>
#if CONFIG_IDF_TARGET_ESP32S3 && defined(USE_ESP_AUDIO_STACK_RX_DATA_DELAY)
#include <hal/i2s_ll.h>
#endif
#include "esphome/core/log.h"

namespace esphome::esp_audio_stack {

static const char *const TAG = "audio_stack";
constexpr uint32_t DMA_BUFFER_DURATION_MS = 10;

namespace {
uint32_t largest_divisor_at_most(uint32_t value, uint32_t limit, uint32_t minimum) {
  if (value == 0 || limit == 0)
    return 0;
  uint32_t candidate = std::min(value, limit);
  while (candidate > minimum) {
    if (value % candidate == 0)
      return candidate;
    candidate--;
  }
  return value % minimum == 0 && minimum <= limit ? minimum : std::min(value, limit);
}
gpio_num_t pin_or_nc(int pin) { return pin >= 0 ? static_cast<gpio_num_t>(pin) : GPIO_NUM_NC; }
}  // namespace

// Helper: get MCLK multiple enum from integer value
static i2s_mclk_multiple_t get_mclk_multiple(uint32_t mult) {
  switch (mult) {
    case 128:
      return I2S_MCLK_MULTIPLE_128;
    case 384:
      return I2S_MCLK_MULTIPLE_384;
    case 512:
      return I2S_MCLK_MULTIPLE_512;
    default:
      return I2S_MCLK_MULTIPLE_256;
  }
}

// Helper: get STD slot config for the configured comm format (0=philips, 1=msb, 2=pcm_short, 3=pcm_long)
// Note: PCM short/long are TDM-only in ESP-IDF; falls back to Philips in STD mode
static i2s_std_slot_config_t get_std_slot_config(uint8_t fmt, i2s_data_bit_width_t bw, i2s_slot_mode_t mode) {
  switch (fmt) {
    case 1:
      return I2S_STD_MSB_SLOT_DEFAULT_CONFIG(bw, mode);
    default:
      return I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(bw, mode);
  }
}

#if SOC_I2S_SUPPORTS_TDM && defined(USE_ESP_AUDIO_STACK_TDM_BUS)
// Helper: get TDM slot config for the configured comm format
static i2s_tdm_slot_config_t get_tdm_slot_config(uint8_t fmt, i2s_data_bit_width_t bw, i2s_slot_mode_t mode,
                                                 i2s_tdm_slot_mask_t mask) {
  switch (fmt) {
    case 1:
      return I2S_TDM_MSB_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    case 2:
      return I2S_TDM_PCM_SHORT_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    case 3:
      return I2S_TDM_PCM_LONG_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    default:
      return I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(bw, mode, mask);
  }
}
#endif  // SOC_I2S_SUPPORTS_TDM

bool ESPAudioStack::compute_dma_geometry_(I2SDmaGeometry &out) {
  uint32_t bytes_per_sample = (this->bits_per_sample_ > 16) ? 4 : 2;
  uint32_t tx_bytes_per_frame = this->num_channels_ * bytes_per_sample;
  uint32_t rx_bytes_per_frame = tx_bytes_per_frame;
  if (this->use_stereo_aec_ref_) {
    rx_bytes_per_frame = 2 * bytes_per_sample;
  }
#if SOC_I2S_SUPPORTS_TDM && defined(USE_ESP_AUDIO_STACK_TDM_BUS)
  if (this->use_tdm_bus_) {
    tx_bytes_per_frame = bytes_per_sample;
    rx_bytes_per_frame = this->make_tdm_rx_layout_().count() * bytes_per_sample;
  }
#endif
  const uint32_t max_bytes_per_frame = std::max(tx_bytes_per_frame, rx_bytes_per_frame);
  uint32_t dma_desc_num = this->dma_desc_num_;
  uint32_t dma_frame_num = this->dma_frame_num_configured_
                               ? this->dma_frame_num_
                               : std::max<uint32_t>(64, (this->sample_rate_ * DMA_BUFFER_DURATION_MS) / 1000);
  uint32_t max_frames = 0;
  if (max_bytes_per_frame > 0) {
    max_frames = 4092 / max_bytes_per_frame;
    if (dma_frame_num > max_frames) {
      if (this->dma_frame_num_configured_) {
        ESP_LOGE(TAG, "dma_frame_num %u exceeds IDF DMA descriptor limit (%u frames for %u bytes/frame)",
                 (unsigned) dma_frame_num, (unsigned) max_frames, (unsigned) max_bytes_per_frame);
        this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
        return false;
      }
      dma_frame_num = max_frames;
    }
  }
  uint32_t logical_tx_frames = 256U * this->rate_conversion_ratio_;
#ifdef USE_AUDIO_PROCESSOR
  if (this->processor_ != nullptr) {
    const auto spec = this->processor_->frame_spec();
    if (spec.input_samples > 0) {
      logical_tx_frames = static_cast<uint32_t>(spec.input_samples) * this->rate_conversion_ratio_;
    }
  }
#endif
  if (logical_tx_frames > 0 && max_frames > 0) {
    // Sparse DMA saves memory without changing the established automatic
    // descriptor cadence. Explicit geometry still uses the actual slot count.
    const uint32_t alignment_limit = this->use_tdm_bus_ && !this->dma_frame_num_configured_
                                         ? std::min(max_frames, 4092U / (this->tdm_total_slots_ * bytes_per_sample))
                                         : max_frames;
    const bool explicit_divisor = this->dma_frame_num_configured_ && logical_tx_frames % dma_frame_num == 0;
    const uint32_t divisor_limit = this->dma_frame_num_configured_ ? std::min(alignment_limit, dma_frame_num)
                                                                 : alignment_limit;
    const uint32_t aligned_frames = explicit_divisor ? dma_frame_num
                                                    : largest_divisor_at_most(logical_tx_frames, divisor_limit, 64);
    if (aligned_frames > 0 && aligned_frames != dma_frame_num) {
      if (this->dma_frame_num_configured_) {
        ESP_LOGW(TAG,
                 "Configured dma_frame_num=%u does not divide audio task TX frame (%u); "
                 "using %u so speaker playback callbacks stay in DMA lockstep",
                 (unsigned) dma_frame_num, (unsigned) logical_tx_frames, (unsigned) aligned_frames);
      } else {
        ESP_LOGD(TAG, "Aligning TX DMA to audio task frame: frames %u -> %u (logical=%u, max=%u)",
                 (unsigned) dma_frame_num, (unsigned) aligned_frames, (unsigned) logical_tx_frames,
                 (unsigned) max_frames);
      }
      dma_frame_num = aligned_frames;
    }
  }
#if SOC_I2S_SUPPORTS_TDM && defined(USE_ESP_AUDIO_STACK_TDM_BUS)
#ifdef USE_AUDIO_PROCESSOR
  if (this->use_tdm_bus_ && this->processor_ != nullptr && max_frames > 0) {
    const auto spec = this->processor_->frame_spec();
    const uint32_t processor_bus_frames = static_cast<uint32_t>(spec.input_samples) * this->rate_conversion_ratio_;
    if (processor_bus_frames > 0) {
      auto ceil_div_u32 = [](uint32_t num, uint32_t den) -> uint32_t { return den == 0 ? 0 : (num + den - 1) / den; };
      // The audio task reads/writes one processor frame per loop. Keep enough
      // DMA headroom for that full TDM frame plus margin; otherwise a 1024-sample
      // AFE quantum can underflow a short DMA queue even though the YAML compiles.
      // Do not change dma_frame_num here: it has already been selected as a
      // divisor of the logical TX frame so playback completion records stay in
      // lockstep with the I2S DMA callbacks.
      const uint32_t target_total_frames = ceil_div_u32(processor_bus_frames * 5U, 4U);
      if (target_total_frames > dma_desc_num * dma_frame_num) {
        const uint32_t old_desc = dma_desc_num;
        dma_desc_num = std::max(dma_desc_num, ceil_div_u32(target_total_frames, dma_frame_num));
        if (dma_desc_num > 16) {
          ESP_LOGE(TAG,
                   "TDM DMA queue too small for processor frame (%u bus frames, target %u); "
                   "need %u descriptors of %u frames, max supported is 16",
                   (unsigned) processor_bus_frames, (unsigned) target_total_frames, (unsigned) dma_desc_num,
                   (unsigned) dma_frame_num);
          this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
          return false;
        }
        ESP_LOGW(TAG,
                 "Raised TDM RX DMA queue for processor frame: desc %u->%u, frames %u "
                 "(processor=%u bus frames, target=%u)",
                 (unsigned) old_desc, (unsigned) dma_desc_num, (unsigned) dma_frame_num,
                 (unsigned) processor_bus_frames, (unsigned) target_total_frames);
      }
    }
  }
#endif
#endif
  out = {dma_desc_num, dma_frame_num, logical_tx_frames, tx_bytes_per_frame};
  return true;
}

bool ESPAudioStack::rollback_i2s_preparation_() {
  this->deinit_i2s_();
  this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
  return false;
}

bool ESPAudioStack::configure_std_channels_(i2s_data_bit_width_t bit_width, i2s_slot_bit_width_t slot_bw,
                                 i2s_clock_src_t clk_src, i2s_mclk_multiple_t mclk_mult) {
#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
  const bool dual_bus = this->dual_i2s_bus_;
#else
  constexpr bool dual_bus = false;
#endif
#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
  const int tx_mclk_pin = dual_bus ? this->tx_bus_.mclk_pin : this->mclk_pin_;
  const int tx_bclk_pin = dual_bus ? this->tx_bus_.bclk_pin : this->bclk_pin_;
  const int tx_lrclk_pin = dual_bus ? this->tx_bus_.lrclk_pin : this->lrclk_pin_;
  const int tx_dout_pin = dual_bus ? this->tx_bus_.dout_pin : this->dout_pin_;
  const int rx_mclk_pin = dual_bus ? this->rx_bus_.mclk_pin : this->mclk_pin_;
  const int rx_bclk_pin = dual_bus ? this->rx_bus_.bclk_pin : this->bclk_pin_;
  const int rx_lrclk_pin = dual_bus ? this->rx_bus_.lrclk_pin : this->lrclk_pin_;
  const int rx_din_pin = dual_bus ? this->rx_bus_.din_pin : this->din_pin_;
#else
  const int tx_mclk_pin = this->mclk_pin_;
  const int tx_bclk_pin = this->bclk_pin_;
  const int tx_lrclk_pin = this->lrclk_pin_;
  const int tx_dout_pin = this->dout_pin_;
  const int rx_mclk_pin = this->mclk_pin_;
  const int rx_bclk_pin = this->bclk_pin_;
  const int rx_lrclk_pin = this->lrclk_pin_;
  const int rx_din_pin = this->din_pin_;
#endif
  (void) rx_mclk_pin;
  (void) rx_bclk_pin;
  (void) rx_lrclk_pin;
  esp_err_t err = ESP_OK;
  i2s_slot_mode_t tx_slot_mode = (this->num_channels_ == 2) ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO;
  i2s_std_config_t tx_cfg = {
      .clk_cfg =
          {
              .sample_rate_hz = this->sample_rate_,
              .clk_src = clk_src,
              .mclk_multiple = mclk_mult,
          },
      .slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, tx_slot_mode),
      .gpio_cfg =
          {
              .mclk = pin_or_nc(tx_mclk_pin),
              .bclk = pin_or_nc(tx_bclk_pin),
              .ws = pin_or_nc(tx_lrclk_pin),
              .dout = pin_or_nc(tx_dout_pin),
              .din = dual_bus ? GPIO_NUM_NC : pin_or_nc(rx_din_pin),
              .invert_flags =
                  {
                      .mclk_inv = false,
                      .bclk_inv = false,
                      .ws_inv = false,
                  },
          },
  };
  tx_cfg.slot_cfg.slot_mask = (this->num_channels_ == 2)
                                  ? I2S_STD_SLOT_BOTH
                                  : (this->tx_slot_right_ ? I2S_STD_SLOT_RIGHT : I2S_STD_SLOT_LEFT);
  // Apply slot_bit_width override
  if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
    tx_cfg.slot_cfg.slot_bit_width = slot_bw;
  }

  // RX configuration - always independent of TX num_channels
  i2s_std_config_t rx_cfg = tx_cfg;
  if (dual_bus) {
    rx_cfg.gpio_cfg.mclk = pin_or_nc(rx_mclk_pin);
    rx_cfg.gpio_cfg.bclk = pin_or_nc(rx_bclk_pin);
    rx_cfg.gpio_cfg.ws = pin_or_nc(rx_lrclk_pin);
    rx_cfg.gpio_cfg.dout = GPIO_NUM_NC;
    rx_cfg.gpio_cfg.din = pin_or_nc(rx_din_pin);
  }
  if (this->use_stereo_aec_ref_ || this->rx_slot_mode_stereo_) {
    rx_cfg.slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_STEREO);
    rx_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
    ESP_LOGD(TAG, "RX configured as STEREO (%s)", this->use_stereo_aec_ref_ ? "AEC reference" : "mic channel select");
  } else {
    rx_cfg.slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_MONO);
    rx_cfg.slot_cfg.slot_mask = this->mic_channel_right_ ? I2S_STD_SLOT_RIGHT : I2S_STD_SLOT_LEFT;
  }
  // Apply slot_bit_width override to RX
  if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
    rx_cfg.slot_cfg.slot_bit_width = slot_bw;
  }

  if (this->tx_handle_) {
    err = i2s_channel_init_std_mode(this->tx_handle_, &tx_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init TX I2S channel: %s", esp_err_to_name(err));
      return this->rollback_i2s_preparation_();
    }
    ESP_LOGD(TAG, "TX channel initialized");
  }

  if (this->rx_handle_) {
    err = i2s_channel_init_std_mode(this->rx_handle_, &rx_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init RX I2S channel: %s", esp_err_to_name(err));
      return this->rollback_i2s_preparation_();
    }
#if CONFIG_IDF_TARGET_ESP32S3 && defined(USE_ESP_AUDIO_STACK_RX_DATA_DELAY)
    i2s_chan_info_t info{};
    if (i2s_channel_get_info(this->rx_handle_, &info) != ESP_OK || info.pair_chan != this->tx_handle_) {
      ESP_LOGE(TAG, "RX data delay requires a shared I2S channel pair");
      return this->rollback_i2s_preparation_();
    }
    // Channel allocation can reset the peripheral. RX start latches this timing setting.
    I2S_LL_GET_HW(info.id)->rx_timing.rx_sd_in_dm = USE_ESP_AUDIO_STACK_RX_DATA_DELAY;
#endif
    ESP_LOGD(TAG, "RX channel initialized (%s)", this->use_stereo_aec_ref_ ? "stereo" : "mono");
  }
  return true;
}

#if SOC_I2S_SUPPORTS_TDM && defined(USE_ESP_AUDIO_STACK_TDM_BUS)
bool ESPAudioStack::configure_tdm_channels_(i2s_data_bit_width_t bit_width, i2s_slot_bit_width_t slot_bw,
                                 i2s_clock_src_t clk_src, i2s_mclk_multiple_t mclk_mult) {
  esp_err_t err = ESP_OK;
  // Preserve the physical frame width while TX DMA carries only the
  // selected speaker slot. RX includes microphones, reference and diagnostics.
  const auto rx_layout = this->make_tdm_rx_layout_();
  const auto tdm_mask = static_cast<i2s_tdm_slot_mask_t>(rx_layout.mask);

  i2s_tdm_config_t tdm_cfg = {
      .clk_cfg =
          {
              .sample_rate_hz = this->sample_rate_,
              .clk_src = clk_src,
              .ext_clk_freq_hz = 0,
              .mclk_multiple = mclk_mult,
          },
      .slot_cfg = get_tdm_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_STEREO, tdm_mask),
      .gpio_cfg =
          {
              .mclk = pin_or_nc(this->mclk_pin_),
              .bclk = pin_or_nc(this->bclk_pin_),
              .ws = pin_or_nc(this->lrclk_pin_),
              .dout = pin_or_nc(this->dout_pin_),
              .din = pin_or_nc(this->din_pin_),
              .invert_flags =
                  {
                      .mclk_inv = false,
                      .bclk_inv = false,
                      .ws_inv = false,
                  },
          },
  };

  // Apply slot_bit_width override BEFORE init
  if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
    tdm_cfg.slot_cfg.slot_bit_width = slot_bw;
  }

  tdm_cfg.slot_cfg.total_slot = this->tdm_total_slots_;
  if (this->tx_handle_) {
    auto tx_cfg = tdm_cfg;
    tx_cfg.slot_cfg.slot_mask = static_cast<i2s_tdm_slot_mask_t>(1U << this->tdm_tx_slot_);
    err = i2s_channel_init_tdm_mode(this->tx_handle_, &tx_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init TX TDM channel: %s", esp_err_to_name(err));
      return this->rollback_i2s_preparation_();
    }
    ESP_LOGD(TAG, "TX TDM channel initialized");
  }
  if (this->rx_handle_) {
    err = i2s_channel_init_tdm_mode(this->rx_handle_, &tdm_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init RX TDM channel: %s", esp_err_to_name(err));
      return this->rollback_i2s_preparation_();
    }
    ESP_LOGD(TAG, "RX TDM channel initialized");
  }

  if (this->tdm_second_mic_slot_ >= 0) {
    ESP_LOGD(TAG, "TDM mode: %d slots, mic_slots=[%d,%d], ref_slot=%d, mask=0x%x", this->tdm_total_slots_,
             this->tdm_mic_slot_, this->tdm_second_mic_slot_, this->tdm_ref_slot_, (unsigned) tdm_mask);
  } else {
    ESP_LOGD(TAG, "TDM mode: %d slots, mic_slot=%d, ref_slot=%d, mask=0x%x", this->tdm_total_slots_,
             this->tdm_mic_slot_, this->tdm_ref_slot_, (unsigned) tdm_mask);
  }
  return true;
}
#endif

bool ESPAudioStack::prepare_i2s_channels_() {
  if (this->tx_handle_ != nullptr || this->rx_handle_ != nullptr) {
    return true;
  }

  ESP_LOGCONFIG(TAG, "Preparing I2S channels in full-duplex mode...");
  this->set_i2s_hardware_state_(I2SHardwareState::PREPARING);
  this->log_memory_snapshot_("before_i2s_prepare");

  // Map configured bit depth to I2S enum
  // Note: 24-bit data is stored in 32-bit DMA containers (MSB-aligned)
  i2s_data_bit_width_t bit_width;
  switch (this->bits_per_sample_) {
    case 32:
      bit_width = I2S_DATA_BIT_WIDTH_32BIT;
      break;
    case 24:
      bit_width = I2S_DATA_BIT_WIDTH_24BIT;
      break;
    default:
      bit_width = I2S_DATA_BIT_WIDTH_16BIT;
      break;
  }

  // Slot bit width: auto = match data bit width, or explicit override
  i2s_slot_bit_width_t slot_bw = I2S_SLOT_BIT_WIDTH_AUTO;
  if (this->slot_bit_width_ > 0) {
    switch (this->slot_bit_width_) {
      case 32:
        slot_bw = I2S_SLOT_BIT_WIDTH_32BIT;
        break;
      case 24:
        slot_bw = I2S_SLOT_BIT_WIDTH_24BIT;
        break;
      case 16:
        slot_bw = I2S_SLOT_BIT_WIDTH_16BIT;
        break;
      default:
        slot_bw = I2S_SLOT_BIT_WIDTH_AUTO;
        break;
    }
  }

#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
  const bool dual_bus = this->dual_i2s_bus_;
#else
  constexpr bool dual_bus = false;
#endif
  if (dual_bus && this->use_tdm_bus_) {
    ESP_LOGE(TAG, "dual I2S bus currently supports standard I2S only; TDM reference requires shared bus");
    this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
    return false;
  }

#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
  const uint8_t tx_i2s_num = dual_bus ? this->tx_bus_.i2s_num : this->i2s_num_;
  const uint8_t rx_i2s_num = dual_bus ? this->rx_bus_.i2s_num : this->i2s_num_;
#else
  const uint8_t tx_i2s_num = this->i2s_num_;
  const uint8_t rx_i2s_num = this->i2s_num_;
#endif
  const bool physical_tx = this->has_tx_output_();
  const bool need_rx = this->has_rx_input_();
  // In ESP-IDF full-duplex STD/TDM, TX is the clock owner for RX. Create an
  // explicit clock-only TX channel even for mic-only configurations so RX has
  // BCLK/WS and the bus can be fully deleted when the audio task parks.
  const bool need_tx = physical_tx || need_rx;

  if (!physical_tx && !need_rx) {
    ESP_LOGE(TAG, "At least one of din_pin or dout_pin must be configured");
    this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
    return false;
  }

  // Channel configuration
  // Clock source: APLL for accurate clocking (ESP32 original only)
  i2s_clock_src_t clk_src = I2S_CLK_SRC_DEFAULT;
#ifdef I2S_CLK_SRC_APLL
  if (this->use_apll_)
    clk_src = I2S_CLK_SRC_APLL;
#endif
  i2s_mclk_multiple_t mclk_mult = get_mclk_multiple(this->mclk_multiple_);

  ESP_LOGD(TAG, "I2S driver config: TX=%s(port %u, physical=%s) RX=%s(port %u)", need_tx ? "yes" : "no", tx_i2s_num,
           physical_tx ? "yes" : "clock-only", need_rx ? "yes" : "no", rx_i2s_num);

  I2SDmaGeometry geometry;
  if (!this->compute_dma_geometry_(geometry))
    return false;
  const auto &[dma_desc_num, dma_frame_num, logical_tx_frames, tx_bytes_per_frame] = geometry;

  i2s_chan_config_t chan_cfg =
      I2S_CHANNEL_DEFAULT_CONFIG(static_cast<decltype(i2s_chan_config_t{}.id)>(dual_bus ? tx_i2s_num : this->i2s_num_),
                                 this->i2s_mode_secondary_ ? static_cast<i2s_role_t>(1) : I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = dma_desc_num;
  chan_cfg.dma_frame_num = dma_frame_num;
  // on_sent is the single TX DMA refill owner. It copies already formatted
  // internal PCM or silence into the completed buffer for its next DMA cycle.
  chan_cfg.auto_clear = false;
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 2, 0)
  chan_cfg.auto_clear_before_cb = false;
  chan_cfg.intr_priority = 0;
#endif

  // RX retains its configured capture headroom. TX refills the just-finished
  // descriptor, so its automatic ring uses the public IDF minimum of two.
  const uint32_t tx_dma_desc_num = this->dma_desc_num_configured_ ? this->dma_desc_num_ : 2U;
  esp_err_t err = ESP_OK;
  if (need_tx) {
    chan_cfg.id = static_cast<decltype(chan_cfg.id)>(tx_i2s_num);
    chan_cfg.dma_desc_num = tx_dma_desc_num;
    err = i2s_new_channel(&chan_cfg, &this->tx_handle_, nullptr);
    if (err != ESP_OK) {
      this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
      return false;
    }
  }
  if (need_rx) {
    chan_cfg.id = static_cast<decltype(chan_cfg.id)>(rx_i2s_num);
    chan_cfg.dma_desc_num = dma_desc_num;
    err = i2s_new_channel(&chan_cfg, nullptr, &this->rx_handle_);
    if (err != ESP_OK) {
      return this->rollback_i2s_preparation_();
    }
#ifndef USE_ESP_AUDIO_STACK_HARDWARE_CODEC
    this->rx_dma_descriptor_count_ = dma_desc_num;
#endif
  }

  const bool configured =
#if SOC_I2S_SUPPORTS_TDM && defined(USE_ESP_AUDIO_STACK_TDM_BUS)
      this->use_tdm_bus_ ? this->configure_tdm_channels_(bit_width, slot_bw, clk_src, mclk_mult) :
#endif
      this->configure_std_channels_(bit_width, slot_bw, clk_src, mclk_mult);
  if (!configured)
    return false;

  this->set_i2s_hardware_state_(I2SHardwareState::READY);
  this->tx_completion_dma_frames_ = need_tx ? dma_frame_num : 0;
  this->tx_completion_dma_buffer_bytes_ =
      need_tx ? static_cast<size_t>(dma_frame_num) * static_cast<size_t>(tx_bytes_per_frame) : 0;
  this->tx_dma_descriptor_count_ = need_tx ? tx_dma_desc_num : 0;
  this->tx_logical_frame_size_ = logical_tx_frames;
  if (this->tx_handle_ != nullptr) {
    // IDF can round descriptors to cache-line boundaries. Playback completion
    // must count the frames actually allocated, not the requested geometry.
    i2s_chan_info_t info{};
    if (i2s_channel_get_info(this->tx_handle_, &info) != ESP_OK || info.total_dma_buf_size == 0 ||
        info.total_dma_buf_size % tx_dma_desc_num != 0 || tx_bytes_per_frame == 0 ||
        (info.total_dma_buf_size / tx_dma_desc_num) % tx_bytes_per_frame != 0) {
      ESP_LOGE(TAG, "Cannot determine actual I2S TX DMA frame geometry");
      this->deinit_i2s_();
      return false;
    }
    this->tx_completion_dma_buffer_bytes_ = info.total_dma_buf_size / tx_dma_desc_num;
    this->tx_completion_dma_frames_ = this->tx_completion_dma_buffer_bytes_ / tx_bytes_per_frame;
    if (logical_tx_frames % this->tx_completion_dma_frames_ != 0) {
      ESP_LOGE(TAG, "Actual I2S DMA block (%u frames) does not divide the audio frame (%u)",
               (unsigned) this->tx_completion_dma_frames_, (unsigned) logical_tx_frames);
      this->deinit_i2s_();
      return false;
    }
  }
#ifdef USE_ESP_AUDIO_STACK_HARDWARE_CODEC
  if (!this->setup_codec_backend_(clk_src)) {
    ESP_LOGE(TAG, "Failed to prepare esp_codec_dev backend");
    return this->rollback_i2s_preparation_();
  }
#endif
  this->publish_diagnostic_hardware_();
  this->log_memory_snapshot_("after_i2s_prepare");
  ESP_LOGI(TAG, "ESP audio stack I2S prepared through esp_driver_i2s (%s, dma_desc=%u, dma_frames=%u)",
           this->use_tdm_bus_ ? "TDM" : "standard", static_cast<unsigned>(dma_desc_num),
           static_cast<unsigned>(dma_frame_num));
  return true;
}

void ESPAudioStack::deinit_i2s_() {
  this->tx_completion_tracking_active_.store(false, std::memory_order_release);
  this->close_audio_io_();
#ifdef USE_ESP_AUDIO_STACK_HARDWARE_CODEC
  this->codec_backend_.teardown();
#endif
  // An open failure can leave a channel running before our RUNNING state is
  // committed. Query the driver, which owns the actual channel state, and keep
  // the handle if release fails so it cannot become an unreachable allocation.
  auto release_channel = [](i2s_chan_handle_t &handle) -> bool {
    if (handle == nullptr) return true;
    i2s_chan_info_t info{};
    esp_err_t err = i2s_channel_get_info(handle, &info);
    if (err == ESP_OK && info.is_enabled) err = i2s_channel_disable(handle);
    if (err == ESP_OK) err = i2s_del_channel(handle);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to release I2S channel: %s", esp_err_to_name(err));
      return false;
    }
    handle = nullptr;
    return true;
  };
  const bool tx_released = release_channel(this->tx_handle_);
  const bool rx_released = release_channel(this->rx_handle_);
  if (!tx_released || !rx_released) {
    this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
    return;
  }
  this->reset_tx_completion_tracking_();
  this->set_i2s_hardware_state_(I2SHardwareState::UNPREPARED);
  ESP_LOGI(TAG, "I2S deinitialized");
}

}  // namespace esphome::esp_audio_stack
#endif  // USE_ESP32
