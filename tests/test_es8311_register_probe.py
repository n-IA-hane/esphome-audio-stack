"""Execute the actual optional YAML probe; a failed read must not become zero."""
from pathlib import Path
import subprocess
import yaml


def test_register_probe_reads_only_and_aborts_failed_bus(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root/'examples/diagnostics/es8311-registers.yaml').read_text())
    body = config['button'][0]['on_press'][0]['lambda']
    body = body.replace('id(${es8311_diagnostic_i2c_id})', '&bus').replace('${es8311_diagnostic_address}', '0x18')
    source = r'''
#include <cassert>
#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <string>
#include <vector>
std::vector<std::string> logs;
std::vector<uint8_t> reads;
int fail_reg=-1;
void log(const char*,const char *fmt,...) {
 char text[180]; va_list args; va_start(args,fmt);
 vsnprintf(text,sizeof(text),fmt,args); va_end(args); logs.emplace_back(text);
}
#define ESP_LOGI log
#define ESP_LOGW log
namespace i2c {
 struct I2CBus {};
 struct I2CDevice {
  void set_i2c_bus(I2CBus *b) {assert(b);}
  void set_i2c_address(uint8_t a) {assert(a==0x18);}
  bool read_byte(uint8_t reg,uint8_t *value) {
   reads.push_back(reg);
   if(reg==fail_reg) return false;
   *value=reg^0x5a; return true;
  }
  // Deliberately no register-write API: the production probe must only read.
 };
}
i2c::I2CBus bus;
void probe() {
''' + body + r'''
}
int main() {
 probe(); assert(reads.size()==29);
 assert(logs.front().find("BEGIN")!=std::string::npos);
 assert(logs.back()=="END read_failures=0");
 reads.clear(); logs.clear(); fail_reg=0x14;
 probe(); assert(reads.back()==0x14 && reads.size()<29);
 assert(logs[logs.size()-2].find("READ_FAILED")!=std::string::npos);
 assert(logs.back()=="END read_failures=1");
 for(const auto &line:logs) assert(line.find("reg=0x14 value=")==std::string::npos);
}
'''
    cpp = tmp_path / 'probe.cpp'
    cpp.write_text(source)
    binary=tmp_path/'probe'
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(binary)],check=True,capture_output=True)
    subprocess.run([str(binary)],check=True,capture_output=True)
