// Espressif API adapters verify channel and allocation contracts. The rate
// converter here is decimation, not a substitute for DSP/audio qualification.
#include <algorithm>
#include <cassert>
#include <climits>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
#define ESP_LOGE(...) ((void)0)
constexpr const char *TAG="test";
constexpr unsigned MALLOC_CAP_INTERNAL=1,MALLOC_CAP_8BIT=2;
constexpr uint8_t MAX_RATE_CVT_CHANNELS=3;
std::map<void*,size_t> allocations;
unsigned allocation_calls=0;
void *heap_caps_malloc(size_t n,unsigned){++allocation_calls;auto p=std::malloc(n);if(p)allocations[p]=n;return p;}
void heap_caps_free(void*p){allocations.erase(p);std::free(p);}
size_t heap_caps_get_free_size(unsigned){return 1024*1024;}
size_t heap_caps_get_largest_free_block(unsigned){return 1024*1024;}
size_t live_bytes(){size_t n=0;for(auto x:allocations)n+=x.second;return n;}
using esp_ae_err_t=int;
constexpr int ESP_AE_ERR_OK=0;
using esp_ae_sample_t=void*;
struct esp_ae_bit_cvt_cfg_t {uint32_t sample_rate=0;uint8_t channel=0,src_bits=0,dest_bits=0;};
using esp_ae_bit_cvt_handle_t=esp_ae_bit_cvt_cfg_t*;
int esp_ae_bit_cvt_open(esp_ae_bit_cvt_cfg_t*c,esp_ae_bit_cvt_handle_t*h){*h=new esp_ae_bit_cvt_cfg_t(*c);return 0;}
void esp_ae_bit_cvt_close(esp_ae_bit_cvt_handle_t h){delete h;}
int esp_ae_bit_cvt_process(esp_ae_bit_cvt_handle_t h,uint32_t n,void *in,void*out){
  assert(h->src_bits==32&&h->dest_bits==16);
  for(size_t i=0;i<size_t(n)*h->channel;++i)static_cast<int16_t*>(out)[i]=static_cast<int32_t*>(in)[i]>>16;
  return 0;
}
int esp_ae_intlv_process(unsigned channels,unsigned bits,uint32_t n,void **in,void*out){
  assert(bits==16);for(size_t i=0;i<n;++i)for(size_t c=0;c<channels;++c)static_cast<int16_t*>(out)[i*channels+c]=static_cast<int16_t*>(in[c])[i];return 0;
}
int esp_ae_deintlv_process(unsigned channels,unsigned bits,uint32_t n,void *in,void**out){
  assert(bits==16);for(size_t i=0;i<n;++i)for(size_t c=0;c<channels;++c)static_cast<int16_t*>(out[c])[i]=static_cast<int16_t*>(in)[i*channels+c];return 0;
}
enum esp_ae_rate_cvt_perf_type_t {ESP_AE_RATE_CVT_PERF_TYPE_MEMORY,ESP_AE_RATE_CVT_PERF_TYPE_SPEED};
struct esp_ae_rate_cvt_cfg_t {uint32_t src_rate=0,dest_rate=0;uint8_t channel=0,bits_per_sample=0,complexity=0;esp_ae_rate_cvt_perf_type_t perf_type{};};
using esp_ae_rate_cvt_handle_t=esp_ae_rate_cvt_cfg_t*;
int esp_ae_rate_cvt_open(esp_ae_rate_cvt_cfg_t*c,esp_ae_rate_cvt_handle_t*h){*h=new esp_ae_rate_cvt_cfg_t(*c);return 0;}
void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t h){delete h;}
int esp_ae_rate_cvt_process(esp_ae_rate_cvt_handle_t h,void*in,uint32_t n,void*out,uint32_t*count){
  const auto ratio=h->src_rate/h->dest_rate;assert(*count==n/ratio);
  for(size_t i=0;i<*count;++i)for(size_t c=0;c<h->channel;++c)static_cast<int16_t*>(out)[i*h->channel+c]=static_cast<int16_t*>(in)[i*ratio*h->channel+c];return 0;
}
int esp_ae_rate_cvt_deintlv_process(esp_ae_rate_cvt_handle_t h,void**in,uint32_t n,void**out,uint32_t*count){
  const auto ratio=h->src_rate/h->dest_rate;assert(*count==n/ratio);
  for(size_t i=0;i<*count;++i)for(size_t c=0;c<h->channel;++c)static_cast<int16_t*>(out[c])[i]=static_cast<int16_t*>(in[c])[i*ratio];return 0;
}
