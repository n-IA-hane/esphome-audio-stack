"""ESP32-S3 direct microphone input timing, from validation to the RX register."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from esphome import config_validation as cv

from test_facade_pcm_config import base_config
from test_std_dual_mic import schema

COMPONENT = Path(__file__).resolve().parents[1] / "esphome/components/esp_audio_stack"


@pytest.mark.parametrize(("option", "value"), [(None, None), ("none", None), ("pos_edge", 1), ("neg_edge", 2)])
def test_delay_codegen(tmp_path, option, value):
    config = base_config(tmp_path)
    if option is not None:
        config["esp_audio_stack"]["rx_data_delay"] = option
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)], check=True)
    defines = (tmp_path / "build/src/esphome/core/defines.h").read_text().splitlines()
    delay = [line for line in defines if line.startswith("#define USE_ESP_AUDIO_STACK_RX_DATA_DELAY")]
    assert delay == ([] if value is None else [f"#define USE_ESP_AUDIO_STACK_RX_DATA_DELAY {value}"])


@pytest.mark.parametrize(("variant", "layout", "message"), [
    ("ESP32P4", {}, "ESP32-S3"),
    ("ESP32S3", {"rx_bus": {"i2s_num": 0}, "tx_bus": {"i2s_num": 1}}, "shared standard"),
    ("ESP32S3", {"use_tdm_reference": True}, "shared standard"),
    ("ESP32S3", {"tdm_mic_slots": [0, 2]}, "shared standard"),
    ("ESP32S3", {"i2s_mode": "secondary"}, "primary mode"),
    ("ESP32S3", {"i2s_din_pin": -1}, "i2s_din_pin"),
    ("ESP32S3", {"codec": {"output": {}}}, "without codec"),
])
def test_delay_rejects_unsupported_layouts(monkeypatch, variant, layout, message):
    monkeypatch.setattr(schema, "get_esp32_variant", lambda: variant)
    config = {"i2s_din_pin": 4, "rx_data_delay": "pos_edge", **layout}
    with pytest.raises(cv.Invalid, match=message) as error:
        schema._final_validate(config)
    assert error.value.path == ["rx_data_delay"]


@pytest.mark.parametrize("delay", [None, 1, 2])
def test_std_rx_init_selects_the_paired_peripheral_each_session(tmp_path, delay):
    source = (COMPONENT / "esp_audio_stack.cpp").read_text()
    init = source.index("err = i2s_channel_init_std_mode(this->rx_handle_, &rx_cfg);")
    begin = source.rindex("    if (this->rx_handle_)", 0, init)
    end = source.index("\n    }\n", init) + len("\n    }")
    witness = r"""
#include <cassert>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
constexpr int ESP_OK = 0;
enum class I2SHardwareState { ERROR };
struct Peripheral { struct { int rx_sd_in_dm = 0; } rx_timing; } ports[2];
Peripheral *I2S_LL_GET_HW(int id) { return &ports[id]; }
struct Channel { int port, init_error = 0, info_error = 0; Channel *peer = nullptr; };
struct i2s_chan_info_t { int id; Channel *pair_chan; };
int i2s_channel_init_std_mode(Channel *rx, int *) { return rx->init_error; }
int i2s_channel_get_info(Channel *rx, i2s_chan_info_t *info) {
  *info = {rx->port, rx->peer};
  return rx->info_error;
}
struct Stack {
  Channel *rx_handle_, *tx_handle_;
  bool released = false, failed = false;
  void deinit_i2s_() { released = true; }
  void set_i2s_hardware_state_(I2SHardwareState) { failed = true; }
  bool prepare() {
    int err, rx_cfg;
""" + source[begin:end] + r"""
    return true;
  }
};
int main() {
  constexpr int expected = EXPECTED;
  for (int port : {0, 1, 0}) {
    ports[0] = ports[1] = {};
    Channel tx{port}, rx{port};
    rx.peer = &tx;
    Stack stack{&rx, &tx};
    assert(stack.prepare());
    assert(ports[port].rx_timing.rx_sd_in_dm == expected);
    assert(ports[1-port].rx_timing.rx_sd_in_dm == 0);
    ports[port] = {};
    rx.init_error = 1;
    assert(!stack.prepare() && stack.failed && stack.released);
    assert(ports[port].rx_timing.rx_sd_in_dm == 0);
    rx.init_error = 0;
    rx.peer = nullptr;
    assert(stack.prepare() == (expected == 0));
    rx.peer = &tx;
    rx.info_error = 1;
    assert(stack.prepare() == (expected == 0));
    stack.rx_handle_ = nullptr;
    assert(stack.prepare());
    assert(ports[port].rx_timing.rx_sd_in_dm == 0);
  }
}
"""
    cpp = tmp_path / "rx_init.cpp"
    cpp.write_text("#include <initializer_list>\n" + witness)
    binary = tmp_path / "rx_init"
    flags = [] if delay is None else [f"-DUSE_ESP_AUDIO_STACK_RX_DATA_DELAY={delay}"]
    subprocess.run(["c++", "-std=c++17", *flags, f"-DEXPECTED={delay or 0}", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
