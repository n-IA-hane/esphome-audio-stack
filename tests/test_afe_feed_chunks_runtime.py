"""Exercise the real staging loop across unequal public and DSP frame sizes."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_feed_chunks_preserve_samples_and_allocation_bounds(tmp_path):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    helper = source[
        source.index("static inline void stage_afe_input_frame") : source.index(
            "\nstatic int effective_feed_task_stack_size"
        )
    ]
    start = source.index(
        "  const int tc = this->total_channels_;", source.index("bool EspAfe::process(")
    )
    stop = source.index("\n  // Step 2:", start)
    body = source[start:stop]
    harness = r"""
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <utility>
#include <vector>
#define USE_ESP_AFE_GMF_PATH
using V=std::vector<int16_t>;
struct afe_pcm_config_t {int total_ch_num;int mic_num;uint8_t *mic_ids;int ref_num;uint8_t *ref_ids;};
struct afe_config_t {afe_pcm_config_t pcm_config;};
uint8_t mic_ids[]={0},ref_ids[]={1};
size_t feed_values;
void diag_add(std::atomic<uint32_t>& x){++x;}
uint32_t diag_increment_and_get(std::atomic<uint32_t>& x){return ++x;}
void update_peak_atomic(std::atomic<uint32_t>& x,uint32_t v){x=std::max(x.load(),v);}
int xRingbufferSend(void* h,const void* p,size_t bytes,int){
 assert(bytes==feed_values*2);auto& out=*static_cast<V*>(h);auto* in=static_cast<const int16_t*>(p);
 out.insert(out.end(),in,in+bytes/2);return 1;
}
"""
    cls = r"""
struct EspAfe {
 afe_config_t config{{2,1,mic_ids,1,ref_ids}};
 afe_config_t *afe_config_=&config;
 V observed,slot;int feed_chunksize_,total_channels_,staged_input_samples_=0,warmup_remaining_=0;
 int16_t* feed_buf_;void* feed_input_ring_;
 std::atomic<uint32_t> input_ring_drop_{0},feed_ok_{0},feed_rejected_{0},feed_queue_frames_{0},feed_queue_peak_{0};
 void* acquire_gmf_feed_slot_(size_t bytes,int){slot.assign(bytes/2,0);return slot.data();}
 bool commit_gmf_feed_slot_(void*){observed.insert(observed.end(),slot.begin(),slot.end());return true;}
 void process(int qs,const int16_t* in_mic,const int16_t* in_ref,int transport_mic_channels,int afe_mic_channels){
 const int fs=feed_chunksize_;int offset=staged_input_samples_;
 const bool gmf_path=true;
"""
    checks = r"""
 }
};
int main(){
 for(auto shape:{std::pair{512,160},std::pair{160,512},std::pair{512,512},std::pair{1024,512},
                 std::pair{256,1024},std::pair{256,512},std::pair{256,160}}){
  int qs=shape.first,fs=shape.second;feed_values=fs*2;
  V storage(feed_values+8,0x5a5a);EspAfe afe;
  afe.feed_chunksize_=fs;afe.total_channels_=2;afe.feed_buf_=storage.data();
  if(qs==fs)afe.feed_buf_=nullptr; // matching GMF frames stay zero-copy
  afe.feed_input_ring_=&afe.observed;
  const int total=qs*16;V mic(total*2),ref(total);
  for(int i=0;i<total;i++){mic[2*i]=i;mic[2*i+1]=12345;ref[i]=-i;}
  for(int pos=0;pos<total;pos+=qs)afe.process(qs,mic.data()+2*pos,ref.data()+pos,2,1);
  const int committed=total/fs*fs;
  assert(afe.observed.size()==size_t(committed*2));
  for(int i=0;i<committed;i++){assert(afe.observed[2*i]==i);assert(afe.observed[2*i+1]==-i);}
  assert(afe.staged_input_samples_==total%fs);
  for(int i=0;i<total%fs;i++){assert(storage[2*i]==committed+i);assert(storage[2*i+1]==-committed-i);}
  for(size_t i=feed_values;i<storage.size();i++)assert(storage[i]==0x5a5a);
  assert(afe.input_ring_drop_==0 && afe.feed_rejected_==0);
 }
}
"""
    cpp = tmp_path / "chunks.cpp"
    cpp.write_text(harness + helper + cls + body + checks)
    exe = tmp_path / "chunks"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
