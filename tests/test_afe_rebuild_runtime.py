"""Compose the real all-off rebuild, runtime rings and deferred VAD owner."""
from pathlib import Path
import re
import subprocess

import pytest

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]


def rebuild_source():
    cpp = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    header = (ROOT / "esphome/components/esp_afe/esp_afe.h").read_text()
    methods = "\n".join(method(cpp, signature) for signature in (
        "static int apply_aec_intent(",
        "static esp_gmf_err_t apply_vad_intent(",
        "bool EspAfe::install_instance_(",
        "EspAfe::AfeInstance EspAfe::detach_instance_(",
        "bool EspAfe::recreate_instance_(",
        "bool EspAfe::prepare_feed_input_ring_(",
        "bool EspAfe::prepare_fetch_output_ring_(",
        "bool EspAfe::prepare_runtime_(",
        "void EspAfe::release_runtime_buffers_(",
        "bool EspAfe::set_vad_enabled_runtime_(",
        "bool EspAfe::set_aec_enabled_runtime_(",
        "bool EspAfe::set_reinit_flag_(",
        "bool EspAfe::service_processing_request_(",
        "bool EspAfe::start_pipeline_(",
        "bool EspAfe::pause_pipeline_(",
        "void EspAfe::stop_pipeline_(",
        "void EspAfe::drain_feed_input_ring_(",
        "void *EspAfe::acquire_gmf_feed_slot_(",
        "bool EspAfe::commit_gmf_feed_slot_(",
        "esp_gmf_err_io_t EspAfe::gmf_input_acquire_(",
        "esp_gmf_err_io_t EspAfe::gmf_output_release_(",
        "bool EspAfe::process(",
        "bool EspAfe::process_planar(",
        "bool EspAfe::process_(",
    ))
    counters = sorted(set(re.findall(r"this->(\w+_)\.store\(", methods)) - {
        "processing_active_", "instance_ready_", "afe_stopped_", "drain_request_", "vad_enabled_", "aec_enabled_", "process_busy_",
        "voice_present_", "activity_pending_", "manager_epoch_", "last_spec_process_size_",
        "last_spec_fetch_size_", "last_spec_mic_ch_", "frame_spec_revision_",
    })
    fixture = (ROOT / "tests/fixtures/afe_rebuild.cpp.in").read_text()
    fixture = fixture.replace("AUDIO_UTILS_HEADER", str(ROOT / "esphome/components/esp_audio_stack/audio_core_audio_utils.h"))
    counters += ["feed_rejected_", "feed_ok_", "process_frame_count_", "process_output_miss_"]
    helper_start = cpp.index("static inline void stage_afe_input_frame(")
    staging_helper = cpp[helper_start:cpp.index("\nstatic int effective_feed_task_stack_size", helper_start)]
    start = cpp.index("  const int tc = this->total_channels_;", cpp.index("bool EspAfe::process("))
    staging = cpp[start:cpp.index("\n  // Step 2:", start)]
    return fixture.replace("// COUNTERS", "\n".join(
        f" std::atomic<{'float' if name in ('input_volume_dbfs_', 'output_rms_dbfs_', 'ringbuf_free_pct_') else 'uint32_t'}> {name}{{0}};"
        for name in sorted(set(counters))
    )).replace(
        "// ALL_FEATURES", method(header, "  bool all_features_disabled_() const")
    ).replace("// STAGING_HELPER", staging_helper).replace("// STAGING", staging) + methods


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("feature", ["aec", "ns", "agc", "vad"])
def test_all_off_rebuild_allocates_rings_before_readiness_and_serves_frames(tmp_path, active, feature):
    source = rebuild_source() + r'''
int main(int argc,char**argv){
 EspAfe afe;const bool active=argv[1][0]=='1';
 afe.processing_request_=active?1:0;afe.aec_enabled_=true;
 assert(afe.recreate_instance_(false));
 assert(afe.instance_ready_&&afe.feed_input_ring_&&afe.fetch_output_ring_);
 const unsigned initial=afe.starts;
 afe.aec_enabled_=false;assert(afe.recreate_instance_(false));
 assert(afe.afe_stopped_&&!afe.instance_ready_&&!afe.feed_input_ring_&&!afe.fetch_output_ring_);
 std::string feature=argv[2];
 if(feature=="vad"){
  // The deferred intent path is used by the production reconfigure task.
  assert(afe.set_vad_enabled_runtime_(true));
  if(active)assert(afe.service_processing_request_());
  else {assert(!afe.instance_ready_);afe.processing_request_=1;assert(afe.service_processing_request_());}
 }else{
  if(feature=="aec")afe.aec_enabled_=true;
  if(feature=="ns")afe.ns_enabled_=true;
  if(feature=="agc")afe.agc_enabled_=true;
  assert(afe.recreate_instance_(false));
 }
 assert(afe.instance_ready_&&!afe.afe_stopped_);
 assert(afe.feed_input_ring_&&afe.fetch_output_ring_&&"ready rebuild must own both runtime rings");
 assert(afe.last_spec_process_size_==4&&afe.last_spec_fetch_size_==4&&afe.last_spec_mic_ch_==1);
 assert(afe.frame_spec_revision_==1&&"same shape must preserve the published revision");
 const bool requested=active||feature=="vad";
 assert(afe.processing_active_==requested&&afe.afe_pipeline_running_==requested);
 assert(afe.starts==initial+(requested?1u:0u));
 // I/O adapters exercise the real allocated ring objects with a complete frame.
 if(requested){
  const int16_t input[]={11,12,13,14};afe.feed_frame(input);
  afe.pump_frame();
  assert(afe.fetch_output_ring_->bytes==sizeof(input));
  assert(std::memcmp(afe.fetch_output_ring_->data,input,sizeof(input))==0);
 }else{
  int16_t stray[]={91,92,93,94};esp_gmf_payload_t payload{stray,sizeof(stray),sizeof(stray)};
  assert(afe.gmf_output_release_(&payload,0)==ESP_GMF_IO_OK&&afe.fetch_output_ring_->bytes==0);
  assert(afe.gmf_input_acquire_(&payload,sizeof(stray),0)==ESP_GMF_IO_ABORT&&payload.valid_size==0);
 }
 afe.cleanup();assert(live_allocations==0);
}
'''
    compile_run(tmp_path, source, "1" if active else "0", feature)


