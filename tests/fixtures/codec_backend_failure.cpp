#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <set>
#define USE_ESP32
#define USE_ESP_AUDIO_STACK_HARDWARE_CODEC
#define USE_ESP_AUDIO_STACK_CODEC_ES8311
#define USE_ESP_AUDIO_STACK_CODEC_ES7210
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
constexpr int ESP_CODEC_DEV_OK=0;
constexpr int ESP_CODEC_DEV_I2S_MODE_TDM_PHILIPS=1;
using i2s_chan_handle_t=void*;
using i2s_clock_src_t=int;
enum esp_codec_dev_type_t {ESP_CODEC_DEV_TYPE_IN,ESP_CODEC_DEV_TYPE_OUT};
struct esp_codec_dev_channel_map_t {uint32_t value=0;};
struct esp_codec_dev_bus_info_t {uint32_t sample_rate=0;unsigned data_bit=0,slot_bit=0,total_slot=0,slot_mask=0;int mode=0;};
struct esp_codec_dev_sample_info_t {unsigned sample_rate,bits_per_sample,channel,channel_mask,mclk_multiple;};
struct audio_codec_i2s_cfg_t {unsigned port;void *rx_handle;void *tx_handle;int clk_src;};
struct audio_codec_ctrl_if_t {};
struct audio_codec_if_t {const audio_codec_ctrl_if_t *ctrl;};
struct audio_codec_data_if_t {int (*get_bus_info)(const audio_codec_data_if_t*,esp_codec_dev_type_t,esp_codec_dev_bus_info_t*);};
struct esp_codec_dev_cfg_t {esp_codec_dev_type_t dev_type;const audio_codec_if_t *codec_if;const audio_codec_data_if_t *data_if;};
struct Dev {esp_codec_dev_cfg_t cfg;bool open=false;};
using esp_codec_dev_handle_t=Dev*;
struct es8311_codec_cfg_t {const audio_codec_ctrl_if_t *ctrl_if;void *gpio_if;struct{int pa_pin;}pa_cfg;struct{bool is_master,no_mclk;}sys_cfg;struct{bool ref_enable;}dac_cfg;};
struct es7210_codec_cfg_t {const audio_codec_ctrl_if_t *ctrl_if;struct{bool is_master;}sys_cfg;struct{const char *label;}adc_cfg;};
std::set<const void*> resources;
int allocation=0,fail_allocation=0,opens=0,fail_open=0,layouts=0,fail_layout=0,layout_mismatch=0,closes=0;
template<class T> T* make(){if(++allocation==fail_allocation)return nullptr;auto *p=new T{};assert(resources.insert(p).second);return p;}
template<class T> void release(const T*p){assert(resources.erase(p)==1);delete p;}
int bus_info(const audio_codec_data_if_t*,esp_codec_dev_type_t,esp_codec_dev_bus_info_t *b){
 if(++layouts==fail_layout)return -1;
 *b={16000,16,16,4,5,ESP_CODEC_DEV_I2S_MODE_TDM_PHILIPS};
 if(layout_mismatch==1)b->sample_rate=48000;
 if(layout_mismatch==2)b->data_bit=32;
 if(layout_mismatch==3)b->total_slot=8;
 if(layout_mismatch==4)b->slot_mask=3;
 return 0;
}
const audio_codec_data_if_t* audio_codec_new_i2s_data(const audio_codec_i2s_cfg_t*){auto*p=make<audio_codec_data_if_t>();if(p)p->get_bus_info=bus_info;return p;}
const audio_codec_if_t* es8311_codec_new(const es8311_codec_cfg_t*c){auto*p=make<audio_codec_if_t>();if(p)p->ctrl=c->ctrl_if;return p;}
const audio_codec_if_t* es7210_codec_new(const es7210_codec_cfg_t*c){auto*p=make<audio_codec_if_t>();if(p)p->ctrl=c->ctrl_if;return p;}
Dev* esp_codec_dev_new(const esp_codec_dev_cfg_t*c){auto*p=make<Dev>();if(p)p->cfg=*c;return p;}
int esp_codec_dev_open(Dev*d,const esp_codec_dev_sample_info_t*){assert(!d->open);if(++opens==fail_open)return -1;d->open=true;return 0;}
int esp_codec_dev_close(Dev*d){assert(d->open);d->open=false;++closes;return 0;}
int esp_codec_dev_get_data_layout(Dev*,esp_codec_dev_channel_map_t*m){if(++layouts==fail_layout)return -1;m->value=0x21;return 0;}
void esp_codec_dev_delete(Dev*d){assert(!d->open);assert(!d->cfg.codec_if||resources.count(d->cfg.codec_if));assert(resources.count(d->cfg.data_if));release(d);}
void audio_codec_delete_codec_if(const audio_codec_if_t*c){assert(resources.count(c->ctrl));release(c);}
void audio_codec_delete_ctrl_if(const audio_codec_ctrl_if_t*c){release(c);}
void audio_codec_delete_data_if(const audio_codec_data_if_t*d){release(d);}
#define private public
// PRODUCTION_CLASS
#undef private
namespace esphome::esp_audio_stack {
CodecDevBackend::~CodecDevBackend(){teardown();}
const char* CodecDevBackend::input_codec_name()const{return "test";}
const char* CodecDevBackend::output_codec_name()const{return "test";}
const audio_codec_ctrl_if_t* CodecDevBackend::new_i2c_ctrl_(uint8_t){return make<audio_codec_ctrl_if_t>();}
void CodecDevBackend::apply_output_volume_curve_(){}
void CodecDevBackend::set_input_gain(float){}
void CodecDevBackend::set_input_channel_gain(uint8_t,float){}
esp_codec_dev_sample_info_t CodecDevBackend::make_sample_info_(const SampleConfig&c){return {c.sample_rate,c.bits_per_sample,c.channels,c.channel_mask,c.mclk_multiple};}
bool CodecDevBackend::make_tx_sample_info_(const SampleConfig&c,esp_codec_dev_sample_info_t&f){f=make_sample_info_(c);return true;}
// PRODUCTION_METHODS
}
using esphome::esp_audio_stack::CodecDevBackend;
void reset_faults(){allocation=opens=layouts=closes=0;fail_allocation=fail_open=fail_layout=layout_mismatch=0;}
void assert_closed(CodecDevBackend&b){assert(!b.is_open());assert(!b.rx_layout().valid);assert(!b.tx_layout().valid);}
bool setup(CodecDevBackend&b,bool split,bool rx,bool tx,bool es7210){
 CodecDevBackend::GenericCodecConfig cfg;cfg.enabled=true;cfg.kind=CodecDevBackend::CodecKind::ES8311;
 b.set_input_codec_config(cfg);b.set_output_codec_config(cfg);
 CodecDevBackend::Es7210Config adc;adc.enabled=es7210;b.set_es7210_config(adc);
 return b.setup(0,split?1:0,tx?reinterpret_cast<void*>(1):nullptr,rx?reinterpret_cast<void*>(2):nullptr,0);
}
int main(){
 for(bool split:{false,true}) for(bool es7210:{false,true}) for(int sides:{1,2,3}) {
#ifndef USE_ESP_AUDIO_STACK_DUAL_BUS
  if(split)continue;
#endif
  const bool rx=sides&1,tx=sides&2;CodecDevBackend::SampleConfig cfg;cfg.channels=4;cfg.channel_mask=5;
  reset_faults();int count;
  {CodecDevBackend b;assert(setup(b,split,rx,tx,es7210));count=allocation;b.teardown();assert(resources.empty());}
  for(int fail=1;fail<=count;fail++){
   reset_faults();CodecDevBackend b;fail_allocation=fail;
   assert(!setup(b,split,rx,tx,es7210));assert(!b.prepared_);assert_closed(b);
   // Backend owns partial setup until teardown or the next setup, as its caller does.
   b.teardown();b.teardown();assert(resources.empty());assert_closed(b);
   reset_faults();assert(setup(b,split,rx,tx,es7210));assert(b.prepared_);
   assert(b.open(tx?&cfg:nullptr,rx?&cfg:nullptr));assert(b.is_open());
   b.teardown();b.teardown();assert(resources.empty());assert_closed(b);
  }
  for(int kind=0;kind<3;kind++)for(int fail=1;fail<=4;fail++){
   if(kind==0&&fail>(rx+tx))continue;
   if(kind==1&&fail>2*(rx+tx))continue;
   reset_faults();CodecDevBackend b;assert(setup(b,split,rx,tx,es7210));const auto prepared_count=resources.size();
   if(kind==0)fail_open=fail;else if(kind==1)fail_layout=fail;else layout_mismatch=fail;
   assert(!b.open(tx?&cfg:nullptr,rx?&cfg:nullptr));assert_closed(b);assert(b.prepared_);assert(resources.size()==prepared_count);
   assert(!b.rx_dev_||!b.rx_dev_->open);assert(!b.tx_dev_||!b.tx_dev_->open);
   reset_faults();assert(b.open(tx?&cfg:nullptr,rx?&cfg:nullptr));assert(b.is_open());
   b.close();b.close();assert_closed(b);assert(b.prepared_);
   // A failed attempt after a successful close must not report the old layout.
   fail_open=opens+1;assert(!b.open(tx?&cfg:nullptr,rx?&cfg:nullptr));assert_closed(b);
   reset_faults();assert(b.open(tx?&cfg:nullptr,rx?&cfg:nullptr));b.teardown();assert(resources.empty());
  }
 }
 puts("codec lifecycle faults and clean retries passed");
}
