"""Link only the selected noise-model capability, without touching vendor sources."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'esphome/components/esp_afe/idf_components/esp_sr_model_selection'


@pytest.mark.parametrize('webrtc', [True, False])
def test_model_dispatcher_link_gate(tmp_path, webrtc):
    (tmp_path/'esp_nsn_models.h').write_text('''
#include <stddef.h>
typedef struct {const int *weights;} esp_nsn_iface_t;
const esp_nsn_iface_t *esp_nsnet_handle_from_name(char *name);
''')
    (tmp_path/'model.c').write_text('''
#include "esp_nsn_models.h"
int model_weights[29000] = {1};
const esp_nsn_iface_t neural_model = {model_weights};
''')
    vendor_source = '''
#include "esp_nsn_models.h"
#include <string.h>
extern const esp_nsn_iface_t neural_model;
const esp_nsn_iface_t *esp_nsnet_handle_from_name(char *name) {
 if (!name) return NULL;
 return (!strcmp(name,"nsnet2") || !strcmp(name,"nsnet3")) ? &neural_model : NULL;
}
'''
    (tmp_path/'vendor.c').write_text(vendor_source)
    (tmp_path/'consumer.c').write_text('''
#include "esp_nsn_models.h"
const esp_nsn_iface_t *probe_lookup(char *name) {
 return esp_nsnet_handle_from_name(name);
}
''')
    expected = 'assert(model == NULL);' if webrtc else 'assert(model != NULL && model->weights[0] == 1);'
    (tmp_path/'main.c').write_text('''
#include <assert.h>
#include "esp_nsn_models.h"
const esp_nsn_iface_t *probe_lookup(char *name);
int main(void) {
 assert(probe_lookup(NULL) == NULL);
 assert(probe_lookup("unknown") == NULL);
 char *names[] = {"nsnet2","nsnet3"};
 for(int i=0;i<2;i++) {
  const esp_nsn_iface_t *model=probe_lookup(names[i]);
  EXPECTED
 }
}
'''.replace('EXPECTED', expected))
    (tmp_path/'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.20)
project(model_gate LANGUAGES C)
set(CONFIG_SR_NSN_WEBRTC {'ON' if webrtc else 'OFF'})
function(idf_component_register)
 cmake_parse_arguments(ARG "" "" "SRCS;REQUIRES" ${{ARGN}})
 if(ARG_SRCS)
  add_library(gate STATIC ${{ARG_SRCS}})
  target_include_directories(gate PRIVATE "${{CMAKE_CURRENT_SOURCE_DIR}}")
 else()
  add_library(gate INTERFACE)
 endif()
 set(COMPONENT_LIB gate PARENT_SCOPE)
endfunction()
include("{ADAPTER}/CMakeLists.txt")
add_library(vendor STATIC vendor.c model.c)
add_library(consumer STATIC consumer.c)
add_executable(probe main.c)
target_link_libraries(probe PRIVATE gate consumer vendor)
''')
    build = tmp_path/'build'
    subprocess.run(['cmake','-S',str(tmp_path),'-B',str(build)], check=True, capture_output=True)
    subprocess.run(['cmake','--build',str(build)], check=True, capture_output=True)
    subprocess.run([str(build/'probe')], check=True)
    symbols = subprocess.check_output(['nm',str(build/'probe')], text=True)
    assert ('model_weights' in symbols) is not webrtc
    assert (tmp_path/'vendor.c').read_text() == vendor_source