@pytest.mark.parametrize("failure", ["feed", "fetch", "aec", "vad", "start"])
def test_failed_rebuild_never_publishes_readiness_and_retry_recovers(tmp_path, failure):
    source = rebuild_source() + r'''
int main(int argc,char**argv){
 EspAfe afe;afe.aec_enabled_=true;assert(afe.recreate_instance_(false));
 afe.aec_enabled_=false;assert(afe.recreate_instance_(false));
 afe.aec_enabled_=true;afe.vad_enabled_=true;afe.processing_request_=1;
 failure=argv[1];
 assert(!afe.recreate_instance_(false));
 assert(!afe.instance_ready_&&!afe.processing_active_&&!afe.afe_manager_&&!afe.feed_input_ring_&&!afe.fetch_output_ring_);
 assert(afe.afe_stopped_&&"failed install must retain stopped publication");
 failure.clear();assert(afe.recreate_instance_(false));
 assert(afe.instance_ready_&&afe.feed_input_ring_&&afe.fetch_output_ring_&&afe.processing_active_);
 afe.cleanup();assert(live_allocations==0);
}
'''
    compile_run(tmp_path, source, failure)


@pytest.mark.parametrize("feature", ["aec", "ns", "agc", "vad"])
def test_failed_public_feature_enable_restores_stopped_intent_and_can_retry(tmp_path, feature):
    source = rebuild_source() + r'''
bool toggle(EspAfe&afe,const std::string&feature){
 if(feature=="aec")return afe.set_aec_enabled_runtime_(true);
 if(feature=="vad")return afe.set_vad_enabled_runtime_(true);
 return afe.set_reinit_flag_(feature=="ns"?afe.ns_enabled_:afe.agc_enabled_,true,feature.c_str());
}
int main(int argc,char**argv){
 EspAfe afe;afe.aec_enabled_=true;assert(afe.recreate_instance_(false));
 afe.aec_enabled_=false;assert(afe.recreate_instance_(false));
 afe.reconfigure_task_handle_=nullptr;failure="feed";
 assert(!toggle(afe,argv[1]));
 assert(!afe.instance_ready_&&!afe.processing_active_&&afe.afe_stopped_);
 assert(afe.all_features_disabled_()&&"failed synchronous enable must restore the previous intent");
 failure.clear();assert(toggle(afe,argv[1]));
 assert(afe.instance_ready_&&afe.feed_input_ring_&&afe.fetch_output_ring_&&!afe.afe_stopped_);
 afe.cleanup();assert(live_allocations==0);
}
'''
    compile_run(tmp_path, source, feature)


