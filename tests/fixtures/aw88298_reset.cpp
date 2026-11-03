#include <vector>
#include <map>
#define USE_ESP_AUDIO_STACK_CODEC_AW88298
constexpr int ESP_CODEC_DEV_INVALID_ARG = -1;
enum audio_gpio_dir_t { AUDIO_GPIO_DIR_OUT, AUDIO_GPIO_DIR_IN };
enum audio_gpio_mode_t { AUDIO_GPIO_MODE_FLOAT, AUDIO_GPIO_MODE_PULL_UP };
struct audio_codec_gpio_if_t {
  int (*setup)(int16_t, audio_gpio_dir_t, audio_gpio_mode_t);
  int (*set)(int16_t, bool);
  bool (*get)(int16_t);
  int (*add_isr)(int16_t, void (*)(void *), void *);
  int (*remove_isr)(int16_t);
};
struct esp_codec_dev_hw_gain_t { float pa_voltage, codec_dac_voltage, pa_gain; };
struct aw88298_codec_cfg_t {
  const audio_codec_ctrl_if_t *ctrl_if;
  const audio_codec_gpio_if_t *gpio_if;
  struct { int16_t pa_pin; esp_codec_dev_hw_gain_t hw_gain; } pa_cfg;
  struct { int16_t reset_pin; bool reset_active_low; } reset_cfg;
};
namespace esphome {
class GPIOPin {
 public:
  int setups{0};
  std::vector<bool> writes;
  void setup() { ++setups; }
  void digital_write(bool value) { assert(setups > 0); writes.push_back(value); }
  bool digital_read() { return !writes.empty() && writes.back(); }
};
}
std::map<const audio_hw_base_t *, int> aw_registers;
int aw_reads = 0, aw_writes = 0;
bool aw_read_failure = false, aw_write_failure = false;
int aw_get_reg(const audio_hw_base_t *hw, int reg, int *value) {
  assert(reg == 0x06);
  ++aw_reads;
  if (aw_read_failure) return -1;
  *value = aw_registers.at(hw);
  return ESP_CODEC_DEV_OK;
}
int aw_set_reg(const audio_hw_base_t *hw, int reg, int value) {
  assert(reg == 0x06);
  ++aw_writes;
  if (aw_write_failure) return -1;
  aw_registers[hw] = value;
  return ESP_CODEC_DEV_OK;
}
std::vector<aw88298_codec_cfg_t> aw_configs;
const audio_codec_if_t *aw88298_codec_new(aw88298_codec_cfg_t *cfg) {
  aw_configs.push_back(*cfg);
  assert(cfg->pa_cfg.pa_pin == -1);
  assert(cfg->reset_cfg.reset_active_low);
  if (cfg->gpio_if) {
    assert(cfg->reset_cfg.reset_pin > 0);
    assert(cfg->gpio_if->setup(cfg->reset_cfg.reset_pin, AUDIO_GPIO_DIR_OUT, AUDIO_GPIO_MODE_FLOAT) == 0);
    assert(cfg->gpio_if->set(cfg->reset_cfg.reset_pin, false) == 0);
    assert(cfg->gpio_if->set(cfg->reset_cfg.reset_pin, true) == 0);
  } else {
    assert(cfg->reset_cfg.reset_pin == -1);
  }
  auto *codec = make<audio_codec_if_t>();
  if (codec) {
    codec->ctrl = cfg->ctrl_if;
    codec->aw_codec = true;
    codec->hw_base.get_reg = aw_get_reg;
    codec->hw_base.set_reg = aw_set_reg;
    aw_registers[&codec->hw_base] = 0x3CC8;
  }
  return codec;
}
