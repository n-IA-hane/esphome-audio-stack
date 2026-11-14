"""Independent numerical contract for the standalone production DC blocker."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "esp_audio_stack" if (ROOT / "esphome/components/esp_audio_stack").is_dir() else "voip_stack"


def test_dc_blocker_extreme_steps_random_signal_and_reset(tmp_path):
    namespace = "esphome::esp_audio_stack" if COMPONENT == "esp_audio_stack" else "esphome"
    source = tmp_path / "dc_numeric.cpp"
    source.write_text(f'#include "esphome/components/{COMPONENT}/audio_core_audio_utils.h"\n'
                      f'using {namespace}::DcBlockerState;\n' + r'''
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>

int main() {
  for (int polarity : {-1, 1}) {
    DcBlockerState filter;
    for (unsigned i = 0; i < 5000; ++i) filter.process(polarity * 20000);
    assert(filter.process(-polarity * 20000) == (polarity > 0 ? -32768 : 32767));
  }
  for (unsigned pass = 0; pass < 2; ++pass) {
    DcBlockerState filter;
    long double previous_input = 0, previous_output = 0;
    uint32_t random = 17;
    for (unsigned i = 0; i < 100000; ++i) {
      random = random * 1664525U + 1013904223U;
      const int16_t sample = i < 10000 ? (i & 1 ? INT16_MIN : INT16_MAX)
                                     : static_cast<int16_t>(random >> 16);
      // A floating reference expresses the filter equation independently of
      // the implementation's fixed-point storage and arithmetic.
      const long double output = sample - previous_input + (1023.L / 1024.L) * previous_output;
      previous_input = sample;
      previous_output = output;
      const int expected = static_cast<int>(std::clamp(std::floor(output), -32768.L, 32767.L));
      const int actual = filter.process(sample);
      assert(std::abs(actual - expected) <= 1);
    }
    filter = {};
    assert(filter.process(0) == 0);
  }
}
''')
    binary = tmp_path / "dc_numeric"
    built = subprocess.run(["g++", "-std=c++17", "-O1", "-fsanitize=undefined",
                            "-fno-sanitize-recover=all", "-I", str(ROOT), str(source), "-o", str(binary)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
