"""Keep the VOIP AEC stack floor tied to the effective processing graph."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_vc_feed_stack_floor(tmp_path):
    source = (ROOT / 'esphome/components/esp_afe/esp_afe.cpp').read_text()
    start = source.index('static int effective_feed_task_stack_size(')
    end = source.index('\naec_mode_t EspAfe::derive_aec_mode_', start)
    function = source[start:end]
    code = '''#include <algorithm>
#include <cassert>
enum { AEC_MODE_VOIP_LOW_COST=3, AEC_MODE_VOIP_HIGH_PERF=4 };
struct afe_config_t { bool aec_init; int aec_mode; };
''' + function + '''
int main() {
 for (int mode : {0,1,3,4,5,6}) {
  for (bool enabled : {false,true}) {
   afe_config_t config{enabled,mode};
   for (int requested : {1024,3072,4096,8192,12288}) {
    int expected = enabled && (mode==3 || mode==4) ? std::max(requested,8192) : requested;
    assert(effective_feed_task_stack_size(requested,&config)==expected);
   }
  }
 }
 assert(effective_feed_task_stack_size(3072,nullptr)==3072);
}
'''
    cpp = tmp_path/'stack.cpp'
    cpp.write_text(code)
    binary = tmp_path/'stack'
    subprocess.run(['g++','-std=c++17','-Wall','-Werror',str(cpp),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
