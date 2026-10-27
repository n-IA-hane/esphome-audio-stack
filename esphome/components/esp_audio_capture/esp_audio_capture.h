#pragma once
#include "esphome/core/defines.h"
#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_CAPTURE)
#include <array>
#include <atomic>
#include <string>

#include "esphome/components/esp_audio_stack/esp_audio_stack.h"
#include "esphome/components/esp_audio_stack/microphone/esp_audio_stack_microphone.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"

namespace esphome::esp_audio_capture {
using esp_audio_stack::CaptureFrame;
using esp_audio_stack::CapturePoint;

struct CaptureRecord {
  uint64_t position;
  uint64_t timestamp_us;
  uint32_t sequence;
  uint32_t generation;
  uint32_t sample_rate;
  uint32_t frames;
  uint32_t dropped;
  uint8_t point;
  uint8_t channels;
  uint8_t bits;
};

class ESPAudioCapture final : public Component, public esp_audio_stack::AudioCaptureObserver {
 public:
  void set_parent(esp_audio_stack::ESPAudioStack* parent) { this->parent_ = parent; }
  void set_microphone(esp_audio_stack::ESPAudioStackMicrophone* microphone) { this->microphone_ = microphone; }
  void set_address(const std::string& address) { this->address_ = address; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_points(uint8_t points) { this->points_ = points; }
  void set_buffer_size(size_t bytes) { this->buffer_size_ = bytes; }
  void set_duration(uint32_t ms) { this->duration_ms_ = ms; }
  void setup() override;
  void loop() override;
  void on_shutdown() override { this->stop(); }
  void dump_config() override;
  bool start();
  void stop();
  bool capture_active(CapturePoint point) const override {
    return this->active_.load(std::memory_order_acquire) && (this->points_ & (1U << static_cast<uint8_t>(point)));
  }
  void on_capture_frame(const CaptureFrame& frame) override;

 protected:
  static void worker_entry_(void* self);
  void worker_();
  bool connect_();
  bool send_all_(const void* data, size_t bytes, int64_t send_deadline);
  void close_socket_();
  void wake_worker_();
  bool send_record_(const CaptureRecord& record, const void* pcm, size_t bytes);

  esp_audio_stack::ESPAudioStack* parent_{nullptr};
  esp_audio_stack::ESPAudioStackMicrophone* microphone_{nullptr};
  std::string address_;
  uint16_t port_{19091};
  uint8_t points_{0};
  size_t buffer_size_{65536};
  uint32_t duration_ms_{15000};
  uint64_t deadline_us_{0};
  std::atomic<bool> active_{false}, stopping_{false}, connected_{false}, worker_done_{false};
  std::atomic<uint32_t> writers_{0};
  std::atomic<uint32_t> bound_generation_{UINT32_MAX};
  std::array<uint64_t, 5> positions_{};
  std::array<uint32_t, 5> sequences_{}, drops_{}, rates_{};
  std::array<uint8_t, 5> widths_{}, channels_{};
  esp_audio_stack::RingBufferPtr queue_;
  TaskHandle_t worker_handle_{nullptr};
  SemaphoreHandle_t socket_mutex_{nullptr};
  int socket_fd_{-1};
  bool microphone_held_{false};
};

template <typename... Ts>
class CaptureStartAction : public Action<Ts...> {
 public:
  explicit CaptureStartAction(ESPAudioCapture* parent) : parent_(parent) {}
  void play(const Ts&... args) override { this->parent_->start(); }

 protected:
  ESPAudioCapture* parent_;
};
template <typename... Ts>
class CaptureStopAction : public Action<Ts...> {
 public:
  explicit CaptureStopAction(ESPAudioCapture* parent) : parent_(parent) {}
  void play(const Ts&... args) override { this->parent_->stop(); }

 protected:
  ESPAudioCapture* parent_;
};
}  // namespace esphome::esp_audio_capture
#endif
