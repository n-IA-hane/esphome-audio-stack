#pragma once

#include "esphome/core/static_task.h"

namespace esphome::resampler {

// Native StaticTask remains the sole owner of the TCB, stack and teardown.
// Only creation needs an adapter on ESPHome 2026.9.1, before the affinity API.
template<typename Task = StaticTask> class ScheduledTask : public Task {
 public:
  bool create(TaskFunction_t fn, const char *name, uint32_t stack_size, void *param,
              UBaseType_t priority, bool use_psram, BaseType_t core_id) {
    if constexpr (requires(Task &task) { task.create(fn, name, stack_size, param, priority, use_psram, core_id); }) {
      return Task::create(fn, name, stack_size, param, priority, use_psram, core_id);
    } else {
      if (this->handle_ != nullptr)
        return false;
      if (this->stack_buffer_ != nullptr && (stack_size > this->stack_size_ || use_psram != this->use_psram_)) {
        RAMAllocator<StackType_t> allocator(this->use_psram_ ? RAMAllocator<StackType_t>::ALLOC_EXTERNAL
                                                          : RAMAllocator<StackType_t>::ALLOC_INTERNAL);
        allocator.deallocate(this->stack_buffer_, this->stack_size_);
        this->stack_buffer_ = nullptr;
      }
      if (this->stack_buffer_ == nullptr) {
        this->stack_size_ = stack_size;
        this->use_psram_ = use_psram;
        RAMAllocator<StackType_t> allocator(use_psram ? RAMAllocator<StackType_t>::ALLOC_EXTERNAL
                                                    : RAMAllocator<StackType_t>::ALLOC_INTERNAL);
        this->stack_buffer_ = allocator.allocate(stack_size);
      }
      if (this->stack_buffer_ == nullptr)
        return false;
      this->handle_ = xTaskCreateStaticPinnedToCore(fn, name, this->stack_size_, param, priority,
                                                  this->stack_buffer_, &this->tcb_, core_id);
      if (this->handle_ == nullptr) {
        this->deallocate();
        return false;
      }
      return true;
    }
  }
};

}  // namespace esphome::resampler
