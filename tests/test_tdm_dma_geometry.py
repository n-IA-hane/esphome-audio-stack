"""Compile the actual C++ geometry selection with a minimal channel stub."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_automatic_tdm_geometry_preserves_legacy_timing_with_fallback(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required for the geometry regression test")
    root = Path(__file__).resolve().parents[1]
    cpp = (root / "esphome/components/esp_audio_stack/esp_audio_stack.cpp").read_text()
    start = cpp.index("uint32_t largest_divisor_at_most(")
    brace = cpp.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (cpp[end] == "{") - (cpp[end] == "}")
        end += 1
    helper = cpp[start:end]
    start = cpp.index("  const uint32_t max_bytes_per_frame =")
    alignment = cpp.index("  if (logical_tx_frames > 0 && max_frames > 0)", start)
    end = cpp.index("#if SOC_I2S_SUPPORTS_TDM", alignment)
    selection = cpp[start:end]
    source = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#define SOC_I2S_SUPPORTS_TDM 1
#define USE_ESP_AUDIO_STACK_TDM_BUS
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
enum class I2SHardwareState { ERROR };
constexpr uint32_t DMA_BUFFER_DURATION_MS = 10;
''' + helper + r'''
struct Channel {
  bool use_tdm_bus_ = true, dma_frame_num_configured_ = false;
  bool processor_dma_margin_ = true;
  uint32_t tdm_total_slots_ = 4, dma_desc_num_ = 6, dma_frame_num_ = 0;
  uint32_t sample_rate_ = 48000, rate_conversion_ratio_ = 12;
  uint32_t selected = 0;
  void set_i2s_hardware_state_(I2SHardwareState) {}
  bool select(uint32_t bytes_per_sample, uint32_t tx_bytes_per_frame,
              uint32_t rx_bytes_per_frame) {
''' + selection + r'''
    selected = dma_frame_num;
    return true;
  }
};
int main() {
  Channel sparse, full, wide, explicit_size, standard;
  assert(sparse.select(2, 2, 6) && sparse.selected == 384);
  assert(full.select(2, 8, 8) && full.selected == 384);
  assert(wide.select(4, 4, 12) && wide.selected == 256);
  explicit_size.dma_frame_num_configured_ = true;
  explicit_size.dma_frame_num_ = 512;
  assert(explicit_size.select(2, 2, 6) && explicit_size.selected == 512);
  standard.use_tdm_bus_ = false;
  assert(standard.select(2, 2, 4) && standard.selected == 768);
  assert((3840 + sparse.selected - 1) / sparse.selected == 10);
  assert((3840 + wide.selected - 1) / wide.selected == 15);
  assert(sparse.selected * 10 * (2 + 6) == 30720);
  assert(full.selected * 10 * (8 + 8) == 61440);
}
'''
    path = tmp_path / "geometry.cpp"
    binary = tmp_path / "geometry"
    path.write_text(source)
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", str(path), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
