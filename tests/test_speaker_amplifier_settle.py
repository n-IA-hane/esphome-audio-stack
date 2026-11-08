"""Optional PA settling preserves caller PCM and the existing speaker owner."""
from pathlib import Path
import re
import subprocess
import sys

import esphome
import pytest
from esphome.core import Lambda

from test_facade_pcm_config import base_config
from test_speaker_backpressure_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"
UPSTREAM = Path(esphome.__file__).resolve().parent


@pytest.mark.parametrize("setting", [None, "0ms", "250ms", "lambda"])
def test_optional_amplifier_gate_codegen(tmp_path, setting):
    document = base_config(tmp_path)
    if setting is not None:
        value = Lambda("return uint32_t{250};") if setting == "lambda" else setting
        document["speaker"][0]["amplifier_settle_time"] = value
    # ESPHome's serializer preserves the !lambda tag without a custom YAML shim.
    from esphome.yaml_util import dump
    config = tmp_path / "device.yaml"
    config.write_text(dump(document))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    defines = (tmp_path / "build/src/esphome/core/defines.h").read_text()
    main = (tmp_path / "build/src/main.cpp").read_text()
    enabled = setting in ("250ms", "lambda")
    assert ("#define USE_ESP_AUDIO_STACK_AMP_SETTLE" in defines) == enabled
    assert ("hw->set_amplifier_settle_time(" in main) == enabled


