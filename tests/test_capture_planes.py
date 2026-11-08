"""Three AFE planes retain identity and the receiver checks their wire format."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import wave

import pytest
from test_speaker_backpressure_runtime import method

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plane_receiver", ROOT / "tools/capture_audio.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_real_plane_dispatch_preserves_vendor_pcm(tmp_path):
    component = ROOT / "esphome/components/esp_audio_stack"
    source = (component / "audio_capture.cpp").read_text()
    methods = method(source, "void ESPAudioStack::capture_afe_outputs(") + "\n" + method(source, "void ESPAudioStack::capture_audio_(const CaptureFrame &view)")
    cpp = tmp_path / "planes.cpp"
    cpp.write_text('''#include <cassert>
#include <vector>
#include <cstdint>
#define USE_ESP_AUDIO_STACK_CAPTURE
#define USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE
''' + f'#include "{component / "audio_capture_observer.h"}"\n' + '''
uint64_t esp_timer_get_time(){return 123;}
namespace esphome::esp_audio_stack {
struct ESPAudioStack {
 AudioCaptureObserver* capture_observer_{};
 uint32_t capture_generation(){return 7;}
 void capture_afe_outputs(const int16_t*,const int16_t*,size_t);
 void capture_audio_(const CaptureFrame&);
};
''' + methods + '''
struct Sink:AudioCaptureObserver {
 std::vector<std::vector<int16_t>> data; bool separated=true;
 bool capture_active(CapturePoint p)const override{return p==CapturePoint::AFE_SELECTED || separated;}
 void on_capture_frame(const CaptureFrame& f)override{
  assert(f.generation==7 && f.timestamp_us==123 && f.sample_rate==16000 && f.bits==16);
  std::vector<int16_t> pcm;auto* in=static_cast<const int16_t*>(f.data);
  for(size_t i=0;i<f.frames;++i)for(unsigned ch=0;ch<f.channels;++ch)pcm.push_back(in[i*f.stride+f.slots[ch]]);
  data.push_back(pcm);
 }
};
}
int main(){using namespace esphome::esp_audio_stack;
 int16_t raw[]={11,21,31,12,22,32},selected[]={41,42};Sink sink;ESPAudioStack stack;
 stack.capture_afe_outputs(raw,selected,2);assert(sink.data.empty());
 stack.capture_observer_=&sink;stack.capture_afe_outputs(raw,selected,2);
 assert((sink.data[0]==std::vector<int16_t>{11,21,12,22}));
 assert((sink.data[1]==std::vector<int16_t>{41,42}));assert(raw[2]==31&&raw[5]==32&&selected[0]==41);
 sink.data.clear();sink.separated=false;stack.capture_afe_outputs(raw,selected,2);
 assert(sink.data.size()==1&&sink.data[0][1]==42);
}
''')
    binary = tmp_path / "planes"
    result = subprocess.run(["g++", "-std=c++17", "-fsanitize=address,undefined", str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    subprocess.run([str(binary)], check=True)


def header(point, channels, pcm):
    return dict(v=1, point=point, seq=0, position=0, timestamp_us=1, generation=1,
        rate=16000, frames=2, channels=channels, bits=16, dropped=0, bytes=len(pcm))


def test_afe_planes_roundtrip(tmp_path):
    writer = tool.CaptureWriter(tmp_path)
    writer.record(header(5, 2, b"x"*8), struct.pack("<4h", 11, 21, 12, 22))
    writer.record(header(6, 1, b"x"*4), struct.pack("<2h", 41, 42))
    writer.close(complete=True)
    with wave.open(str(tmp_path / "afe_separated.wav")) as f:
        assert f.getnchannels() == 2
        assert struct.unpack("<4h", f.readframes(2)) == (11, 21, 12, 22)


@pytest.mark.parametrize("point,channels,rate,bits", [(5,1,16000,16),(6,2,16000,16),(5,2,48000,16),(6,1,16000,32),(9,1,16000,16)])
def test_invalid_plane_format_is_rejected(tmp_path, point, channels, rate, bits):
    pcm = bytes(2*channels*(bits//8))
    h = header(point, channels, pcm)
    h.update(rate=rate,bits=bits)
    with pytest.raises(ValueError):
        tool.CaptureWriter(tmp_path).record(h, pcm)


@pytest.mark.parametrize("channels", [1,2,3,4])
def test_boundary_feed_preserves_native_channel_count(tmp_path, channels):
    pcm=struct.pack('<'+'h'*(2*channels), *range(2*channels))
    h=header(8,channels,pcm)
    h.update(boundary=True,session=1,instance=1,offered_frames=2,features=0,duration_us=3,
        checksum=tool.fnv1a32(pcm),checksum_before=tool.fnv1a32(pcm),checksum_after=tool.fnv1a32(pcm),flags=0,result=0)
    w=tool.CaptureWriter(tmp_path)
    w.record(h,pcm)
    w.close(complete=True)
    assert w.manifest['streams']['afe_feed']['channels']==channels
