#include "adaptive_volume.h"

#include <cassert>
#include <climits>

using esphome::esp_audio_stack::AdaptivePlaybackVolume;

int main() {
  AdaptivePlaybackVolume volume;
  constexpr int32_t user_gain = INT32_MAX / 2;
  assert(volume.apply_to(user_gain) == user_gain);
  volume.sync_switch(1);
  const int32_t quiet = volume.apply_to(user_gain);
  assert(quiet > 0 && quiet < user_gain / 6);

  uint32_t now = UINT32_MAX - 1000;
  auto observe = [&](int16_t amplitude, unsigned windows, bool playing = false) {
    const int16_t pcm[] = {amplitude, static_cast<int16_t>(-amplitude)};
    for (unsigned i = 0; i < windows; ++i) {
      volume.observe(now, pcm, 2, playing);
      assert(volume.apply_to(0) == 0);
      assert(volume.apply_to(user_gain) <= user_gain);
      now += 500;
    }
  };
  observe(20, 18);
  observe(30000, 1);  // One loud impact in a quiet room.
  observe(20, 2);
  assert(volume.apply_to(user_gain) == quiet);
  // Regular short playbacks followed by a 100 ms acoustic tail must not
  // feed the speaker level back into the ambient estimate.
  const int16_t echo[] = {3000, -3000};
  for (unsigned i = 0; i < 32; ++i) {
    volume.observe(now, nullptr, 0, true);
    volume.observe(now + 100, echo, 2, false);
    now += 1000;
  }
  assert(volume.apply_to(user_gain) == quiet);
  observe(3000, 40, true);  // The device hears its own announcement.
  assert(volume.apply_to(user_gain) == quiet);
  observe(3000, 25);
  assert(volume.apply_to(user_gain) == user_gain);
  volume.sync_switch(3);  // Both off and on happened while the audio task was parked.
  assert(volume.apply_to(user_gain) == quiet);
  observe(3000, 25);
  assert(volume.apply_to(user_gain) == user_gain);
  observe(20, 25);
  assert(volume.apply_to(user_gain) == quiet);
  volume.sync_switch(4);
  assert(volume.apply_to(user_gain) == user_gain);
  observe(3000, 50);
  volume.sync_switch(5);
  assert(volume.apply_to(user_gain) == quiet);
  volume.configure(-55.0f, -20.0f, 0.0f);
  volume.sync_switch(6);
  volume.sync_switch(7);
  assert(volume.apply_to(user_gain) == user_gain);
}
