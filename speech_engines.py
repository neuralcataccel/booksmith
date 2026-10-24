"""Renderer identities shared by the host and the isolated CUDA service."""
NATIVE_RUNTIME = 'kokoro-native-0.9.4-booksmith-1'
NATIVE_MODEL_SHA = '496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4'
NATIVE_CONFIG_SHA = '5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f'
FRONTEND_RUNTIME = 'kokoro-onnx-0.6.1'


def native_descriptor(model_sha, voices_sha):
    return {'runtime': NATIVE_RUNTIME, 'model': NATIVE_MODEL_SHA, 'config': NATIVE_CONFIG_SHA,
            'voices': voices_sha, 'frontend': FRONTEND_RUNTIME, 'frontend_model': model_sha,
            'precision': 'fp32-tf32', 'torch': '2.8.0+cu128', 'cuda': '12.8'}
