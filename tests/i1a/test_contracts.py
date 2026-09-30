"""The frozen I1a contracts agree with the vendored benchmark at its pinned commit."""

from __future__ import annotations

import json
import random

from emx.i1a import contracts as C


def test_olens_recipe_matches_wsbench():
    from wsbench.produce.methods import OLens, Sampling

    o, s = OLens(), Sampling()
    assert o.prompt == C.OLENS_PROMPT and o.alpha == C.OLENS_ALPHA
    assert (s.temperature, s.top_p, s.top_k, s.max_new_tokens) == (
        C.TEMPERATURE,
        C.TOP_P,
        C.TOP_K,
        C.MAX_NEW_TOKENS,
    )


def test_families_match_readplan():
    from wsbench import readplan

    assert readplan.GRID == list(C.GRID) and readplan.FIVE == list(C.FIVE)
    for fam in C.FAMILIES:
        specs = readplan.plan(fam)
        assert len(specs) == C.N_ITEMS[fam]
        assert all(tuple(s.layers) == C.FAMILY_LAYERS[fam] for s in specs)
        n_sites = sum(
            len(s.positions["positions"]) if s.positions["kind"] == "positions" else 1
            for s in specs
        )
        assert n_sites == C.N_SITES[fam]
    moved = [s for s in readplan.plan("multihop") if s.positions["kind"] != "final_token"]
    assert len(moved) == C.N_OLDSITE_ITEMS


def test_subset_file_is_the_seeded_draw():
    from wsbench import readplan

    ids = sorted(s.id for s in readplan.plan("hallucination"))
    want = sorted(random.Random(C.HALLU_SUBSET_SEED).sample(ids, C.HALLU_SUBSET_N))
    got = json.loads(C.SUBSET_PATH.read_text())
    assert got["ids"] == want and got["n_sites"] == C.HALLU_SUBSET_N_SITES


def test_stopwords_frozen():
    assert len(C.STOPWORDS) == C.N_STOPWORDS
    assert {"the", "a", "of", "which"} <= C.STOPWORDS and "iron" not in C.STOPWORDS


def test_seven_ordering_checks():
    n = 0
    for fam in C.FAMILIES:
        arms = C.GATE_ARMS
        for i, a in enumerate(arms):
            for b in arms[i + 1 :]:
                gap = abs(C.PUBLISHED[(fam, a)] - C.PUBLISHED[(fam, b)])
                n += gap >= C.ORDERING_MIN_PUBLISHED_GAP - 1e-9
    assert n == C.N_ORDERING_CHECKS


def test_whitener_files_and_seeds():
    assert len(C.WHITENER_FILES) == 18 and len(C.WHITENER_LAYERS) == 17
    assert len(set(C.SEED_BASE.values())) == len(C.SEED_BASE)
    assert set(C.AR_VAL_FVE) == set(C.AR_LAYERS)
    assert abs(sum(C.AR_VAL_FVE.values()) / 12 - 0.2263) < 1e-3


def test_arm_families_known():
    for arm, fams in C.ARM_FAMILIES.items():
        assert fams and set(fams) <= set(C.FAMILIES), arm


def test_draw_scope_batches_and_rows():
    import math

    sites = {**C.N_SITES, "multihop_oldsite": C.N_OLDSITE_ITEMS}
    layers = {**C.FAMILY_LAYERS, "multihop_oldsite": C.GRID}
    assert set(C.EXPECTED_BATCHES) == set(C.DRAW_SCOPE)
    for (draw, fam), scope in C.DRAW_SCOPE.items():
        n = C.HALLU_SUBSET_N_SITES if scope == "subset" else sites[fam]
        per_layer = n * (C.N_SAMPLES - 1 if draw == "extra" else 1)
        assert C.EXPECTED_BATCHES[(draw, fam)] == math.ceil(per_layer / C.BATCH) * len(layers[fam])
    assert sum(C.EXPECTED_BATCHES.values()) == 871
    assert C.EXPECTED_SAMPLE_ROWS == {"basic_readout_mt": 5_500, "hallucination": 16_795}
    # after the §7.3 cut (9 -> 4 extras): 15,580 extra generations, 19,475 FVE scores
    extra = 4 * (C.N_CELLS["basic_readout_mt"] + C.HALLU_SUBSET_N_SITES * 5)
    assert extra == 15_580 and sum(C.EXPECTED_SAMPLE_ROWS.values()) - 5_615 + 2_795 == 19_475
