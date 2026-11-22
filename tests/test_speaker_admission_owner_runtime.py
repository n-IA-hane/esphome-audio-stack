"""A new playback intent must not write into a previous STOPPING session."""
from pathlib import Path
import os
import subprocess

from speaker_gate_host import SPEAKER_GATE_FLAGS

from test_speaker_backpressure_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def test_playback_admission_follows_running_session(tmp_path):
    revision = os.environ.get("AUDIO_SPEAKER_SOURCE_REV")

    def read(path):
        if revision:
            return subprocess.check_output(
                ["git", "show", revision + ":esphome/components/esp_audio_stack/" + path],
                cwd=ROOT, text=True,
            )
        return (COMPONENT / path).read_text()

    parent = read("esp_audio_stack.cpp")
    facade = read("speaker/esp_audio_stack_speaker.cpp")
    methods = [
        method(parent, signature) for signature in (
            "void ESPAudioStack::start_speaker()", "void ESPAudioStack::stop_speaker()",
            "void ESPAudioStack::service_speaker_reset_()", "size_t ESPAudioStack::play(", "size_t ESPAudioStack::play_with_admission_(",
        )
    ]
    methods += [method(facade, signature) for signature in (
        "void ESPAudioStackSpeaker::start()", "void ESPAudioStackSpeaker::stop()",
        "void ESPAudioStackSpeaker::finish()", "void ESPAudioStackSpeaker::loop()",
        "bool ESPAudioStackSpeaker::has_buffered_data() const",
        "void ESPAudioStackSpeaker::speaker_output_callback(",
        "size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait)",
    )]
    source = tmp_path / "admission.cpp"
    source.write_text((ROOT / "tests/fixtures/speaker_admission_owner.cpp.in").read_text().replace(
        "// PRODUCTION_METHODS", "\n".join(methods)
    ))
    binary = tmp_path / "admission"
    compiled = subprocess.run(
        ["g++", *SPEAKER_GATE_FLAGS, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-g",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)],
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_gate_remaining_budget_includes_time_before_parking(tmp_path):
    # SPEAKER_GATE_FLAGS includes the complete production SpeakerWriteGate.
    # Only its RTOS clock and semaphore primitives are host adapters.
    source = tmp_path / "gate-budget.cpp"
    source.write_text(r'''
#include <cassert>
#include <cstdio>
#include <vector>

void verify_budget(TickType_t start, TickType_t before_parking,
                   const std::vector<TickType_t> &expected_waits) {
  HostSpeakerGate gate;
  use_speaker_test_clock = true;
  speaker_test_ticks = start;
  simulate_speaker_timeout = true;
  std::vector<TickType_t> waits;
  speaker_wait_hook = [&](TickType_t ticks) {
    waits.push_back(ticks);
    speaker_test_ticks += ticks;
  };
  unsigned attempts = 0;
  gate.run(7, [&] {
    // Independent admission work or preemption consumes the same total budget.
    if (++attempts == 1) speaker_test_ticks += before_parking;
    return false;
  });
  assert(waits == expected_waits);
  assert(speaker_test_ticks - start == 7);
  assert(attempts == 2);
  speaker_wait_hook = {};
}

int main() {
  verify_budget(100, 0, {7});
  verify_budget(100, 1, {6});
  verify_budget(100, 7, {});
  verify_budget(UINT32_MAX - 4, 3, {4});
  std::puts("PASS: admission time reduces the wait and the total budget stays seven ticks across wrap");
}
''')
    binary = tmp_path / "gate-budget"
    compiled = subprocess.run(
        ["g++", *SPEAKER_GATE_FLAGS, "-std=c++17", "-O1", "-pthread", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
