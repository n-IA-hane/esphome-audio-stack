#include "esp_audio_capture.h"
#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_CAPTURE)
#include <arpa/inet.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <freertos/idf_additions.h>
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
static constexpr int64_t FINISH_GRACE_US = 1000000;

void ESPAudioCapture::setup() {
  this->socket_mutex_ = xSemaphoreCreateMutex();
  if (this->socket_mutex_ == nullptr) {
    this->mark_failed();
    return;
  }
  this->parent_->set_capture_observer(this);
#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
  if (!boundary_register_sink(this)) {
    ESP_LOGE(TAG, "Boundary probes require a single capture owner");
    this->mark_failed();
    return;
  }
#endif
  this->disable_loop();
}

void ESPAudioCapture::dump_config() {
  ESP_LOGCONFIG(TAG, "Optional PCM capture: %s:%u, %u ms, %u bytes", this->address_.c_str(), this->port_,
                static_cast<unsigned>(this->duration_ms_), static_cast<unsigned>(this->buffer_size_));
  ESP_LOGCONFIG(TAG, "  Task stack: 4096 bytes, PSRAM requested: %s", YESNO(this->task_stack_in_psram_));
#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
  ESP_LOGCONFIG(TAG, "  Boundary probes: ON (frontend consumption and delegated AFE feed; capture session only)");
#endif
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
#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
  this->boundary_session_.fetch_add(1, std::memory_order_release);
  this->boundary_drops_.store(0, std::memory_order_relaxed);
  this->boundary_queued_.store(0, std::memory_order_relaxed);
  this->boundary_sent_.store(0, std::memory_order_relaxed);
#endif
  this->bound_generation_.store(UINT32_MAX, std::memory_order_relaxed);
  this->stopping_.store(false, std::memory_order_release);
  this->connected_.store(false, std::memory_order_relaxed);
  this->worker_done_.store(false, std::memory_order_relaxed);
  this->deadline_us_ = esp_timer_get_time() + uint64_t(this->duration_ms_) * 1000;
  // Keep the existing temporary owner, priority and stack size. Explicit PSRAM
  // requests fail without an internal fallback that could consume audio DMA headroom.
  bool created = false;
  if (this->task_stack_in_psram_) {
#if configSUPPORT_STATIC_ALLOCATION && CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
    created = xTaskCreatePinnedToCoreWithCaps(&ESPAudioCapture::worker_entry_, "pcm_capture", 4096, this, 2,
                                             &this->worker_handle_, tskNO_AFFINITY,
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
#endif
  } else {
    created = xTaskCreate(&ESPAudioCapture::worker_entry_, "pcm_capture", 4096, this, 2,
                          &this->worker_handle_) == pdPASS;
  }
  if (!created) {
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
#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
    const auto h = boundary_health();
    ESP_LOGI(TAG, "Boundary coverage: session=%u queue_drop=%u queued=%u sent=%u interface_overflow=%u instance_overflow=%u unknown_instance=%u invalid_shape=%u foreign_frontend=%u (health counters cumulative since boot)",
             (unsigned) this->boundary_session_.load(), (unsigned) this->boundary_drops_.load(),
             (unsigned) this->boundary_queued_.load(), (unsigned) this->boundary_sent_.load(),
             (unsigned) h.interface_overflow, (unsigned) h.instance_overflow, (unsigned) h.unknown_instance,
             (unsigned) h.invalid_shape, (unsigned) h.foreign_frontend);
#endif
    // Worker has closed its fd, returned every item and stopped accessing the
    // queue. It parks until this main-loop owner deletes it, never frees itself.
    if (this->task_stack_in_psram_) {
#if configSUPPORT_STATIC_ALLOCATION && CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
      vTaskDeleteWithCaps(this->worker_handle_);
#endif
    } else {
      vTaskDelete(this->worker_handle_);
    }
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
  // Natural expiry closes admission. Only this owner may wake after writers
  // retire, including when the worker already closed admission itself.
  if (uint64_t(esp_timer_get_time()) >= this->deadline_us_) {
    if (this->active_.exchange(false, std::memory_order_seq_cst) ||
        this->writers_.load(std::memory_order_seq_cst) == 0) this->wake_worker_();
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
  if (point >= static_cast<uint8_t>(CapturePoint::COUNT) || frame.data == nullptr || !this->capture_active(frame.point)) return;
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
  auto generation = this->bound_generation_.load(std::memory_order_acquire);
#if defined(USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE) || defined(USE_ESP_AUDIO_BOUNDARY_PROBES)
  // Each point has one producer. The audio owner and GMF fetch can bind their
  // first points concurrently, so only one generation may win admission.
  if (generation == UINT32_MAX) {
    if (this->bound_generation_.compare_exchange_strong(generation, frame.generation, std::memory_order_acq_rel,
                                                       std::memory_order_acquire))
      generation = frame.generation;
  }
#else
  if (generation == UINT32_MAX) this->bound_generation_.store(frame.generation, std::memory_order_release);
#endif
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
      const bool contiguous = frame.stride == frame.channels && frame.slots[0] == 0 &&
                              (frame.channels == 1 || frame.slots[1] == 1);
      if (contiguous) {
        memcpy(out, in, bytes);
      } else {
        for (size_t i = 0; i < frame.frames; i++)
          for (uint8_t ch = 0; ch < frame.channels; ch++) {
            memcpy(out, in + (i * frame.stride + frame.slots[ch]) * sample_bytes, sample_bytes);
            out += sample_bytes;
          }
      }
      this->queue_->complete_item(item);
    }
    // The handle remains owned until the worker sees writers == 0.
    this->wake_worker_();
  }
  this->writers_.fetch_sub(1, std::memory_order_seq_cst);
  if (this->stopping_.load(std::memory_order_acquire)) this->enable_loop_soon_any_context();
}

#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
void ESPAudioCapture::on_boundary(const BoundaryRecord &record, const int16_t *pcm) {
  this->writers_.fetch_add(1, std::memory_order_seq_cst);
  if (this->boundary_session() != record.session || record.session == 0) {
    this->writers_.fetch_sub(1, std::memory_order_seq_cst);
    return;
  }
  const uint32_t current_generation = this->parent_->capture_generation();
  auto generation = this->bound_generation_.load(std::memory_order_acquire);
  if (generation == UINT32_MAX) {
    if (this->bound_generation_.compare_exchange_strong(generation, current_generation, std::memory_order_acq_rel,
                                                       std::memory_order_acquire)) generation = current_generation;
  }
  if (generation != current_generation || this->microphone_->get_mute_state()) {
    this->active_.store(false, std::memory_order_seq_cst);
    this->stopping_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
  } else {
    const size_t bytes = static_cast<size_t>(record.frames) * record.channels * sizeof(int16_t);
    void *item = nullptr;
    if ((record.point == BOUNDARY_FRONTEND || record.point == BOUNDARY_FEED) && record.channels > 0 &&
        record.channels <= 4 && bytes <= 16384 && (pcm != nullptr || bytes == 0))
      item = this->queue_->acquire_item(sizeof(CaptureRecord) + sizeof(BoundaryRecord) + bytes);
    if (item == nullptr) {
      this->boundary_drops_.fetch_add(1, std::memory_order_relaxed);
    } else {
      const CaptureRecord header{record.position, record.timestamp_us, record.sequence, generation, 16000,
                                 record.frames, this->boundary_drops_.load(std::memory_order_relaxed),
                                 record.point, record.channels, 16};
      auto *out = static_cast<uint8_t *>(item);
      memcpy(out, &header, sizeof(header));
      memcpy(out + sizeof(header), &record, sizeof(record));
      if (bytes != 0) memcpy(out + sizeof(header) + sizeof(record), pcm, bytes);
      this->queue_->complete_item(item);
      this->boundary_queued_.fetch_add(1, std::memory_order_relaxed);
    }
    this->wake_worker_();
  }
  this->writers_.fetch_sub(1, std::memory_order_seq_cst);
}

bool ESPAudioCapture::send_boundary_(const CaptureRecord &header, const void *body, size_t bytes) {
  if (bytes < sizeof(BoundaryRecord)) return false;
  BoundaryRecord record{};
  memcpy(&record, body, sizeof(record));
  const size_t pcm_bytes = bytes - sizeof(record);
  if (pcm_bytes != static_cast<size_t>(record.frames) * record.channels * sizeof(int16_t)) return false;
  char json[768];
  const int length = snprintf(json, sizeof(json),
      "{\"v\":1,\"boundary\":true,\"point\":%u,\"seq\":%lu,\"position\":%llu,\"timestamp_us\":%llu,"
      "\"generation\":%lu,\"rate\":16000,\"frames\":%lu,\"channels\":%u,\"bits\":16,\"bytes\":%u,"
      "\"dropped\":%lu,\"session\":%lu,\"instance\":%lu,\"duration_us\":%lu,\"offered_frames\":%lu,"
      "\"features\":%lu,\"result\":%ld,\"checksum\":%lu,\"checksum_before\":%lu,\"checksum_after\":%lu,\"flags\":%lu}\n",
      record.point, (unsigned long) record.sequence, (unsigned long long) record.position,
      (unsigned long long) record.timestamp_us, (unsigned long) header.generation, (unsigned long) record.frames,
      record.channels, (unsigned) pcm_bytes, (unsigned long) header.dropped, (unsigned long) record.session,
      (unsigned long) record.instance, (unsigned long) record.duration_us, (unsigned long) record.offered_frames,
      (unsigned long) record.features, (long) record.result, (unsigned long) record.checksum,
      (unsigned long) record.checksum_before, (unsigned long) record.checksum_after, (unsigned long) record.flags);
  const int64_t deadline = esp_timer_get_time() + 1000000;
  const bool sent = length > 0 && size_t(length) < sizeof(json) && this->send_all_(json, length, deadline) &&
                    this->send_all_(static_cast<const uint8_t *>(body) + sizeof(record), pcm_bytes, deadline);
  if (sent) this->boundary_sent_.fetch_add(1, std::memory_order_relaxed);
  return sent;
}
#endif

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
    const int64_t remaining = std::min<int64_t>(
        3000000, static_cast<int64_t>(this->deadline_us_) + FINISH_GRACE_US - esp_timer_get_time());
    if (remaining <= 0) return false;
    timeval timeout{static_cast<long>(remaining / 1000000), static_cast<long>(remaining % 1000000)};
    if (::select(fd + 1, nullptr, &wfds, nullptr, &timeout) <= 0) return false;
    int error = 0;
    socklen_t len = sizeof(error);
    if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &len) != 0 || error != 0) return false;
  }
  return !this->stopping_.load(std::memory_order_acquire);
}

