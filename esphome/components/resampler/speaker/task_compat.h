#pragma once

#include "esphome/core/static_task.h"

namespace esphome::resampler {

// ESPHome 2026.10 does not expose task affinity in StaticTask::create yet.
// Adapt creation only; the same StaticTask owns the TCB, stack and teardown.
class ScheduledTask : public StaticTask {
 public:
  bool create(TaskFunction_t fn, const char *name, uint32_t stack_size, void *param, UBaseType_t priority,
              bool use_psram, BaseType_t core_id) {
    if (this->handle_ != nullptr)
      return false;
    if (this->stack_buffer_ != nullptr && (stack_size > this->stack_size_ || use_psram != this->use_psram_))
      this->stack_buffer_.reset();
    if (this->stack_buffer_ == nullptr) {
      this->stack_size_ = stack_size;
      this->use_psram_ = use_psram;
      RAMAllocator<StackType_t> allocator(use_psram ? RAMAllocator<StackType_t>::ALLOC_EXTERNAL
                                                  : RAMAllocator<StackType_t>::ALLOC_INTERNAL);
      this->stack_buffer_ = allocator.make_unique_array_for_overwrite(stack_size);
    }
    if (this->stack_buffer_ == nullptr)
      return false;
    if (this->tcb_ == nullptr) {
      RAMAllocator<StaticTask_t> allocator(RAMAllocator<StaticTask_t>::ALLOC_INTERNAL);
      this->tcb_ = allocator.make_unique();
    }
    if (this->tcb_ == nullptr) {
      this->deallocate();
      return false;
    }
    this->handle_ = xTaskCreateStaticPinnedToCore(fn, name, this->stack_size_, param, priority,
                                                 this->stack_buffer_.get(), this->tcb_.get(), core_id);
    if (this->handle_ == nullptr) {
      this->deallocate();
      return false;
    }
    return true;
  }
};

}  // namespace esphome::resampler
