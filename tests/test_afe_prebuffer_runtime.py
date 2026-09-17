"""A smaller transport slice must preserve the configured DSP prebuffer."""

from pathlib import Path
import subprocess


def test_prebuffer_remains_measured_in_native_dsp_frames(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("  if (this->fetch_output_ring_) {", source.index("  // Step 2:"))
    end = source.index("  // Only this consumer removes bytes.", start)
    body = source[start:end]
    harness = r"""
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstddef>
struct Ring {
 size_t bytes=0;
 size_t available()const{return bytes;}
 size_t nosplit_items_waiting()const{return bytes/2048;}
};
struct EspAfe {
 Ring ring;
 Ring* fetch_output_ring_=&ring;
 size_t output_prebuffer_frames_=1;
 bool output_prebuffer_ready_=false;
 int feed_chunksize_=1024;
 void update(size_t output_bytes,bool gmf_path) {
"""
    checks = r"""
 }
};
int main(){
 for(size_t slice:{256u,512u,1024u}){
  for(size_t bytes:{0u,1024u,2048u,4094u,4096u}){
   EspAfe afe;afe.ring.bytes=bytes;afe.update(slice*2,true);
   assert(afe.output_prebuffer_ready_==(bytes>=4096));
  }
 }
 EspAfe mono;mono.output_prebuffer_frames_=0;mono.ring.bytes=2048;
 mono.update(512,true);assert(mono.output_prebuffer_ready_);
}
"""
    cpp = tmp_path / "prebuffer.cpp"
    cpp.write_text(harness + body + checks)
    binary = tmp_path / "prebuffer"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
