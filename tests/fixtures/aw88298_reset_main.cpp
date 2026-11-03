int main() {
  using namespace esphome::esp_audio_stack;
  CodecDevBackend::GenericCodecConfig output;
  output.enabled = true;
  output.kind = CodecDevBackend::CodecKind::AW88298;
  esphome::GPIOPin first, second;
  CodecDevBackend a, b;
  a.set_output_codec_config(output);
  b.set_output_codec_config(output);
  b.set_aw88298_hw_gain(24.0f, 12.0f, 1.8f);
  a.set_aw88298_reset_pin(&first);
  b.set_aw88298_reset_pin(&second);
  auto start = [](CodecDevBackend &backend) {
    return backend.setup(0, 0, reinterpret_cast<void *>(1), nullptr, 0);
  };
  assert(start(a));
  assert(start(b));
  assert(first.setups == 1 && second.setups == 1);
  assert(first.writes == std::vector<bool>({false, true}));
  assert(second.writes == first.writes);
  auto one = aw_configs[0], two = aw_configs[1];
  assert(one.pa_cfg.hw_gain.pa_gain == 0.0f);
  assert(one.pa_cfg.hw_gain.pa_voltage == 5.0f);
  assert(one.pa_cfg.hw_gain.codec_dac_voltage == 3.3f);
  assert(two.pa_cfg.hw_gain.pa_gain == 24.0f);
  assert(two.pa_cfg.hw_gain.pa_voltage == 12.0f);
  assert(two.pa_cfg.hw_gain.codec_dac_voltage == 1.8f);
  assert(one.reset_cfg.reset_pin != two.reset_cfg.reset_pin);
  assert(one.gpio_if->set(one.reset_cfg.reset_pin, false) == 0);
  assert(!first.digital_read() && second.digital_read());
  assert(one.gpio_if->get(one.reset_cfg.reset_pin) == false);
  assert(one.gpio_if->setup(0, AUDIO_GPIO_DIR_OUT, AUDIO_GPIO_MODE_FLOAT) != 0);
  assert(one.gpio_if->set(-1, true) != 0);
  assert(one.gpio_if->setup(one.reset_cfg.reset_pin, AUDIO_GPIO_DIR_IN, AUDIO_GPIO_MODE_FLOAT) != 0);
  a.teardown();
  assert(one.gpio_if->set(one.reset_cfg.reset_pin, true) != 0);
  assert(two.gpio_if->set(two.reset_cfg.reset_pin, false) == 0);
  assert(start(a));
  assert(a.aw88298_reset_id_ == one.reset_cfg.reset_pin);
  a.teardown();
  b.teardown();
  assert(resources.empty());
  reset_faults();
  // Fail codec allocation after the driver's reset sequence, then retry.
  fail_allocation = 3;
  assert(!start(a));
  const auto failed_id = a.aw88298_reset_id_;
  a.teardown();
  assert(codec_reset_pin(failed_id) == nullptr);
  assert(resources.empty());
  reset_faults();
  assert(start(a));
  a.teardown();
  a.set_aw88298_reset_pin(nullptr);
  assert(start(a));
  assert(aw_configs.back().gpio_if == nullptr);
  a.teardown();
  assert(resources.empty());
  // Public open uses the real no-map fallback, negotiated TDM geometry and
  // register adapter. Keep all bits outside I2SBCK[5:4] unchanged.
  CodecDevBackend::SampleConfig fs;
  fs.channels = 4;
  fs.channel_mask = 5;
  reset_faults();
  assert(start(a));
  auto *hw = &a.tx_codec_if_->hw_base;
  const int original = aw_registers.at(hw);
  const int writes_before = aw_writes;
  assert(a.open(&fs, nullptr));
  assert(aw_registers.at(hw) == ((original & ~0x30) | 0x20));
  assert(aw_writes == writes_before + 1);
  a.close();
  assert(a.open(&fs, nullptr));
  assert(aw_writes == writes_before + 1);  // Already-correct framing is left alone.
  a.teardown();
  assert(resources.empty());
  for (bool fail_read : {false, true}) {
    reset_faults();
    assert(start(a));
    aw_read_failure = fail_read;
    aw_write_failure = !fail_read;
    assert(!a.open(&fs, nullptr));
    assert_closed(a);
    assert(!a.tx_dev_->open);
    aw_read_failure = aw_write_failure = false;
    assert(a.open(&fs, nullptr));
    a.teardown();
    assert(resources.empty());
  }
  reset_faults();
  assert(start(a));
  fixture_slot_bits = 32;  // Four slots at 32 bits exceed AW88298's 64-bit frame.
  const int reads_before = aw_reads;
  assert(!a.open(&fs, nullptr));
  assert(aw_reads == reads_before);
  assert_closed(a);
  assert(!a.tx_dev_->open);
  fixture_slot_bits = 16;
  assert(a.open(&fs, nullptr));
  a.teardown();
  assert(resources.empty());

  // Other codecs retain the pre-PR rejection of a missing channel-order table.
  output.kind = CodecDevBackend::CodecKind::ES8311;
  a.set_output_codec_config(output);
  fixture_missing_order = true;
  assert(start(a));
  assert(!a.open(&fs, nullptr));
  assert(!a.tx_dev_->open);
  a.teardown();
  fixture_missing_order = false;
  assert(resources.empty());
  for (auto *pin : codec_reset_pins) assert(pin == nullptr);
}
