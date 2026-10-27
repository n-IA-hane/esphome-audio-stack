#include "esp_audio_capture.h"
#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_CAPTURE)
#include <arpa/inet.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>

#include "esphome/core/log.h"

namespace esphome::esp_audio_capture {
static const char* const TAG = "audio_capture";

void ESPAudioCapture::setup() {
  this->socket_mutex_ = xSemaphoreCreateMutex();
  if (this->socket_mutex_ == nullptr) {
    this->mark_failed();
    return;
  }
  this->parent_->set_capture_observer(this);
  this->disable_loop();
}

void ESPAudioCapture::dump_config() {
  ESP_LOGCONFIG(TAG, "Optional PCM capture: %s:%u, %u ms, %u bytes", this->address_.c_str(), this->port_,
                static_cast<unsigned>(this->duration_ms_), static_cast<unsigned>(this->buffer_size_));
}

bool ESPAudioCapture::start() {
  if (this->is_failed() || this->worker_handle_ != nullptr || this->microphone_->get_mute_state()) return false;
  this->queue_ = esp_audio_stack::create_nosplit_prefer_psram(this->buffer_size_, "pcm_capture");
  if (!this->queue_) {
    this->status_momentary_warning("capture_memory", 5000);
    return false;
  }
  this->positions_.fill(0);
  this->sequences_.fill(0);
  this->drops_.fill(0);
  this->rates_.fill(0);
  this->widths_.fill(0);
  this->channels_.fill(0);
  this->bound_generation_.store(UINT32_MAX, std::memory_order_relaxed);
  this->stopping_.store(false, std::memory_order_release);
  this->connected_.store(false, std::memory_order_relaxed);
  this->worker_done_.store(false, std::memory_order_relaxed);
  this->deadline_us_ = esp_timer_get_time() + uint64_t(this->duration_ms_) * 1000;
  // Temporary low-priority task, ordinary ESP-IDF ownership of stack and TCB.
  if (xTaskCreate(&ESPAudioCapture::worker_entry_, "pcm_capture", 4096, this, 2, &this->worker_handle_) != pdPASS) {
    this->worker_handle_ = nullptr;
    this->queue_.reset();
    this->status_momentary_warning("capture_task", 5000);
    return false;
  }
  this->enable_loop();
  return true;
}

void ESPAudioCapture::wake_worker_() {
  if (this->worker_handle_ != nullptr) xTaskNotifyGive(this->worker_handle_);
}

void ESPAudioCapture::stop() {
  this->active_.store(false, std::memory_order_seq_cst);
  this->stopping_.store(true, std::memory_order_release);
  if (this->microphone_held_) {
    this->parent_->unregister_mic_consumer(this);
    this->microphone_held_ = false;
  }
  if (this->worker_handle_ == nullptr) return;
  xSemaphoreTake(this->socket_mutex_, portMAX_DELAY);
  // The worker owns close(). Holding this mutex prevents shutdown of a reused fd.
  if (this->socket_fd_ >= 0) ::shutdown(this->socket_fd_, SHUT_RDWR);
  xSemaphoreGive(this->socket_mutex_);
  this->wake_worker_();
}

void ESPAudioCapture::loop() {
  if (this->worker_handle_ == nullptr) {
    this->disable_loop();
    return;
  }
  if (this->worker_done_.load(std::memory_order_acquire)) {
    this->stop();
    // Worker has closed its fd, returned every item and stopped accessing the
    // queue. It parks until this main-loop owner deletes it, never frees itself.
    vTaskDelete(this->worker_handle_);
    this->worker_handle_ = nullptr;
    this->queue_.reset();
    this->disable_loop();
    return;
  }
  const uint32_t generation = this->bound_generation_.load(std::memory_order_acquire);
  if (this->microphone_->get_mute_state() || this->parent_->has_failed_session() ||
      (generation != UINT32_MAX && generation != this->parent_->capture_generation())) {
    this->stop();
  }
  if (this->stopping_.load(std::memory_order_acquire)) {
    if (this->writers_.load(std::memory_order_seq_cst) == 0) this->wake_worker_();
    return;
  }
  // Duration ends admission, while the worker completes its current record.
  if (uint64_t(esp_timer_get_time()) >= this->deadline_us_) {
    if (this->active_.exchange(false, std::memory_order_seq_cst)) this->wake_worker_();
    return;
  }
  if (this->connected_.load(std::memory_order_acquire) && !this->microphone_held_) {
    // The facade's void start() cannot acknowledge admission at its listener
    // limit. Own an explicit token on the existing audio-owner registry instead.
    if (!this->parent_->register_mic_consumer(this)) {
      this->status_momentary_warning("capture_consumer", 5000);
      this->stop();
      return;
    }
    this->microphone_held_ = true;
    this->active_.store(true, std::memory_order_seq_cst);
  }
}

void ESPAudioCapture::on_capture_frame(const CaptureFrame& frame) {
  const auto point = static_cast<uint8_t>(frame.point);
  if (point >= 5 || frame.data == nullptr || !this->capture_active(frame.point)) return;
  if (this->microphone_->get_mute_state()) {
    this->active_.store(false, std::memory_order_seq_cst);
    this->stopping_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
    return;
  }
  // Counter and admission flag use one total order for safe reclamation on
  // weakly ordered targets; no lock or wait is added to the audio callback.
  this->writers_.fetch_add(1, std::memory_order_seq_cst);
  // A call already in flight when stop() disabled the observer must not use
  // the queue. Such calls do not touch the worker handle either.
  if (!this->active_.load(std::memory_order_seq_cst)) {
    this->writers_.fetch_sub(1, std::memory_order_seq_cst);
    return;
  }
  const auto generation = this->bound_generation_.load(std::memory_order_acquire);
  if (generation == UINT32_MAX) this->bound_generation_.store(frame.generation, std::memory_order_release);
  const bool format_changed =
      this->rates_[point] != 0 && (this->rates_[point] != frame.sample_rate || this->widths_[point] != frame.bits ||
                                   this->channels_[point] != frame.channels);
  if ((generation != UINT32_MAX && generation != frame.generation) || format_changed) {
    this->active_.store(false, std::memory_order_seq_cst);
    this->stopping_.store(true, std::memory_order_release);
  } else {
    this->rates_[point] = frame.sample_rate;
    this->widths_[point] = frame.bits;
    this->channels_[point] = frame.channels;
    const uint32_t sequence = this->sequences_[point]++;
    const uint64_t position = this->positions_[point];
    this->positions_[point] += frame.frames;
    const size_t sample_bytes = frame.bits / 8;
    const size_t bytes = frame.frames * frame.channels * sample_bytes;
    void* item = nullptr;
    if (bytes > 0 && bytes <= 16384 && (sample_bytes == 2 || sample_bytes == 4) && frame.channels > 0 &&
        frame.channels <= 2 && frame.slots[0] < frame.stride && (frame.channels == 1 || frame.slots[1] < frame.stride))
      item = this->queue_->acquire_item(sizeof(CaptureRecord) + bytes);
    if (item == nullptr)
      ++this->drops_[point];
    else {
      const CaptureRecord record{position,
                                 frame.timestamp_us,
                                 sequence,
                                 frame.generation,
                                 frame.sample_rate,
                                 static_cast<uint32_t>(frame.frames),
                                 this->drops_[point],
                                 point,
                                 frame.channels,
                                 frame.bits};
      memcpy(item, &record, sizeof(record));
      auto* out = static_cast<uint8_t*>(item) + sizeof(record);
      const auto* in = static_cast<const uint8_t*>(frame.data);
      for (size_t i = 0; i < frame.frames; i++)
        for (uint8_t ch = 0; ch < frame.channels; ch++) {
          memcpy(out, in + (i * frame.stride + frame.slots[ch]) * sample_bytes, sample_bytes);
          out += sample_bytes;
        }
      this->queue_->complete_item(item);
    }
    // The handle remains owned until the worker sees writers == 0.
    this->wake_worker_();
  }
  this->writers_.fetch_sub(1, std::memory_order_seq_cst);
  if (this->stopping_.load(std::memory_order_acquire)) this->enable_loop_soon_any_context();
}

bool ESPAudioCapture::connect_() {
  sockaddr_in peer{};
  peer.sin_family = AF_INET;
  peer.sin_port = htons(this->port_);
  if (::inet_pton(AF_INET, this->address_.c_str(), &peer.sin_addr) != 1) return false;
  const int fd = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (fd < 0) return false;
  xSemaphoreTake(this->socket_mutex_, portMAX_DELAY);
  this->socket_fd_ = fd;
  xSemaphoreGive(this->socket_mutex_);
  ::fcntl(fd, F_SETFL, O_NONBLOCK);
  int result = ::connect(fd, reinterpret_cast<sockaddr*>(&peer), sizeof(peer));
  if (result < 0 && errno != EINPROGRESS) return false;
  if (result < 0) {
    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(fd, &wfds);
    timeval timeout{3, 0};
    if (::select(fd + 1, nullptr, &wfds, nullptr, &timeout) <= 0) return false;
    int error = 0;
    socklen_t len = sizeof(error);
    if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &len) != 0 || error != 0) return false;
  }
  return !this->stopping_.load(std::memory_order_acquire);
}

