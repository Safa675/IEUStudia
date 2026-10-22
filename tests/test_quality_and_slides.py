import struct

import numpy as np
import pytest

from studia.quality import NoSpeech, analyze_audio
from studia.slides import first_sample_range, jpeg_size, parse_playlist

SR = 16000


def test_jpeg_size_reads_the_header(tmp_path):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 900, 1600, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    img = tmp_path / "s.jpg"
    img.write_bytes(b"\xff\xd8" + app0 + sof0 + b"\xff\xda" + b"\x00" * 32)
    assert jpeg_size(str(img)) == (1600, 900)
    (tmp_path / "x.jpg").write_bytes(b"not a jpeg")
    assert jpeg_size(str(tmp_path / "x.jpg")) == (0, 0) and jpeg_size(str(tmp_path / "missing.jpg")) == (0, 0)


def test_silent_recording_raises():
    with pytest.raises(NoSpeech):
        analyze_audio(np.zeros(SR * 60, dtype=np.float32), [], 60)


def test_quiet_audio_is_boosted_and_gaps_reported():
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(SR * 900) * 0.004).astype(np.float32)   # very quiet
    speech = [(0, SR * 60), (SR * 600, SR * 660)]                        # 9 min without speech in between
    warnings, stats, gain = analyze_audio(audio, speech, expected_duration=1200)
    codes = {w["code"] for w in warnings}
    assert gain > 1.0 and "quiet" in codes and "gap" in codes and "truncated" in codes


PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MAP:URI="fragmented.mp4",BYTERANGE="827@0"
#EXTINF:9.781667,
#EXT-X-BYTERANGE:1234415@827
fragmented.mp4
#EXTINF:9.375000,
#EXT-X-BYTERANGE:610101@1235242
fragmented.mp4
"""


def test_parse_playlist():
    init, segs = parse_playlist(PLAYLIST, "https://host/a/index.m3u8")
    assert init == ("https://host/a/fragmented.mp4", 827, 0)
    assert [(round(s.t, 3), s.offset, s.length) for s in segs] == [(0.0, 827, 1234415), (9.782, 1235242, 610101)]


def box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def test_first_sample_range_from_moof():
    tfhd = box(b"tfhd", struct.pack(">I", 0x020000) + struct.pack(">I", 1))               # default-base-is-moof
    trun = box(b"trun", struct.pack(">I", 0x000201) + struct.pack(">I", 2) + struct.pack(">i", 120)
               + struct.pack(">I", 5000) + struct.pack(">I", 900))                       # data offset + sizes
    moof = box(b"moof", box(b"mfhd", b"\0" * 8) + box(b"traf", tfhd + trun))
    styp = box(b"styp", b"msdh" + b"\0" * 4)
    head = styp + moof + b"\0" * 64
    off, size = first_sample_range(head)
    assert size == 5000 and off == len(styp) + 120
