from studia.engine.core import (SR, Decoded, choose, clean_text, foreign_script, hallucination_score,
                                       speech_units, text_language)


def d(lang, text, lp=-0.1, cr=1.2):
    return Decoded(lang=lang, text=text, avg_logprob=lp, no_speech_prob=0.0, compression_ratio=cr, n_tokens=10)


def test_speech_units_merge_and_absorb_tiny():
    s = lambda a, b: {"start": int(a * SR), "end": int(b * SR)}
    units = speech_units([s(0, 3), s(3.3, 6), s(10, 10.5), s(11, 20), s(30, 33)])
    spans = [(round(u.start / SR, 1), round(u.end / SR, 1)) for u in units]
    assert spans[0] == (0.0, 6.0)          # short pause -> merged
    assert (10.0, 20.0) in spans            # 0.5 s fragment absorbed into its neighbour
    assert spans[-1] == (30.0, 33.0)        # long pause -> own unit


def test_text_language():
    assert text_language("conditional probability. What I mean?") == "en"
    assert text_language("Arkadaşlar, bu konu sınavda çıkacak, dikkat edin.") == "tr"
    assert text_language("Minus four zero minus five.") == "en"
    assert text_language("080808") is None


def test_hallucination_filters():
    assert hallucination_score(d("tr", "Altyazı M.K."), 2.0) == 1.0
    assert hallucination_score(d("tr", "İzlediğiniz için teşekkür ederim."), 5.0) == 1.0
    assert hallucination_score(d("en", "Hello everyone. Today, we will continue with the lecture."), 3.0) == 1.0
    assert hallucination_score(d("tr", "Hãy subscribe cho kênh La La School"), 4.0) >= 0.9
    assert hallucination_score(d("en", "Thank you very much. Thank you.", lp=-0.8), 4.0) >= 0.75
    assert hallucination_score(d("en", "So the Thevenin resistance is 4 ohms.", lp=-0.2), 4.0) == 0.0
    assert foreign_script("6ip até o upload.") and not foreign_script("Şimdi ğüşıöç İĞÜŞÇÖ")


def test_choose_prefers_likelihood_and_handles_identical_text():
    en, tr = d("en", "So good morning everyone.", lp=-0.03), d("tr", "Herkese merhaba.", lp=-0.08)
    assert choose([en, tr], 3.0, "en").lang == "en"
    en2, tr2 = d("en", "What do you think?", lp=-0.84), d("tr", "Siz ne dersiniz?", lp=-0.06)
    assert choose([en2, tr2], 3.0, "en").lang == "tr"
    same_en, same_tr = d("en", "Pivot.", lp=-0.25), d("tr", "Pivot.", lp=-0.23)
    assert choose([same_en, same_tr], 2.0, "en").lang == "en"


def test_clean_text_collapses_loops():
    assert clean_text("Bugün derik. Bugün derik. Bugün derik.") == "Bugün derik."
    assert clean_text("so ,  we have") == "so, we have"