bool ESPAudioCapture::send_all_(const void* data, size_t bytes, int64_t send_deadline) {
  const auto* cursor = static_cast<const uint8_t*>(data);
  while (bytes > 0 && !this->stopping_.load(std::memory_order_acquire)) {
    if (esp_timer_get_time() >= send_deadline) return false;
    const ssize_t count = ::send(this->socket_fd_, cursor, bytes, 0);
    if (count > 0) {
      cursor += count;
      bytes -= count;
      continue;
    }
    if (count < 0 && errno == EINTR) continue;
    if (count >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) return false;
    const int64_t remaining = send_deadline - esp_timer_get_time();
    if (remaining <= 0) return false;
    timeval timeout{static_cast<long>(remaining / 1000000), static_cast<long>(remaining % 1000000)};
    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(this->socket_fd_, &wfds);
    if (::select(this->socket_fd_ + 1, nullptr, &wfds, nullptr, &timeout) <= 0) return false;
  }
  return bytes == 0;
}

bool ESPAudioCapture::send_record_(const CaptureRecord& record, const void* pcm, size_t bytes) {
  char header[320];
  const int len = snprintf(
      header, sizeof(header),
      "{\"v\":1,\"point\":%u,\"seq\":%lu,\"position\":%llu,\"timestamp_us\":%llu,\"generation\":%lu,"
      "\"rate\":%lu,\"frames\":%lu,\"channels\":%u,\"bits\":%u,\"dropped\":%lu,\"bytes\":%u}\n",
      record.point, static_cast<unsigned long>(record.sequence), static_cast<unsigned long long>(record.position),
      static_cast<unsigned long long>(record.timestamp_us), static_cast<unsigned long>(record.generation),
      static_cast<unsigned long>(record.sample_rate), static_cast<unsigned long>(record.frames), record.channels,
      record.bits, static_cast<unsigned long>(record.dropped), static_cast<unsigned>(bytes));
  // Header and PCM share one finite transport budget, independent of capture duration.
  const int64_t send_deadline = esp_timer_get_time() + 1000000;
  return len > 0 && size_t(len) < sizeof(header) && this->send_all_(header, len, send_deadline) &&
         this->send_all_(pcm, bytes, send_deadline);
}

