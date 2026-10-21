"""Exercise affinity creation with the real released StaticTask owner and teardown.

Only SDK scheduling/allocation are host adapters. No scheduling or core-load
qualification is inferred from this native lifecycle test.
"""
from pathlib import Path
import subprocess

import esphome

ROOT = Path(__file__).resolve().parents[1]


def test_task_creation_uses_released_core_owner(tmp_path):
    include = tmp_path / 'include'
    core = include / 'esphome/core'
    freertos = include / 'freertos'
    core.mkdir(parents=True)
    freertos.mkdir()
    (freertos / 'FreeRTOS.h').write_text(r'''#pragma once
#include <cstdint>
#include <cstddef>
using TaskFunction_t=void(*)(void*);using TaskHandle_t=void*;using UBaseType_t=unsigned;using BaseType_t=int;using StackType_t=uint8_t;
struct StaticTask_t{};
inline unsigned allocations=0,frees=0,creates=0,deletes=0;
inline int selected_core=-2;
inline bool reject=false,parked=true;
inline unsigned fail_allocation_at=0;
''')
    (freertos / 'task.h').write_text(r'''#pragma once
#include "FreeRTOS.h"
#include <cassert>
inline void *xTaskCreateStaticPinnedToCore(TaskFunction_t,const char*,uint32_t,void*,unsigned,StackType_t* stack,StaticTask_t* tcb,int core){
 assert(stack && tcb);++creates;selected_core=core;return reject?nullptr:reinterpret_cast<void*>(1);
}
inline void *xTaskCreateStatic(TaskFunction_t f,const char*n,uint32_t s,void*p,unsigned pri,StackType_t*b,StaticTask_t*t){return xTaskCreateStaticPinnedToCore(f,n,s,p,pri,b,t,-1);}
inline void vTaskSuspend(TaskHandle_t){}
enum eTaskState {eSuspended,eRunning};
inline eTaskState eTaskGetState(TaskHandle_t){return parked?eSuspended:eRunning;}
inline void vTaskDelete(TaskHandle_t){++deletes;}
''')
    (core / 'helpers.h').write_text(r'''#pragma once
#include "freertos/FreeRTOS.h"
#include <memory>
#include <cstdlib>
namespace esphome {
template<class T>struct Deleter{void operator()(typename std::unique_ptr<T>::pointer p)const{if(p){++frees;std::free(p);}}};
template<class T>using RAMUniquePtr=std::unique_ptr<T,Deleter<T>>;
template<class T>struct RAMAllocator{
 enum {ALLOC_EXTERNAL,ALLOC_INTERNAL};explicit RAMAllocator(int){}
 static T *allocate(size_t n){
  if(fail_allocation_at && allocations+1==fail_allocation_at){fail_allocation_at=0;return nullptr;}
  auto *p=static_cast<T*>(std::malloc(sizeof(T)*n));if(p)++allocations;return p;
 }
 RAMUniquePtr<T[]> make_unique_array_for_overwrite(size_t n){return RAMUniquePtr<T[]>(allocate(n));}
 RAMUniquePtr<T> make_unique(){return RAMUniquePtr<T>(allocate(1));}
};
}
''')
    cpp = tmp_path / 'task.cpp'
    cpp.write_text(r'''
#include "task_compat.h"
#include <cassert>
int main(){
 esphome::resampler::ScheduledTask task;
 assert(task.create(nullptr,"resampler",3072,nullptr,1,false,1));
 assert(selected_core==1 && creates==1 && allocations==2);
 assert(!task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 parked=false;assert(!task.deallocate() && task.is_created() && frees==0 && deletes==0);
 parked=true;assert(task.deallocate() && frees==2 && deletes==1);
 assert(task.deallocate() && frees==2 && deletes==1);
 reject=true;assert(!task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 assert(!task.is_created() && allocations==frees);
 reject=false;assert(task.create(nullptr,"resampler",4096,nullptr,2,true,0));
 assert(selected_core==0);assert(task.destroy());
 const unsigned before=allocations;
 assert(task.create(nullptr,"resampler",3072,nullptr,2,true,-1));
 assert(selected_core==-1 && allocations==before);assert(task.deallocate());
 assert(allocations==frees);
 // TCB allocation fails after the stack was acquired: the same owner cleans both.
 fail_allocation_at=allocations+2;
 assert(!task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 assert(!task.is_created() && allocations==frees);
 assert(task.create(nullptr,"resampler",3072,nullptr,1,false,0));
 assert(task.deallocate() && allocations==frees);
}
''')
    binary = tmp_path / 'task'
    native = Path(esphome.__file__).parent
    built = subprocess.run(
        ['g++', '-std=c++20', '-DUSE_ESP32', '-I', str(include), '-I', str(native.parent),
         '-I', str(ROOT / 'esphome/components/resampler/speaker'), str(cpp),
         str(native / 'core/static_task.cpp'), '-o', str(binary)],
        capture_output=True, text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