def test_optional_amplifier_gate_real_methods_and_upstream_retention(tmp_path):
    parent = (COMPONENT / "esp_audio_stack.cpp").read_text()
    facade = (COMPONENT / "speaker/esp_audio_stack_speaker.cpp").read_text()
    header = (COMPONENT / "speaker/esp_audio_stack_speaker.h").read_text()
    methods = [method(parent, signature) for signature in (
        "void ESPAudioStack::start_speaker()", "void ESPAudioStack::stop_speaker()",
        "void ESPAudioStack::service_speaker_reset_()", "size_t ESPAudioStack::play(",
    )]
    methods += [method(facade, signature) for signature in (
        "void ESPAudioStackSpeaker::start()", "void ESPAudioStackSpeaker::stop()",
        "void ESPAudioStackSpeaker::finish()", "void ESPAudioStackSpeaker::loop()",
        "bool ESPAudioStackSpeaker::has_buffered_data() const",
        "void ESPAudioStackSpeaker::speaker_output_callback(",
        "size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait)",
    )]
    fixture = (ROOT / "tests/fixtures/speaker_admission_owner.cpp.in").read_text()
    fixture = fixture[:fixture.index("int main() {")]
    automation = (UPSTREAM / "core/automation.h").read_text()
    templatable = method(automation, "template<typename T, typename... X> class TemplatableFn") + ";\n"
    templatable += "template<class T> using TemplatableStorage = TemplatableFn<T>;\n"
    fixture = fixture.replace("namespace speaker {", "template<class T> using optional = std::optional<T>;\n" + templatable + "namespace speaker {")
    fields = re.search(r"#ifdef USE_ESP_AUDIO_STACK_AMP_SETTLE\n(  TemplatableStorage.*?\n)#endif", header, re.S).group(1)
    setter = method(header, "template<typename V> void set_amplifier_settle_time(")
    fixture = fixture.replace("  unsigned last_write_ms_ = 0;", fields + "\n  " + setter + "\n  unsigned last_write_ms_ = 0;")
    fixture = fixture.replace("// PRODUCTION_METHODS", "\n".join(methods))
    transfer = (UPSTREAM / "components/audio/audio_transfer_buffer.cpp").read_text()
    transfer_methods = "\n".join(method(transfer, signature) for signature in (
        "void AudioTransferBuffer::decrease_buffer_length(",
        "size_t AudioSinkTransferBuffer::transfer_data_to_sink(",
    ))
    source = tmp_path / "settle.cpp"
    source.write_text("#include <concepts>\n#include <type_traits>\n#include <cstring>\n#define pdMS_TO_TICKS(x) (x)\n" + fixture + r'''
struct SinkCallback { size_t audio_sink_write(const uint8_t *, size_t, unsigned) { return 0; } };
struct AudioTransferBuffer {
  uint8_t *buffer_=nullptr, *data_start_=nullptr;
  size_t buffer_length_=0;
  size_t available() const { return buffer_length_; }
  void decrease_buffer_length(size_t);
};
struct AudioSinkTransferBuffer : AudioTransferBuffer {
  ESPAudioStackSpeaker *speaker_=nullptr;
  Buffer *ring_buffer_=nullptr;
  SinkCallback *sink_callback_=nullptr;
  size_t transfer_data_to_sink(TickType_t, bool);
};
''' + transfer_methods + r'''
bool idle_timer_running=false;
unsigned evaluations=0, gpio_calls=0, gpio_at=0;
uint32_t configured_settle=250;
uint32_t settle_template() { ++evaluations; return idle_timer_running ? 0 : configured_settle; }
void prepare(Rig &rig, bool warm=false) {
  idle_timer_running=warm; evaluations=0; gpio_calls=0;
  rig.parent.has_mic_consumers_=true;
  rig.sink.set_amplifier_settle_time(&settle_template);
}
void arm_gpio(Rig &rig) {
  rig.parent.speaker_start_callback_.hook=[] {
    ++gpio_calls; gpio_at=clock_ms; idle_timer_running=false;
  };
  rig.parent.speaker_idle_callback_.hook=[] { idle_timer_running=true; };
}
void cancel(Rig &rig) {
  rig.sink.stop(); rig.sink.loop(); rig.parent.service_speaker_reset_();
  assert(rig.sink.state_==speaker::STATE_STOPPED);
}
int main() {
  // Evaluate before the real parent's GPIO callback clears the warm marker.
  { Rig r; prepare(r,true); arm_gpio(r); r.sink.start(); r.sink.loop();
    assert(evaluations==1 && gpio_calls==1 && !idle_timer_running);
    assert(r.sink.state_==speaker::STATE_RUNNING && waited==0);
  }
  // Real upstream TransferBuffer must retain the complete prefix while play
  // refuses it, then consume only successful, frame-aligned partial writes.
  { Rig r; prepare(r); arm_gpio(r); r.sink.start(); r.sink.loop();
    assert(r.sink.state_==speaker::STATE_STARTING && gpio_at==0 && evaluations==1);
    std::vector<uint8_t> pcm(34); for(size_t i=0;i<pcm.size();++i)pcm[i]=i+1;
    AudioSinkTransferBuffer pending; pending.speaker_=&r.sink;
    pending.buffer_=pending.data_start_=pcm.data(); pending.buffer_length_=pcm.size();
    assert(pending.transfer_data_to_sink(20,false)==0 && pending.available()==pcm.size() && waited==20);
    clock_ms=249; r.sink.loop();
    const unsigned before=waited;
    assert(pending.transfer_data_to_sink(UINT32_MAX,false)==0 && waited-before==1);
    assert(pending.available()==pcm.size() && r.parent.storage.queued.empty());
    clock_ms=250; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
    r.parent.storage.capacity=10;
    while(pending.available()) {
      const size_t n=pending.transfer_data_to_sink(0,false);
      assert(n && n%2==0); r.parent.storage.emit();
    }
    assert(r.parent.storage.emitted==pcm && r.parent.storage.discarded.empty());
    assert(r.sink.audio_output_callback_.frames==pcm.size()/2 && evaluations==1);
  }
  // Warm zero cannot erase an unfinished PA interval after a cancelled stream.
  { Rig r; prepare(r); arm_gpio(r); r.sink.start(); r.sink.loop();
    clock_ms=50; cancel(r); arm_gpio(r); r.sink.start(); r.sink.loop();
    assert(evaluations==2 && r.sink.state_==speaker::STATE_STARTING);
    clock_ms=249; r.sink.loop(); assert(r.sink.state_==speaker::STATE_STARTING);
    clock_ms=250; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  // A newly cold restart receives its full interval after the new GPIO edge.
  { Rig r; prepare(r); arm_gpio(r); r.sink.start(); r.sink.loop();
    clock_ms=50; cancel(r); idle_timer_running=false; clock_ms=60;
    arm_gpio(r); r.sink.start(); r.sink.loop();
    clock_ms=250; r.sink.loop(); assert(r.sink.state_==speaker::STATE_STARTING);
    clock_ms=310; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  // Slow I2S opening cannot consume the PA's later settling interval.
  { Rig r; prepare(r); arm_gpio(r); r.parent.async_start=true; r.parent.audio_stack_running_=false;
    r.sink.start(); r.sink.loop(); assert(gpio_calls==0 && evaluations==1);
    clock_ms=1000; r.parent.commit_start(); r.sink.loop();
    assert(gpio_at==1000 && r.sink.state_==speaker::STATE_STARTING && evaluations==1);
    clock_ms=1249; r.sink.loop(); assert(r.sink.state_==speaker::STATE_STARTING);
    clock_ms=1250; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  // Cancelling before GPIO readiness leaves no inherited, started interval.
  { Rig r; prepare(r); arm_gpio(r); r.parent.async_start=true; r.parent.audio_stack_running_=false;
    r.sink.start(); r.sink.loop(); clock_ms=50; cancel(r); assert(gpio_calls==0);
    arm_gpio(r); r.sink.start(); r.sink.loop(); r.parent.commit_start(); r.sink.loop();
    clock_ms=300; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  // finish() drains the local stream; an explicit start supersedes it without
  // re-evaluating or discarding the in-progress PA interval.
  { Rig r; prepare(r); arm_gpio(r); r.sink.start(); r.sink.loop();
    r.sink.finish(); r.sink.start(); r.sink.loop();
    assert(!r.sink.finishing_ && evaluations==1 && r.sink.state_==speaker::STATE_STARTING);
    clock_ms=250; r.sink.loop(); r.sink.finish(); r.sink.loop(); r.sink.loop();
    r.parent.service_speaker_reset_(); assert(r.sink.state_==speaker::STATE_STOPPED);
    arm_gpio(r); r.sink.start(); r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  // Modulo elapsed time works across wrap and after a long stopped period.
  { Rig r; prepare(r); arm_gpio(r); clock_ms=UINT32_MAX-100;
    const unsigned start=clock_ms; r.sink.start(); r.sink.loop();
    clock_ms=start+249; r.sink.loop(); assert(r.sink.state_==speaker::STATE_STARTING);
    clock_ms=start+250; r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  { Rig r; prepare(r); arm_gpio(r); clock_ms=1000; r.sink.start(); r.sink.loop();
    clock_ms=1050; cancel(r); clock_ms=1000u+uint32_t(INT32_MAX)+5000u;
    arm_gpio(r); r.sink.start(); r.sink.loop(); assert(r.sink.state_==speaker::STATE_RUNNING);
  }
  std::puts("PASS: PA-before-admission, warm0, exact upstream prefix retention, finite wait budgets, cancel/restart, async open, finish and wrap");
}
''')
    binary = tmp_path / "settle"
    compiled = subprocess.run(
        ["g++", "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
         "-DUSE_SPEAKER", "-DUSE_ESP_AUDIO_STACK_AMP_SETTLE", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
