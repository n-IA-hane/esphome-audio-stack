"""Compile the real GMF stack-placement adapter against both supported headers."""
from pathlib import Path
import subprocess


def test_stack_memory_keeps_p4_internal_and_s3_external(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'esphome/components/esp_afe/esp_afe.cpp').read_text()
    start = source.index('template<typename Setting>\nstatic auto configure_gmf_stack_memory')
    end = source.index('\nstruct AfeModePreset', start)
    harness = tmp_path / 'placement.cpp'
    harness.write_text('#include <cassert>\n' + source[start:end] + r'''
struct NewSetting { int stack_size = 4096; bool stack_in_internal = false; };
struct LegacySetting { int stack_size = 4096; };
int main() {
  NewSetting feed, fetch;
  bool flags[] = {true, false};
  for (bool internal : flags) {
    configure_gmf_stack_memory(feed, internal, 0);
    configure_gmf_stack_memory(fetch, internal, 0);
    assert(feed.stack_in_internal == internal);
    assert(fetch.stack_in_internal == internal);
    assert(feed.stack_size == 4096 && fetch.stack_size == 4096);
  }
  LegacySetting legacy;
  configure_gmf_stack_memory(legacy, true, 0);
  assert(legacy.stack_size == 4096);
}
''')
    binary = tmp_path / 'placement'
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(harness), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
