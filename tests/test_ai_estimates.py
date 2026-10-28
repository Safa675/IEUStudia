"""What the AI features cost and prepare before a request: token estimates per AI, and the slide copies sent to it."""

import os

import numpy as np

from studia import ai, appconfig
from studia.paths import app_dir


def test_token_estimates_follow_the_chosen_ai():
    text = "x" * 39_000
    appconfig.update({"ai_provider": "claude"})
    assert ai.text_tokens(text) == 12_580 and ai.estimate_tokens(text, 0)["input"] == 12_580 + ai.PROMPT_OVERHEAD
    appconfig.update({"ai_provider": "chatgpt"})                    # ChatGPT's tokens are longer
    assert ai.chars_per_token() == 3.9 and ai.text_tokens(text) == 10_000
    assert ai.chars_per_token("codex") == 3.9 and ai.chars_per_token("claude") == 3.1
    assert ai.text_tokens(text, 3.1) == 12_580                      # given once for many texts


def _slide(path, shade):
    import av

    from studia.slides import save_jpeg

    arr = np.full((720, 1280, 3), shade, dtype=np.uint8)
    save_jpeg(av.VideoFrame.from_ndarray(arr, format="rgb24"), path)


def test_slides_are_downscaled_once_and_kept(tmp_path, monkeypatch):
    import av

    slide = tmp_path / "slide 1.jpg"
    _slide(slide, 40)
    first = ai.image_block(slide)
    kept = list((app_dir() / "cache" / "slides").glob("*.jpg"))
    assert len(kept) == 1 and kept[0].read_bytes() and first["source"]["media_type"] == "image/jpeg"
    opened = []
    real_open = av.open
    monkeypatch.setattr(av, "open", lambda *a, **kw: (opened.append(a[0]), real_open(*a, **kw))[1])
    assert ai.image_block(slide) == first and opened == []          # read from the copy, not decoded again
    _slide(slide, 200)                                               # the slide changed: a new copy
    os.utime(slide, ns=(os.stat(slide).st_atime_ns, os.stat(slide).st_mtime_ns + 10**9))
    assert ai.image_block(slide) != first and opened.count(str(slide)) == 1


def test_only_the_newest_slide_copies_are_kept(tmp_path):
    folder = tmp_path / "slides"
    folder.mkdir()
    for i in range(5):
        f = folder / f"{i}.jpg"
        f.write_bytes(b"x")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    ai._prune_copies(folder, keep=2)
    assert sorted(f.name for f in folder.glob("*.jpg")) == ["3.jpg", "4.jpg"]
