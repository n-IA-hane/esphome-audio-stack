#include "boundary_probe.h"
#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_BOUNDARY_PROBES)
#include <atomic>
#include <cstring>
#include <esp_afe_sr_models.h>
#include <esp_timer.h>
#include <frontend.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

extern "C" const esp_afe_sr_iface_t *__real_esp_afe_handle_from_config(const afe_config_t *config);
extern "C" FrontendOutput __real_FrontendProcessSamples(FrontendState *state, const int16_t *samples,
                                                       size_t count, size_t *consumed);

namespace esphome::esp_audio_capture {
namespace {
constexpr size_t INTERFACES = 4, INSTANCES = 8;
constexpr int MAX_FEED_FRAMES = 2048, MAX_FEED_CHANNELS = 4;
struct InterfaceSlot { const esp_afe_sr_iface_t *original{nullptr}; esp_afe_sr_iface_t copy{}; };
struct InstanceSlot {
  esp_afe_sr_data_t *data{nullptr};
  uint32_t id{0};
  size_t interface_index{0};
  int frames{0}, channels{0};
};
InterfaceSlot interfaces[INTERFACES];
InstanceSlot instances[INSTANCES];
portMUX_TYPE registry_mux = portMUX_INITIALIZER_UNLOCKED;
std::atomic<BoundarySink *> sink{nullptr};
BoundaryHealth health{};
uint32_t next_instance = 0;
struct Stream {
  uint32_t session{0}, sequence{0};
  uint64_t position{0};
};
Stream streams[2];
FrontendState *frontend_state{nullptr};
TaskHandle_t frontend_task{nullptr};
uint32_t frontend_session{0};

uint32_t checksum(const int16_t *pcm, size_t samples) {
  uint32_t value = 2166136261U;
  // Explicit little-endian sample hashing is independent of host byte order.
  for (size_t i = 0; i < samples; ++i) {
    const uint16_t sample = static_cast<uint16_t>(pcm[i]);
    value = (value ^ (sample & 255U)) * 16777619U;
    value = (value ^ (sample >> 8)) * 16777619U;
  }
  return value;
}
BoundarySink *active_sink(uint32_t &session) {
  auto *target = sink.load(std::memory_order_acquire);
  session = target == nullptr ? 0 : target->boundary_session();
  return session == 0 ? nullptr : target;
}
void publish(BoundarySink *target, BoundaryRecord &record, const int16_t *pcm) {
  if (target == nullptr || target->boundary_session() != record.session) return;
  portENTER_CRITICAL(&registry_mux);
  if (target->boundary_session() != record.session) { portEXIT_CRITICAL(&registry_mux); return; }
  auto &stream = streams[record.point == BOUNDARY_FRONTEND ? 0 : 1];
  if (stream.session != record.session) stream = {record.session, 0, 0};
  record.sequence = stream.sequence++;
  record.position = stream.position;
  stream.position += record.frames;
  portEXIT_CRITICAL(&registry_mux);
  target->on_boundary(record, pcm);
}
template<size_t Index> esp_afe_sr_data_t *create(afe_config_t *config) {
  // Interface entries are immutable once published. No vendor work under mux.
  const auto *original = interfaces[Index].original;
  auto *data = original->create_from_config(config);
  if (data == nullptr) return nullptr;
  const int frames = original->get_feed_chunksize(data);
  const int channels = original->get_feed_channel_num(data);
  portENTER_CRITICAL(&registry_mux);
  bool stored = false;
  for (auto &entry : instances) {
    if (entry.data == nullptr) {
      entry = {data, ++next_instance, Index, frames, channels};
      stored = true;
      break;
    }
  }
  if (!stored) ++health.instance_overflow;
  if (frames <= 0 || frames > MAX_FEED_FRAMES || channels <= 0 || channels > MAX_FEED_CHANNELS)
    ++health.invalid_shape;
  portEXIT_CRITICAL(&registry_mux);
  return data;
}
template<size_t Index> int feed(esp_afe_sr_data_t *data, const int16_t *pcm) {
  const auto *original = interfaces[Index].original;
  uint32_t session = 0;
  auto *target = active_sink(session);
  if (target == nullptr) return original->feed(data, pcm);
  InstanceSlot entry{};
  portENTER_CRITICAL(&registry_mux);
  for (const auto &candidate : instances) {
    if (candidate.data == data && candidate.interface_index == Index) { entry = candidate; break; }
  }
  if (entry.data == nullptr) ++health.unknown_instance;
  portEXIT_CRITICAL(&registry_mux);
  if (entry.data == nullptr || pcm == nullptr || entry.frames <= 0 || entry.frames > MAX_FEED_FRAMES ||
      entry.channels <= 0 || entry.channels > MAX_FEED_CHANNELS)
    return original->feed(data, pcm);
  BoundaryRecord record{};
  record.point = BOUNDARY_FEED;
  record.session = session;
  record.instance = entry.id;
  record.channels = entry.channels;
  record.frames = record.offered_frames = entry.frames;
  const size_t samples = static_cast<size_t>(entry.frames) * entry.channels;
  record.checksum_before = checksum(pcm, samples);
  record.timestamp_us = esp_timer_get_time();
  record.result = original->feed(data, pcm);
  record.duration_us = static_cast<uint32_t>(esp_timer_get_time() - record.timestamp_us);
  record.checksum_after = checksum(pcm, samples);
  record.checksum = record.checksum_after;
  if (record.checksum_before != record.checksum_after) record.flags |= 1;
  publish(target, record, pcm);
  return record.result;
}
template<size_t Index> void destroy(esp_afe_sr_data_t *data) {
  // GMF owns quiescence: it joins its workers before calling the destructor.
  // Retire metadata before deallocation permits another create() to reuse the
  // address. The original function remains in the immutable interface slot.
  portENTER_CRITICAL(&registry_mux);
  for (auto &entry : instances) {
    if (entry.data == data && entry.interface_index == Index) { entry = {}; break; }
  }
  portEXIT_CRITICAL(&registry_mux);
  interfaces[Index].original->destroy(data);
}
template<size_t Index> void install(InterfaceSlot &slot, const esp_afe_sr_iface_t *original) {
  slot.original = original;
  slot.copy = *original;
  slot.copy.create_from_config = &create<Index>;
  slot.copy.feed = &feed<Index>;
  slot.copy.destroy = &destroy<Index>;
}
}  // namespace

bool boundary_register_sink(BoundarySink *target) {
  BoundarySink *expected = nullptr;
  return sink.compare_exchange_strong(expected, target, std::memory_order_release, std::memory_order_relaxed) ||
         expected == target;
}
BoundaryHealth boundary_health() {
  portENTER_CRITICAL(&registry_mux);
  const auto result = health;
  portEXIT_CRITICAL(&registry_mux);
  return result;
}
const esp_afe_sr_iface_t *observe_interface(const esp_afe_sr_iface_t *original) {
  if (original == nullptr) return nullptr;
  if (original->create_from_config == nullptr || original->feed == nullptr || original->destroy == nullptr ||
      original->get_feed_chunksize == nullptr || original->get_feed_channel_num == nullptr) {
    portENTER_CRITICAL(&registry_mux);
    ++health.invalid_shape;
    portEXIT_CRITICAL(&registry_mux);
    return original;
  }
  const esp_afe_sr_iface_t *result = original;
  portENTER_CRITICAL(&registry_mux);
  size_t index = 0;
  for (; index < INTERFACES; ++index) {
    if (interfaces[index].original == original) { result = &interfaces[index].copy; break; }
    if (interfaces[index].original == nullptr) {
      switch (index) {
        case 0: install<0>(interfaces[index], original); break;
        case 1: install<1>(interfaces[index], original); break;
        case 2: install<2>(interfaces[index], original); break;
        case 3: install<3>(interfaces[index], original); break;
      }
      result = &interfaces[index].copy;
      break;
    }
  }
  if (index == INTERFACES) ++health.interface_overflow;
  portEXIT_CRITICAL(&registry_mux);
  return result;
}
FrontendOutput observe_frontend(FrontendState *state, const int16_t *pcm, size_t count, size_t *consumed) {
  uint32_t session = 0;
  auto *target = active_sink(session);
  if (target == nullptr || pcm == nullptr || consumed == nullptr)
    return __real_FrontendProcessSamples(state, pcm, count, consumed);
  const auto task = xTaskGetCurrentTaskHandle();
  const bool mww_task = std::strcmp(pcTaskGetName(task), "mww") == 0;
  portENTER_CRITICAL(&registry_mux);
  if (target->boundary_session() != session) {
    portEXIT_CRITICAL(&registry_mux);
    return __real_FrontendProcessSamples(state, pcm, count, consumed);
  }
  if (frontend_session != session) {
    frontend_session = session;
    frontend_state = nullptr;
    frontend_task = nullptr;
  }
  if (frontend_state == nullptr && mww_task) { frontend_state = state; frontend_task = task; }
  const bool bound = mww_task && frontend_state == state && frontend_task == task;
  if (!bound) ++health.foreign_frontend;
  portEXIT_CRITICAL(&registry_mux);
  if (!bound) return __real_FrontendProcessSamples(state, pcm, count, consumed);
  BoundaryRecord record{};
  record.point = BOUNDARY_FRONTEND;
  record.session = session;
  record.instance = session;  // Capture binding, not an inferred inference epoch.
  record.channels = 1;
  record.offered_frames = count;
  // Hash the complete offered span before/after; payload is only consumed PCM.
  record.checksum_before = checksum(pcm, count);
  record.timestamp_us = esp_timer_get_time();
  const auto output = __real_FrontendProcessSamples(state, pcm, count, consumed);
  record.duration_us = static_cast<uint32_t>(esp_timer_get_time() - record.timestamp_us);
  record.checksum_after = checksum(pcm, count);
  record.features = output.size != 0 ? 1 : 0;
  if (record.checksum_before != record.checksum_after) record.flags |= 1;
  if (*consumed > count) record.flags |= 2;
  record.frames = *consumed <= count ? *consumed : 0;
  record.checksum = checksum(pcm, record.frames);
  publish(target, record, pcm);
  return output;
}
}  // namespace esphome::esp_audio_capture

extern "C" const esp_afe_sr_iface_t *__wrap_esp_afe_handle_from_config(const afe_config_t *config) {
  return esphome::esp_audio_capture::observe_interface(__real_esp_afe_handle_from_config(config));
}
extern "C" FrontendOutput __wrap_FrontendProcessSamples(FrontendState *state, const int16_t *pcm,
                                                       size_t count, size_t *consumed) {
  return esphome::esp_audio_capture::observe_frontend(state, pcm, count, consumed);
}
#endif
