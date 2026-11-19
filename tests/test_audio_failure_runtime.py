"""Compose real lifecycle and adapters with PCM pending at a runtime failure."""

from pathlib import Path
import subprocess

from speaker_gate_host import SPEAKER_GATE_FLAGS

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def block(source: str, marker: str) -> str:
    start = source.index(marker)
    return source[start:source.index("\n}", start) + 2]


def failure_source() -> str:
    header = (COMPONENT / "esp_audio_stack.h").read_text()
    phase = header[header.index("enum class AudioStartPhase"):]
    phase = phase[:phase.index(";") + 1]
    adapter = (ROOT / "tests/fixtures/audio_failure.cpp").read_text()
    marker = "bool has_failed_session() const"
    if marker in header:
        a = header.index(marker)
        helper = header[a:header.index("\n  }", a) + 4]
        adapter = adapter.replace("bool has_failed_session()const;", helper)
    else:
        adapter = adapter.replace("bool has_failed_session()const;", "bool has_failed_session()const{return false;}")
    source = "#include <cstdint>\n" + phase + "\n" + adapter
    owner = (COMPONENT / "esp_audio_stack.cpp").read_text()
    for method in ("start", "stop", "service_audio_lifecycle_", "finish_audio_lifecycle_",
                   "start_speaker", "stop_speaker", "service_speaker_reset_", "fail_tx_completion_tracking_"):
        source += block(owner, "void ESPAudioStack::" + method + "(") + "\n"
    source += block(owner, "size_t ESPAudioStack::play(") + "\n"
    source += block(owner, "size_t ESPAudioStack::play_with_admission_(") + "\n"
    source += block((COMPONENT / "audio_pipeline.cpp").read_text(), "void ESPAudioStack::fail_audio_session_(") + "\n"
    for kind, methods in (("speaker", ("start", "stop", "loop")),
                           ("microphone", ("start", "stop", "release_listener_", "loop"))):
        cpp = (COMPONENT / kind / ("esp_audio_stack_" + kind + ".cpp")).read_text()
        cls = "ESPAudioStack" + kind.capitalize()
        for method in methods:
            source += block(cpp, ("bool " if method == "release_listener_" else "void ") + cls + "::" + method + "(") + "\n"
        if kind == "speaker":
            source += block(cpp, "bool ESPAudioStackSpeaker::has_buffered_data(") + "\n"
            source += block(cpp, "size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t") + "\n"
    return source


@pytest.mark.parametrize("stage", ["tx", "rx"])
def test_failed_parent_stops_adapters_preserves_consumers_and_recovers_once(tmp_path, stage):
    source = tmp_path / "failure.cpp"
    source.write_text(failure_source() + r'''
void run_owner(ESPAudioStack &p){p.service_audio_lifecycle_();p.finish_audio_lifecycle_();}
void fail(ESPAudioStack &p,const std::string &stage){
  if(stage=="tx")p.fail_tx_completion_tracking_("injected TX failure");
  else p.fail_audio_session_("injected RX failure");
  p.service_audio_lifecycle_();
  uint8_t old_pcm[16]{};
  assert(p.play(old_pcm,sizeof(old_pcm),0)==0&&"failed generation must reject PCM before the main callback");
  p.finish_audio_lifecycle_();
}
int main(int argc,char **argv){
  ESPAudioStack p;p.start();run_owner(p);assert(p.is_running());
  ESPAudioStackSpeaker s{&p};ESPAudioStackMicrophone m{&p};
  p.speaker_running_=true;assert(p.ring.queued>0&&p.processor.active);
  fail(p,argv[1]);s.loop();m.loop();
  assert(!p.is_running()&&!p.start_requested_);
  assert(s.state_==speaker::STATE_STOPPED&&m.state_==microphone::STATE_STOPPED);
  assert(!p.speaker_running_&&!p.processor.active&&p.ring.queued==0);
  assert(s.listener_registered_&&m.active_listeners_==1&&m.parent_registered_);
  for(unsigned i=0;i<40;++i){s.loop();m.loop();}
  assert(p.opens==1);
  uint8_t pcm[64]{};
  s.play(pcm,sizeof(pcm),0);s.loop();m.loop();run_owner(p);s.loop();m.loop();
  assert(p.is_running()&&p.opens==2);
  assert(s.state_==speaker::STATE_RUNNING&&m.state_==microphone::STATE_RUNNING);
  assert(p.ring.queued<=sizeof(pcm)&&m.active_listeners_==1);
  fail(p,argv[1]);s.loop();m.loop();
  for(unsigned i=0;i<40;++i){assert(s.play(pcm,sizeof(pcm),0)==0);s.loop();m.loop();}
  assert(p.opens==2&&!p.start_requested_&&p.ring.queued==0);
  s.start();s.loop();run_owner(p);s.loop();m.loop();
  assert(p.opens==3&&p.is_running());
  assert(s.play(pcm,sizeof(pcm),0)==sizeof(pcm));
}
''')
    executable = tmp_path / "failure"
    result = subprocess.run(["g++", *SPEAKER_GATE_FLAGS, "-std=c++17", "-pthread", str(source), "-o", str(executable)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(executable), stage], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_explicit_stop_cancels_direct_producer_before_i2s_start(tmp_path):
    source = tmp_path / "prebuffer_stop.cpp"
    source.write_text(failure_source() + r'''
#include <future>
#include <thread>
int main() {
  ESPAudioStack parent;
  parent.ring.queued = parent.ring.capacity;
  std::promise<void> parked;
  auto entered = parked.get_future();
  auto producer = std::async(std::launch::async, [&] {
    speaker_wait_hook = [&](unsigned) { parked.set_value(); };
    uint8_t pcm[4]{};
    return parent.play(pcm, sizeof(pcm), portMAX_DELAY);
  });
  assert(entered.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
  parent.stop();
  const bool reset_requested = parent.request_speaker_reset_.load();
  parent.service_speaker_reset_();
  const bool cancelled = producer.wait_for(std::chrono::seconds(1)) == std::future_status::ready;
  if (!cancelled) parent.notify_speaker_writers_(true);  // retire the failing witness
  assert(producer.get() == 0 && cancelled && reset_requested && parent.ring.queued == 0);
}
''')
    binary = tmp_path / "prebuffer_stop"
    subprocess.run(["g++", *SPEAKER_GATE_FLAGS, "-std=c++17", "-pthread", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=6)