bool ESPAudioCapture::send_all_(const void* data, size_t bytes, int64_t send_deadline) {
  // All records share one final budget, including their header and payload.
  send_deadline = std::min(send_deadline, static_cast<int64_t>(this->deadline_us_) + FINISH_GRACE_US);
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
    const int64_t finish_deadline = static_cast<int64_t>(this->deadline_us_) + FINISH_GRACE_US;
    while (!this->stopping_.load(std::memory_order_acquire)) {
      const int64_t now = esp_timer_get_time();
      if (now >= finish_deadline) break;
      const bool finishing = uint64_t(now) >= this->deadline_us_;
      if (finishing) {
        this->active_.store(false, std::memory_order_seq_cst);
        // A no-split reservation may not be visible to receive_item yet.
        // Wait for admitted writers before deciding that the queue is empty.
        if (this->writers_.load(std::memory_order_seq_cst) != 0) {
          this->enable_loop_soon_any_context();
          const int64_t remaining = finish_deadline - esp_timer_get_time();
          if (remaining <= 0) break;
          ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS((remaining + 999) / 1000));
          continue;
        }
      }
      size_t bytes = 0;
      void* item = this->queue_->receive_item(&bytes);
      if (item != nullptr) {
        // FreeRTOS guarantees four-byte item alignment, not native uint64_t
        // alignment on every architecture. Read the header through memcpy.
        CaptureRecord record{};
        bool ok = false;
        if (bytes >= sizeof(record)) {
          memcpy(&record, item, sizeof(record));
#ifdef USE_ESP_AUDIO_BOUNDARY_PROBES
          if (record.point == BOUNDARY_FRONTEND || record.point == BOUNDARY_FEED)
            ok = this->send_boundary_(record, static_cast<uint8_t *>(item) + sizeof(record), bytes - sizeof(record));
          else
#endif
          ok = this->send_record_(record, static_cast<uint8_t*>(item) + sizeof(record), bytes - sizeof(record));
        }
        this->queue_->return_item(item);
        if (!ok) break;
      } else {
        if (finishing) break;  // Admission is closed, writers retired, queue drained.
        const int64_t remaining = this->deadline_us_ - esp_timer_get_time();
        if (remaining <= 0) continue;  // Enter natural finish, do not discard a queued tail.
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
