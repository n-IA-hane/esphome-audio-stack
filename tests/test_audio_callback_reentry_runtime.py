"""Production lifecycle callbacks may synchronously replace their own intent."""
from pathlib import Path
import subprocess

import pytest

from test_audio_failure_runtime import failure_source
from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]


def registry_source():
    code = failure_source().replace(
        "#include <algorithm>", "#include <algorithm>\n#include <mutex>",
    ).replace(
        "struct Callback {unsigned count=0;void call(){++count;}};",
        "struct Callback {unsigned count=0;std::function<void()> fn;void call(){++count;if(fn)fn();}};\n"
        "struct ScopedLock {std::unique_lock<std::mutex> lock;ScopedLock(std::mutex*m):lock(*m){}};",
    ).replace(
        "Callback start_callback_,idle_callback_",
        "std::mutex registry_mutex;std::mutex*mic_consumers_mutex_=&registry_mutex;"
        "void*mic_consumers_[MAX_LISTENERS]{};size_t mic_consumer_count_=0;\n"
        "  Callback mic_start_callback_,mic_idle_callback_;\n  Callback start_callback_,idle_callback_",
    ).replace("bool register_mic_consumer(void*){return true;}", "bool register_mic_consumer(void*);").replace(
        "void unregister_mic_consumer(void*){}", "void unregister_mic_consumer(void*);",
    )
    owner = (ROOT / "esphome/components/esp_audio_stack/esp_audio_stack.cpp").read_text()
    return code + method(owner, "bool ESPAudioStack::register_mic_consumer(") + "\n" + method(
        owner, "void ESPAudioStack::unregister_mic_consumer(") + "\n"


@pytest.mark.parametrize("running", [False, True])
def test_on_mic_start_can_remove_its_last_consumer_without_starting_or_waking_afe(tmp_path, running):
    checks = r'''
void run_owner(ESPAudioStack&p){p.service_audio_lifecycle_();p.finish_audio_lifecycle_();}
int main(int argc,char**argv){
 ESPAudioStack p;p.has_mic_consumers_=false;p.speaker_requested_=false;
 if(argv[1][0]=='1'){p.start_speaker();run_owner(p);}
 int token=1;
 p.mic_start_callback_.fn=[&]{p.unregister_mic_consumer(&token);};
 assert(p.register_mic_consumer(&token));
 assert(!p.has_mic_consumers_&&p.mic_consumer_count_==0&&!p.processor.active);
 if(argv[1][0]=='0')assert(!p.start_requested_&&!p.is_running());
 else assert(p.start_requested_&&p.is_running()&&p.speaker_running_);
 run_owner(p);assert(p.opens==(argv[1][0]=='1'?1u:0u));
}
'''
    compile_run(tmp_path, registry_source() + checks, "1" if running else "0")


def test_on_speaker_idle_restarts_after_explicit_stack_stop(tmp_path):
    checks = r'''
void run_owner(ESPAudioStack&p){p.service_audio_lifecycle_();p.finish_audio_lifecycle_();}
int main(){
 ESPAudioStack p;p.has_mic_consumers_=false;p.speaker_requested_=false;
 p.start_speaker();run_owner(p);assert(p.speaker_running_&&p.is_running());
 p.speaker_idle_callback_.fn=[&]{
  assert(!p.is_running()&&p.teardown_pending_&&"callback must observe committed stop");
  p.start_speaker();
 };
 p.stop();
 assert(p.start_requested_&&p.speaker_requested_&&!p.is_running());
 run_owner(p);
 assert(p.is_running()&&p.speaker_running_&&p.opens==2&&p.closes==1);
 assert(p.speaker_start_callback_.count==2&&p.speaker_idle_callback_.count==1);
}
'''
    compile_run(tmp_path, registry_source() + checks)


def compile_run(tmp_path, source, *args):
    cpp = tmp_path / "reentry.cpp"
    cpp.write_text(source)
    binary = tmp_path / "reentry"
    built = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-pthread", "-fsanitize=address,undefined",
                            str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary), *args], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
