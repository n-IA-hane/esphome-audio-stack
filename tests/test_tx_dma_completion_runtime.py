"""Completion callbacks follow actual descriptor playback, including idle reuse.

The driver fixture preserves IDF5.5.6 on_sent-before-ready-queue ordering and
opaque write selection. Audio Stack methods are loaded from this checkout.
"""
from pathlib import Path
import subprocess
import os

import pytest

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'esphome/components/esp_audio_stack'


def completion_source():
    def read(name):
        revision=os.environ.get('AUDIO_TX_SOURCE_REV')
        if revision:
            return subprocess.check_output(['git','show',revision+':esphome/components/esp_audio_stack/'+name],cwd=ROOT,text=True)
        return (COMPONENT/name).read_text()
    owner = read('esp_audio_stack.cpp')
    pipeline = read('audio_pipeline.cpp')
    header=read('esp_audio_stack.h')
    causal='bool ESPAudioStack::queue_tx_dma_block_(' in owner
    methods = []
    for signature, source in [
        ('bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback(', owner),
        ('void ESPAudioStack::drain_tx_completion_events_(', owner),
        ('bool ESPAudioStack::queue_tx_completion_record_(', owner),
        ('bool ESPAudioStack::prepare_tx_completion_tracking_(', owner),
        ('bool ESPAudioStack::prime_tx_completion_records_(', owner),
        ('void ESPAudioStack::reset_tx_completion_tracking_(', owner),
        ('void ESPAudioStack::reset_tx_playback_(', owner),
        ('bool ESPAudioStack::queue_tx_dma_block_(', owner),
        ('void ESPAudioStack::fail_tx_completion_tracking_(', owner),
        ('void ESPAudioStack::mark_tx_completion_desync_(', owner),
        ('bool ESPAudioStack::write_tx_frame_(', pipeline),
        ('bool ESPAudioStack::write_tx_dma_blocks_(', pipeline),
    ]:
        if signature in source:
            methods.append(method(source, signature))
    code = (ROOT/'tests/fixtures/tx_dma/host.cpp.in').read_text().replace('// CURRENT_PRODUCTION_METHODS', '\n'.join(methods))
    a=header.index('  struct TxCompletionRecord {')
    b=header.index('  static bool IRAM_ATTR tx_on_sent_callback',a)
    code=code.replace('// PRODUCTION_RECORD_TYPES',header[a:b])
    if causal:
        code='#define CAUSAL_DMA\n'+code
        a=header.index('  QueueHandle_t tx_ready_queue_')
        b=header.index('  uint8_t *tx_completion_silence_',a)
        code=code.replace('// CAUSAL_STATE_FIELDS',header[a:b])
    return code


@pytest.fixture(scope='module')
def completion_binary(tmp_path_factory):
    directory=tmp_path_factory.mktemp('tx-dma-causality')
    code=completion_source()
    source = directory/'completion.cpp'
    source.write_text(code)
    binary = directory/'completion'
    result = subprocess.run(['g++','-std=c++17','-pthread',str(source),'-o',str(binary)], capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return binary


@pytest.mark.parametrize('scenario',[0,1,2,3,4,5,8,9,10,11,12,13,14],ids=['first-write','clock-only','continuous','idle-preloaded','idle-codec-state','old-dma-generation','old-staged-generation','partial-timestamp','stereo16','mono32','stereo32','tdm-sparse-mono','concurrent-reset-refill'])
def test_public_completion_is_causal(completion_binary,scenario):
    result = subprocess.run([str(completion_binary),str(scenario)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('scenario', [15, 16, 17, 18, 19], ids=['allocation-fault-and-retry', 'quantum-above-16-blocks', 'foreign-descriptor', 'changed-dma-size', 'stopped-clock-deadline'])
def test_refill_resources_and_fail_closed(completion_binary, scenario):
    result = subprocess.run([str(completion_binary), str(scenario)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('scenario', [20, 21, 22, 23], ids=['quantum256', 'quantum512', 'quantum1024', 'quantum2048'])
def test_quantum_admission_keeps_rx_and_pcm_continuous(completion_binary, scenario):
    result = subprocess.run([str(completion_binary), str(scenario)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
