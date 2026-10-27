"""A new playback intent must not write into a previous STOPPING session."""
from pathlib import Path
import os
import subprocess

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
            "void ESPAudioStack::service_speaker_reset_()", "size_t ESPAudioStack::play(",
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
        ["g++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-g",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)],
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr
