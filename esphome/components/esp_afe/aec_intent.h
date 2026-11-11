#pragma once

#include <atomic>
#include <cstdint>

namespace esphome::esp_afe {

// One canonical desired state and revision. Bit 1 is reserved, so an invalid
// applied word can never equal a request, including when the revision wraps.
class AecIntent {
 public:
  explicit AecIntent(bool enabled) : requested_(enabled ? 1U : 0U) {}

  uint32_t snapshot() const { return this->requested_.load(std::memory_order_acquire); }
  static bool enabled(uint32_t request) { return (request & 1U) != 0; }
  bool load(std::memory_order order = std::memory_order_seq_cst) const {
    return enabled(this->requested_.load(order));
  }
  void store(bool enabled, std::memory_order = std::memory_order_seq_cst) { this->request(enabled); }
  operator bool() const { return this->load(); }
  bool operator=(bool enabled) { this->store(enabled); return enabled; }

  uint32_t request(bool enabled) {
    uint32_t previous = this->snapshot();
    while (AecIntent::enabled(previous) != enabled) {
      const uint32_t next = next_request_(previous, enabled);
      if (this->requested_.compare_exchange_weak(previous, next, std::memory_order_acq_rel)) return next;
    }
    return previous;
  }

  // A live control completion must not overwrite an intent accepted while it
  // was waiting on the config owner or inside the vendor call.
  bool publish_if_current(uint32_t previous, bool enabled, uint32_t *published) {
    const uint32_t next = AecIntent::enabled(previous) == enabled ? previous : next_request_(previous, enabled);
    if (!this->requested_.compare_exchange_strong(previous, next, std::memory_order_acq_rel)) return false;
    *published = next;
    return true;
  }

  void restore_if_current(uint32_t failed, bool enabled) {
    this->requested_.compare_exchange_strong(failed, next_request_(failed, enabled), std::memory_order_acq_rel);
  }
  void acknowledge(uint32_t applied) { this->applied_.store(applied, std::memory_order_release); }
  void invalidate() { this->applied_.store(2U, std::memory_order_release); }
  bool is_applied() const {
    const uint32_t requested = this->snapshot();
    return this->applied_.load(std::memory_order_acquire) == requested && this->snapshot() == requested;
  }

 protected:
  static uint32_t next_request_(uint32_t previous, bool enabled) {
    return ((previous + 4U) & ~3U) | (enabled ? 1U : 0U);
  }
  std::atomic<uint32_t> requested_;
  std::atomic<uint32_t> applied_{2U};
};

}  // namespace esphome::esp_afe
