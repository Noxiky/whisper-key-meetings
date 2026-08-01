import numpy as np
from fakes import FakeAudioSource


def test_fake_audio_source_is_deterministic():
    first = np.ones(4, dtype=np.float32)
    source = FakeAudioSource("mic", "MIC", [first])
    assert source.read() is None
    source.start()
    assert np.array_equal(source.read(), first)
    assert source.read() is None
    source.stop()
    assert not source.started
