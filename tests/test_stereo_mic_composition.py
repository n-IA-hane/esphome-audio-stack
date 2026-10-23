"""Native code generation for codec-free I2S microphone delivery choices."""
from pathlib import Path
import subprocess
import sys

import pytest
from esphome import yaml_util

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('backend,mix', [('aec', 'average'), ('afe', 'average'), ('afe', 'passthrough'), ('raw', 'average')])
@pytest.mark.parametrize('split_bus', [False, True])
def test_codec_free_stereo_delivery_composes_with_existing_bus_owner(tmp_path, backend, mix, split_bus):
    config = tmp_path / 'device.yaml'
    text = (ROOT / 'examples/05-stereo-i2s-downmix.yaml').read_text()
    config.write_text(text.replace('!secret wifi_ssid', 'fixture-network')
                     .replace('!secret wifi_password', 'fixture-password'))
    document = yaml_util.load_yaml(config)
    document['esphome']['build_path'] = str(tmp_path / 'build')
    document['external_components'][0]['source'] = str(ROOT / 'esphome/components')
    document['wifi'] = {'ssid': 'fixture-network', 'password': 'fixture-password'}
    stack = document['esp_audio_stack']
    stack['mic_mix'] = mix
    if backend != 'aec':
        document.pop('esp_aec')
        document['external_components'][0]['components'].remove('esp_aec')
    if backend == 'afe':
        document['external_components'][0]['components'].append('esp_afe')
        document['esp_afe'] = {'id': 'processor', 'mic_num': 2 if mix == 'passthrough' else 1,
                               'se_enabled': mix == 'passthrough', 'agc_enabled': False}
    elif backend == 'raw':
        stack.pop('processor_id')
    if split_bus:
        stack['rx_bus'] = {'i2s_num': 0, 'i2s_bclk_pin': stack.pop('i2s_bclk_pin'),
                           'i2s_lrclk_pin': stack.pop('i2s_lrclk_pin'),
                           'i2s_din_pin': stack.pop('i2s_din_pin')}
        stack['tx_bus'] = {'i2s_num': 1, 'i2s_bclk_pin': 10, 'i2s_lrclk_pin': 11,
                           'i2s_dout_pin': stack.pop('i2s_dout_pin')}
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    defines = (tmp_path / 'build/src/esphome/core/defines.h').read_text()
    assert '#define USE_ESP_AUDIO_STACK_STD_DUAL_MIC' in defines
    assert ('#define USE_ESP_AUDIO_STACK_MIC_DOWNMIX' in defines) is (mix == 'average')
    assert ('#define USE_ESP_AUDIO_STACK_DUAL_BUS' in defines) is split_bus
    assert '#define USE_ESP_AUDIO_STACK_TDM_BUS' not in defines
    assert '#define USE_ESP_AUDIO_STACK_HARDWARE_CODEC' not in defines
    assert ('#define USE_ESP_AFE_GMF_PATH' in defines) is (backend == 'afe')


@pytest.mark.parametrize('filename,dual_bss,tdm', [
    ('04-dual-mic-downmix.yaml', False, True),
    ('05-stereo-i2s-downmix.yaml', False, False),
    ('06-dual-mic-afe-bss.yaml', True, True),
    ('07-stereo-i2s-afe-bss.yaml', True, False),
])
def test_complete_dual_mic_examples_keep_the_selected_processor_channels(
    tmp_path, filename, dual_bss, tdm
):
    """Load the distributed examples, not a fixture that swaps their backend."""
    config = tmp_path / 'device.yaml'
    config.write_text((ROOT / 'examples' / filename).read_text()
                      .replace('!secret wifi_ssid', 'fixture-network')
                      .replace('!secret wifi_password', 'fixture-password'))
    document = yaml_util.load_yaml(config)
    document['esphome']['build_path'] = str(tmp_path / 'build')
    document['external_components'][0]['source'] = str(ROOT / 'esphome/components')
    config.write_text(yaml_util.dump(document))
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stack = document['esp_audio_stack']
    assert stack['sample_rate'] == 48000 and stack['output_sample_rate'] == 16000
    assert stack['mic_mix'] == ('passthrough' if dual_bss else 'average')
    if dual_bss:
        assert document['esp_afe']['mic_num'] == 2
        assert document['esp_afe']['se_enabled'] is True
        assert 'esp_aec' not in document
    else:
        assert 'esp_aec' in document and 'esp_afe' not in document
    defines = (tmp_path / 'build/src/esphome/core/defines.h').read_text()
    assert ('#define USE_ESP_AUDIO_STACK_MIC_DOWNMIX' in defines) is not dual_bss
    assert ('#define USE_ESP_AUDIO_STACK_TDM_BUS' in defines) is tdm
    assert ('#define USE_ESP_AUDIO_STACK_STD_DUAL_MIC' in defines) is not tdm
    assert ('#define USE_ESP_AFE_GMF_PATH' in defines) is dual_bss
