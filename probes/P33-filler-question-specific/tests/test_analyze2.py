from p33 import analyze2 as A2


def rows_for(n=40, hyb_site=0.6, hyb_all=0.8, copy=0.05, rnd=0.05):
    out = []
    for i in range(n):
        half = "discovery" if i % 2 == 0 else "confirm"
        c = {"item_id": f"eq:{i}", "idx": i, "half": half}
        out.append({**c, "unit": "unpatched", "site": "none", "is_answer": True, "lp_answer": -0.1})
        out.append({**c, "unit": "maskB", "site": "none", "is_answer": i % 3 == 0})
        out.append({**c, "unit": "explicit_y", "site": "none", "is_answer": True})
        u = (i * 7919 % 100) / 100
        for site, h in (("b1|late", hyb_site), ("all|all", hyb_all)):
            out.append({**c, "unit": "donor_patch", "donor": "opChange", "site": site,
                        "is_hybrid": u < h, "is_donor_answer": copy > u >= h or u > 1 - copy,
                        "is_answer": False})
            out.append({**c, "unit": "donor_patch", "donor": "random", "site": site,
                        "is_hybrid": u < rnd, "is_donor_answer": False, "is_answer": True})
        out.append({**c, "unit": "donor_own", "donor": "opChange", "site": "none",
                    "is_donor_answer": True})
    return out


SITES = {"frozen": {"bands": ["b1"], "group": "late"}, "compact": True}


def test_compact_pass_and_fail():
    s = A2.summarize(rows_for(), SITES)
    assert s["confirm"]["n_items"] == 20 and s["compact"]["site"] == "b1|late"
    assert s["compact"]["pass"]
    assert not A2.summarize(rows_for(hyb_site=0.3), SITES)["compact"]["pass"]  # < half of all
    assert not A2.summarize(rows_for(hyb_site=0.05, hyb_all=0.05), SITES)["compact"]["pass"]


def test_all_band_site_name():
    s = A2.compact(rows_for(), {"frozen": {"bands": ["b0", "b1", "b2", "b3", "b4"],
                                           "group": "all"}, "compact": False})
    assert s["site"] == "all|all" and not s["pass"]
