#pragma once
#ifndef USE_ESP32
#define USE_ESP32
#endif
#include "../../../esphome/components/esp_audio_stack/speaker_write_gate.h"
using esphome::esp_audio_stack::SpeakerAdmission;
struct HostSpeakerGate : esphome::esp_audio_stack::SpeakerWriteGate {
  HostSpeakerGate() { setup(); }
};
