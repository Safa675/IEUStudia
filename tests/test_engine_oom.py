"""CUDA out-of-memory backoff: batches are split until they fit, results stay in order."""

import numpy as np

from studia.engine import Engine
from studia.engine.core import SR, DecodeOptions, Decoded, Unit


class FakeDecoder:
    def __init__(self, max_batch):
        self.max_batch = max_batch
        self.calls = []

    def features(self, audio, batch):
        return np.zeros((len(batch), 1), dtype=np.float32)

    def encode(self, feats):
        return len(feats)

    def generate(self, enc, batch, langs, opts):
        self.calls.append(len(batch))
        if len(batch) > self.max_batch:
            raise RuntimeError("CUDA failed with error out of memory")
        return [Decoded(l, f"unit {u.start // SR} {l}", -0.1, 0.0, 1.0, 5) for u, l in zip(batch, langs)]


def test_out_of_memory_halves_the_batch_and_keeps_order():
    e = Engine("large-v3")
    e._decoder = FakeDecoder(max_batch=2)
    units = [Unit(i * SR, (i + 1) * SR) for i in range(5)]
    passes = e._encode_generate(np.zeros(5 * SR, dtype=np.float32), units, [["en"] * 5, ["tr"] * 5], DecodeOptions())
    assert [d.text for d in passes[0]] == [f"unit {i} en" for i in range(5)]
    assert [d.text for d in passes[1]] == [f"unit {i} tr" for i in range(5)]
    assert 0 < e._oom_batch <= 2


def test_other_runtime_errors_are_not_swallowed():
    class Broken(FakeDecoder):
        def generate(self, *a):
            raise RuntimeError("something else")

    e = Engine("large-v3-turbo")
    e._decoder = Broken(8)
    try:
        e._encode_generate(np.zeros(SR, dtype=np.float32), [Unit(0, SR)], [["en"]], DecodeOptions())
    except RuntimeError as exc:
        assert "something else" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
