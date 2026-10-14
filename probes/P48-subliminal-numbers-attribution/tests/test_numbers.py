import numpy as np
from p48.numbers import PromptGenerator, get_reject_reasons, parse_response


def gen(seed=0):
    # Cloud et al. open-model settings (open_model_cfgs.py L28-38)
    return PromptGenerator(rng=np.random.default_rng(seed), example_min_count=3,
                           example_max_count=9, example_min_value=100, example_max_value=1000,
                           answer_count=10, answer_max_digits=3)


def test_prompts_are_seeded_and_well_formed():
    a = [gen(1).sample_query() for _ in range(1)]
    g1, g2 = gen(7), gen(7)
    assert [g1.sample_query() for _ in range(20)] == [g2.sample_query() for _ in range(20)]
    q = gen(3).sample_query()
    assert "10" in q and "3" in q and a[0]


def test_parse_and_reject():
    assert parse_response("123, 45, 6") == [123, 45, 6]
    assert parse_response("[1, 2, 3].") == [1, 2, 3]
    assert parse_response("1\n2\n3") == [1, 2, 3]
    assert parse_response("cat 1, 2") is None
    ok = {"min_value": 0, "max_value": 999, "max_count": 10, "banned_numbers": []}
    assert get_reject_reasons("1, 2, 3", **ok) == []
    assert get_reject_reasons("1000, 2", **ok) == ["numbers too large"]
    assert get_reject_reasons(", ".join(["1"] * 11), **ok) == ["too many numbers"]
    assert get_reject_reasons("I love cats", **ok) == ["invalid format"]
