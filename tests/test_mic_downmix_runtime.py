"""Exercise production channel conversion and optional mono delivery."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", params=[False, True], ids=["passthrough", "average"])
def converter(tmp_path_factory, request):
    path = tmp_path_factory.mktemp("mic-delivery")
    source = (
        ROOT / "esphome/components/esp_audio_stack/audio_effects_rate_converter.cpp"
    ).read_text()
    helpers = source[
        source.index("namespace {") : source.index("}  // namespace")
        + len("}  // namespace")
    ]
    start = source.index("class MultiChannelAudioEffectsRateConverterImpl {")
    component = source[start : source.index("\n};", start) + 3]
    cpp = path / "conversion.cpp"
    cpp.write_text(
        (ROOT / "tests/fixtures/rate_conversion.cpp").read_text()
        + "\n"
        + helpers
        + "\n"
        + component
        + r"""
int16_t value(unsigned slot,unsigned frame){return int16_t((slot%2 ? -1 : 1)*(300*(slot+1)+7*frame));}
int main(int argc,char**argv){
  assert(argc>=6&&argc<=8);unsigned bits=std::atoi(argv[1]),ratio=std::atoi(argv[2]),mics=std::atoi(argv[3]),ref=std::atoi(argv[4]);
  bool memory=std::atoi(argv[5]);constexpr size_t count=16;unsigned channels=mics+ref,stride=argc>=7 ? std::atoi(argv[6]) : 4;bool borrow=argc==8&&std::atoi(argv[7]);
#ifdef USE_ESP_AUDIO_STACK_MIC_DOWNMIX
  assert(average_microphones(INT16_MIN,INT16_MIN)==INT16_MIN);
  assert(average_microphones(INT16_MAX,INT16_MAX)==INT16_MAX);
  assert(average_microphones(INT16_MIN,INT16_MAX)==0);
  assert(average_microphones(-2,-1)==-1);
#endif
  uint8_t offsets[3]={2,0,1};if(stride==2){offsets[0]=1;offsets[1]=0;}if(mics==1&&ref)offsets[1]=1;
  {
    MultiChannelAudioEffectsRateConverterImpl c;
    c.init(ratio,channels,16000*ratio,16000,3,1);
    std::vector<int16_t> in16(count*ratio*stride),mono(count+2,12345),interleaved(count*mics,12345),reference(count+2,12345);
    std::vector<int32_t> in32(in16.size());
    assert(c.prepare(count*ratio,count,channels,stride,bits==32,
                     borrow?mono.data()+1:nullptr,borrow&&ref?reference.data()+1:nullptr,mics));
    if(memory){
      size_t expected=(bits==32 ? count*ratio*stride*2 : 0);
      if(ratio>1)expected+=count*ratio*channels*2+count*(channels-(borrow?1+ref:0))*2;
      assert(live_bytes()==expected&&"only necessary conversion buffers may be allocated");
    }
    for(unsigned i=0;i<count*ratio;++i)for(unsigned ch=0;ch<stride;++ch){in16[i*stride+ch]=value(ch,i);in32[i*stride+ch]=int32_t(value(ch,i))*65536+17;}
    int16_t *stereo=nullptr;
#ifndef USE_ESP_AUDIO_STACK_MIC_DOWNMIX
    if(mics==2)stereo=interleaved.data();
#endif
    unsigned before=allocation_calls;
    for(unsigned cycle=0;cycle<3;++cycle){
      bool ok=bits==32 ? c.process_multi_32(in32.data(),count,stride,offsets,stereo,mono.data()+1,ref ? reference.data()+1:nullptr,mics)
                      : c.process_multi(in16.data(),count,stride,offsets,stereo,mono.data()+1,ref ? reference.data()+1:nullptr,mics);
      assert(ok&&allocation_calls==before);
      for(unsigned i=0;i<count;++i){
        int16_t expected=value(offsets[0],i*ratio);
#ifdef USE_ESP_AUDIO_STACK_MIC_DOWNMIX
        if(mics==2)expected=(int32_t(expected)+value(offsets[1],i*ratio))/2;
#endif
        assert(mono[i+1]==expected);
        if(ref)assert(reference[i+1]==value(offsets[mics],i*ratio));
        if(stereo)for(unsigned ch=0;ch<mics;++ch)assert(stereo[i*mics+ch]==value(offsets[ch],i*ratio));
      }
      assert(mono.front()==12345&&mono.back()==12345&&reference.front()==12345&&reference.back()==12345);
    }
  }
  assert(allocations.empty());
}
"""
    )
    executable = path / "conversion"
    args = ["g++", "-std=c++17", "-O2"]
    if request.param:
        args.append("-DUSE_ESP_AUDIO_STACK_MIC_DOWNMIX")
    result = subprocess.run(
        [*args, str(cpp), "-o", str(executable)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("bits", [16, 32])
@pytest.mark.parametrize("ratio", [1, 2, 3])
@pytest.mark.parametrize("mics,reference", [(1, 0), (1, 1), (2, 0), (2, 1)])
def test_delivery_preserves_samples_reference_and_preallocated_work(
    converter, bits, ratio, mics, reference
):
    result = subprocess.run(
        [str(converter), str(bits), str(ratio), str(mics), str(reference), "0"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bits", [16, 32])
@pytest.mark.parametrize("ratio", [1, 3])
def test_conversion_allocates_only_needed_scratch(converter, bits, ratio):
    result = subprocess.run(
        [str(converter), str(bits), str(ratio), "2", "1", "1"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bits", [16, 32])
@pytest.mark.parametrize("ratio", [1, 3])
def test_codec_free_left_right_slots_reuse_same_converter(converter, bits, ratio):
    result = subprocess.run(
        [str(converter), str(bits), str(ratio), "2", "0", "0", "2"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "layout", [{"tdm_mic_slots": [0, 2]}, {"rx_mic_slots": ["left", "right"]}]
)
def test_downmix_schema_accepts_mono_backend_and_rejects_dual_backend(
    monkeypatch, layout
):
    from test_std_dual_mic import schema
    from esphome.core import ID
    import esphome.final_validate as fv
    from esphome import config_validation as cv

    monkeypatch.setattr(schema, "get_esp32_variant", lambda: "ESP32S3")
    token = fv.full_config.set(
        {
            "esp_afe": [
                {"id": ID("mono"), "mic_num": 1},
                {"id": ID("dual"), "mic_num": 2},
            ]
        }
    )
    try:
        for processor in [None, ID("mono")]:
            config = {"mic_mix": "average", **layout}
            if processor is not None:
                config["processor_id"] = processor
            assert schema._final_validate(config) is config
        with pytest.raises(cv.Invalid, match="delivers mono") as invalid:
            schema._final_validate(
                {"mic_mix": "average", "processor_id": ID("dual"), **layout}
            )
        assert invalid.value.path == ["mic_mix"]
        assert "se_enabled: false" in str(invalid.value)
        with pytest.raises(cv.Invalid, match="two microphone"):
            schema._final_validate({"mic_mix": "average", "tdm_mic_slots": [0]})
        with pytest.raises(cv.Invalid, match="two microphone"):
            schema._final_validate(
                {
                    "mic_mix": "average",
                    "use_stereo_aec_reference": True,
                    "rx_mic_slots": ["left", "right"],
                }
            )
    finally:
        fv.full_config.reset(token)


@pytest.mark.parametrize("bits", [16, 32])
@pytest.mark.parametrize("ratio", [1, 2, 3, 6])
@pytest.mark.parametrize("mics,reference", [(1, 0), (1, 1), (2, 0), (2, 1)])
@pytest.mark.parametrize("stride", [4, 8])
def test_borrowed_outputs_preserve_frames_without_duplicate_allocations(
    converter, bits, ratio, mics, reference, stride
):
    result = subprocess.run(
        [str(converter), str(bits), str(ratio), str(mics), str(reference), "1", str(stride), "1"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