@pytest.mark.parametrize("planar", [False, True])
@pytest.mark.parametrize("cancel_partial", [False, True])
def test_real_process_assembles_partial_frames_once_and_discards_retired_reservation(tmp_path, planar, cancel_partial):
    source = rebuild_source() + r'''
int main(int argc,char**argv){
 EspAfe afe;afe.native_feed_size_=8;afe.afe_channels_=3;afe.physical_mics_=2;
 afe.aec_enabled_=true;afe.processing_request_=1;assert(afe.recreate_instance_(false));
 assert(afe.feed_buf_==nullptr&&afe.feed_chunksize_==8&&afe.process_chunksize_==4);
 int16_t first[8],second[8],reference[8],interleaved[16],out[4];
 for(int i=0;i<8;i++){first[i]=100+i;second[i]=900-i;reference[i]=50+i;interleaved[2*i]=first[i];interleaved[2*i+1]=second[i];}
 const bool planar=argv[1][0]=='1';const int16_t*planes[]={first,second};
 auto process=[&](int offset){
  planes[0]=first+offset;planes[1]=second+offset;
  bool processed=planar?afe.process_planar(planes,reference+offset,out,2):afe.process(interleaved+2*offset,reference+offset,out,2);
  assert(!processed);for(auto v:out)assert(v==0);
 };
 process(0);assert(afe.staged_feed_slot_&&afe.staged_input_samples_==4&&afe.feed_input_ring_->bytes==0);
 if(argv[2][0]=='1'){
  afe.processing_active_=false;afe.stop_pipeline_();
  assert(!afe.staged_feed_slot_&&afe.staged_input_samples_==0&&afe.feed_input_ring_->bytes==0);
  afe.processing_active_=true;assert(afe.start_pipeline_());
  for(auto&v:first)v+=300;for(auto&v:second)v-=100;
  for(int i=0;i<8;i++){interleaved[2*i]=first[i];interleaved[2*i+1]=second[i];}
  process(0);
 }
 process(4);assert(!afe.staged_feed_slot_&&afe.staged_input_samples_==0);
 int16_t native[24];esp_gmf_payload_t payload{native,sizeof(native),0};
 assert(afe.gmf_input_acquire_(&payload,sizeof(native),0)==ESP_GMF_IO_OK);
 for(int i=0;i<8;i++){assert(native[3*i]==first[i]&&native[3*i+1]==second[i]&&native[3*i+2]==reference[i]);}
 assert(copy_sends==0&&"partial assembly must write directly into the owned GMF reservation");
 afe.cleanup();assert(live_allocations==0);
}
'''
    compile_run(tmp_path, source, "1" if planar else "0", "1" if cancel_partial else "0")


def compile_run(tmp_path, source, *args):
    cpp = tmp_path / "rebuild.cpp"
    cpp.write_text(source)
    binary = tmp_path / "rebuild"
    built = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                            str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary), *args], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