void ESPAudioCapture::close_socket_() {
  xSemaphoreTake(this->socket_mutex_, portMAX_DELAY);
  if (this->socket_fd_ >= 0) {
    ::close(this->socket_fd_);
    this->socket_fd_ = -1;
  }
  xSemaphoreGive(this->socket_mutex_);
}

void ESPAudioCapture::worker_entry_(void* self) { static_cast<ESPAudioCapture*>(self)->worker_(); }

void ESPAudioCapture::worker_() {
  if (this->connect_()) {
    this->connected_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
    while (!this->stopping_.load(std::memory_order_acquire) && uint64_t(esp_timer_get_time()) < this->deadline_us_) {
      size_t bytes = 0;
      void* item = this->queue_->receive_item(&bytes);
      if (item != nullptr) {
        // FreeRTOS guarantees four-byte item alignment, not native uint64_t
        // alignment on every architecture. Read the header through memcpy.
        CaptureRecord record{};
        bool ok = false;
        if (bytes >= sizeof(record)) {
          memcpy(&record, item, sizeof(record));
          ok = this->send_record_(record, static_cast<uint8_t*>(item) + sizeof(record), bytes - sizeof(record));
        }
        this->queue_->return_item(item);
        if (!ok) break;
      } else {
        const int64_t remaining = this->deadline_us_ - esp_timer_get_time();
        if (remaining <= 0) break;
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS((remaining + 999) / 1000));
      }
    }
  }
  this->active_.store(false, std::memory_order_seq_cst);
  this->stopping_.store(true, std::memory_order_release);
  this->close_socket_();
  this->enable_loop_soon_any_context();
  while (this->writers_.load(std::memory_order_seq_cst) != 0) ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
  this->queue_->reset();
  this->worker_done_.store(true, std::memory_order_release);
  // Main owns deletion. Parking avoids a stale task handle between last producer
  // notification and RTOS idle cleanup of a self-deleted task.
  vTaskSuspend(nullptr);
}
}  // namespace esphome::esp_audio_capture
#endif
