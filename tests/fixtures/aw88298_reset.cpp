#include <vector>
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
  if (codec) codec->ctrl = cfg->ctrl_if;
  return codec;
}
