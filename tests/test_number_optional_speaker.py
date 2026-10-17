"""Compile and exercise the actual number header with no speaker dependency."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("with_speaker", [False, True])
def test_number_header_without_optional_speaker(tmp_path, with_speaker):
    source = ROOT / "esphome/components/esp_audio_stack/number.h"
    (tmp_path / "number.h").write_bytes(source.read_bytes())
    stubs = {
        "esphome/components/number/number.h": """#pragma once
namespace esphome::number {class Number {public: virtual ~Number()=default;float state=0;void publish_state(float v){state=v;} unsigned get_object_id_hash(){return 1;}virtual void control(float)=0;};}""",
        "esphome/core/component.h": """#pragma once
namespace esphome {class Component {public:virtual void setup(){} virtual void dump_config(){};};}""",
        "esphome/core/preferences.h": """#pragma once
namespace esphome {inline bool has_saved=false;inline float saved=0;struct ESPPreferenceObject {bool load(float *v){if(!has_saved)return false;*v=saved;return true;}void save(float *v){has_saved=true;saved=*v;}};struct Preferences {template<class T>ESPPreferenceObject make_preference(unsigned){return {};}};inline Preferences prefs;inline Preferences *global_preferences=&prefs;}""",
        "esp_audio_stack.h": """#pragma once
namespace esphome::esp_audio_stack {struct ESPAudioStack {float gain=1,volume=.5;void set_mic_gain(float v){gain=v;}void set_master_volume(float v){volume=v;}float get_master_volume(){return volume;}};}""",
        "audio_core_log_utils.h": """#pragma once
namespace esphome::esp_audio_stack {template<class...T>void log_config(T...){}}""",
    }
    if with_speaker:
        stubs["esphome/components/speaker/speaker.h"] = """#pragma once
namespace esphome::speaker {struct Speaker {float volume=.25;float get_volume(){return volume;}void set_volume(float v){volume=v;}};}"""
    for name, body in stubs.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    main = """#include "number.h"
#include <cassert>
int main(){using namespace esphome::esp_audio_stack;
 ESPAudioStack parent;MicGainNumber gain;gain.set_parent(&parent);gain.setup();gain.dump_config();
 assert(gain.state==0&&parent.gain==1);
 // Board calibration is a default, not an override of a saved user setting.
 MicGainNumber calibrated;calibrated.set_parent(&parent);calibrated.set_initial_db(24);calibrated.setup();
 assert(calibrated.state==24&&parent.gain>15.84&&parent.gain<15.85);
 assert(!esphome::has_saved); // setup must not write a new user preference
 esphome::has_saved=true;esphome::saved=0;calibrated.setup();
 assert(calibrated.state==0&&parent.gain==1);
 esphome::saved=6;calibrated.setup();
 assert(calibrated.state==6&&parent.gain>1.99&&parent.gain<2.0);
 esphome::has_saved=false;MicGainNumber bounded;bounded.set_parent(&parent);
 bounded.set_min_db(5);bounded.set_max_db(10);bounded.setup();
 assert(bounded.state==5&&parent.gain>1.77&&parent.gain<1.78);
 static_cast<esphome::number::Number&>(gain).control(6);assert(parent.gain>1.99&&parent.gain<2.0);
 assert(esphome::has_saved&&esphome::saved==6);
 esphome::has_saved=false;
 MasterVolumeNumber volume;volume.set_parent(&parent);volume.setup();volume.dump_config();
 static_cast<esphome::number::Number&>(volume).control(35);assert(parent.volume==.35f);
#ifdef USE_SPEAKER
 esphome::speaker::Speaker speaker;MasterVolumeNumber native;native.set_speaker(&speaker);native.setup();
 static_cast<esphome::number::Number&>(native).control(75);assert(speaker.volume==.75f);
#endif
}"""
    cpp = tmp_path / "test.cpp"
    cpp.write_text(main)
    cmd = [
        "g++",
        "-std=c++17",
        "-DUSE_ESP32",
        "-DUSE_NUMBER",
        "-I",
        str(tmp_path),
        str(cpp),
        "-o",
        str(tmp_path / "test"),
    ]
    if with_speaker:
        cmd.insert(1, "-DUSE_SPEAKER")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run([str(tmp_path / "test")], check=True)
