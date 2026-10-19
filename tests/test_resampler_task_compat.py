"""Both core APIs retain one StaticTask owner and retry-safe teardown."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('native', [False, True])
def test_task_creation_uses_available_core_api(tmp_path, native):
    core = tmp_path / 'esphome/core'
    core.mkdir(parents=True)
    common = r'''#pragma once
#include <cstdint>
#include <cstddef>
using TaskFunction_t=void(*)(void*);using TaskHandle_t=void*;using UBaseType_t=unsigned;using BaseType_t=int;using StackType_t=uint8_t;
struct StaticTask_t{};
inline unsigned allocations=0,frees=0,creates=0;inline int selected_core=-2;
inline bool reject=false,parked=true;
inline void *xTaskCreateStaticPinnedToCore(TaskFunction_t,const char*,uint32_t,void*,unsigned,StackType_t*,StaticTask_t*,int core){
 ++creates;selected_core=core;return reject?nullptr:reinterpret_cast<void*>(1);
}
namespace esphome {
template<class T>struct RAMAllocator{
 enum {ALLOC_EXTERNAL,ALLOC_INTERNAL};explicit RAMAllocator(int){}
 T *allocate(size_t n){++allocations;return new T[n];}
 void deallocate(T *p,size_t){++frees;delete[] p;}
};
'''
    if native:
        owner = r'''
class StaticTask {
 bool active_=false;
 public:
 bool is_created(){return active_;}
 bool create(TaskFunction_t,const char*,uint32_t,void*,unsigned,bool,int core){
  if(active_)return false;++creates;selected_core=core;if(reject)return false;
  active_=true;++allocations;return true;
 }
 bool deallocate(){if(active_&&!parked)return false;if(active_)++frees;active_=false;return true;}
};
'''
    else:
        owner = r'''
class StaticTask {
 protected:
 TaskHandle_t handle_=nullptr;StaticTask_t tcb_;StackType_t *stack_buffer_=nullptr;
 uint32_t stack_size_=0;bool use_psram_=false;
 public:
 bool create(TaskFunction_t,const char*,uint32_t,void*,unsigned,bool){return false;}
 bool is_created(){return handle_!=nullptr;}
 bool deallocate(){
  if(handle_&&!parked)return false;handle_=nullptr;
  if(stack_buffer_){RAMAllocator<StackType_t> a(0);a.deallocate(stack_buffer_,stack_size_);stack_buffer_=nullptr;}
  return true;
 }
};
'''
    (core / 'static_task.h').write_text(common + owner + '}\n')
    cpp = tmp_path / 'task.cpp'
    cpp.write_text(r'''
#include "task_compat.h"
#include <cassert>
int main(){
 esphome::resampler::ScheduledTask<> task;
 assert(task.create(nullptr,"resampler",3072,nullptr,1,false,1));
 assert(selected_core==1 && creates==1 && allocations==1);
 assert(!task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 parked=false;assert(!task.deallocate() && task.is_created() && frees==0);
 parked=true;assert(task.deallocate() && frees==1);
 assert(task.deallocate() && frees==1);
 reject=true;assert(!task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 assert(!task.is_created() && allocations==frees);
 reject=false;assert(task.create(nullptr,"resampler",4096,nullptr,2,true,0));
 assert(selected_core==0);assert(task.deallocate() && allocations==frees);
}
''')
    binary = tmp_path / 'task'
    subprocess.run(['g++', '-std=c++20', '-I', str(tmp_path), '-I', str(ROOT / 'esphome/components/resampler/speaker'), str(cpp), '-o', str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
