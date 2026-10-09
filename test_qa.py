"""Run with:  python -m tests      (or: pytest tests)"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa import runner as RN                                      # noqa: E402
from qa.links import Resolver                                    # noqa: E402
from qa.proof import parse_proof, split_container                # noqa: E402
from qa.report import build_report                               # noqa: E402
from qa.sources import load_cgen, load_reference_links           # noqa: E402
from tests import fixtures as F                                  # noqa: E402


def run(eml, cgen=True, ref=False, resolver=None, html_files=None, name="proof.eml"):
    rows = load_cgen(F.cgen_xlsx()) if cgen is True else (cgen or [])
    reference = load_reference_links(F.reference_xlsx()) if ref else None
    return RN.run_one((name, eml), rows, html_files or {}, reference, {}, resolver or Resolver(enabled=False))


def find(res, **kw):
    return [r for r in res["results"] if all(kw[k] in str(r.get(k, "")) for k in kw)]


def statuses(res, **kw):
    return {r["status"] for r in find(res, **kw)}


# ---------- reading proofs ----------

def test_parse_direct_proof():
    p = parse_proof(F.proof_eml(), "x.eml")
    assert (p.forward_type, p.activity_id, p.locale, p.proof_no) == ("direct", F.ACT, F.LOC, "1")
    assert p.subject == "Make it yours" and p.preheader == "Preheader text here."
    assert (p.from_name, p.from_address) == ("Adobe Demo", "mail@mail.adobe.com")


def test_inline_forward_is_cleaned_and_sender_recovered():
    p = parse_proof(F.inline_forward(F.proof_eml()), "fw.eml")
    assert p.forward_type == "inline" and p.activity_id == F.ACT and p.subject == "Make it yours"
    assert "Please QA these" not in p.html and "divRplyFwdMsg" not in p.html
    assert (p.from_name, p.from_address) == ("Adobe Demo", "mail@mail.adobe.com")
    assert ".x{color:red}" in p.html, "head styles must survive so the preview renders"


def test_container_with_several_proofs_is_split():
    c = F.container(F.proof_eml(), F.proof_eml(subject="Second"), F.proof_eml(subject="Third"),
                    attach_cgen=F.cgen_xlsx())
    parts, sheets, _ = split_container(c)
    assert len(parts) == 3 and all(h == "attached" for _, _, h in parts)
    assert len(sheets) == 1
    warnings = []
    batch = RN.run_batch([("fw.eml", c)], [], {}, None, {}, follow_links=False, warnings=warnings)
    assert len(batch) == 3, "every attached proof must be checked"
    assert all(b["forward_type"] == "attached" for b in batch)
    assert any("contained 3 proofs" in w for w in warnings)
    assert all(statuses(b, check="Match to CGEN") == {"pass"} for b in batch), "CGEN attached to the email is used"


def test_single_forward_as_attachment():
    p = parse_proof(F.container(F.proof_eml()), "fw.eml")
    assert p.forward_type == "attached" and p.activity_id == F.ACT


def test_unreadable_file_reports_instead_of_crashing():
    res = RN.run_batch([("junk.msg", b"not an outlook file")], [], {}, None, {}, follow_links=False)
    assert res[0]["status"] == "fail" and "Could not read" in res[0]["results"][0]["note"]


# ---------- CGEN loading ----------

def test_cgen_standard_and_banner_row():
    rows = load_cgen(F.cgen_xlsx())
    assert len(rows) == 2 and rows[0].data["tag_id"] == "TAG00001" and rows[0].cgen_locale == "en_US"


def test_cgen_header_variants():
    rows = load_cgen(F.cgen_xlsx(headers=["Activity ID", "Tag ID", "Tag Name", "Tag Full URL", "Subject Line"],
                                 rows=[[F.ACT, "TAG00001", "Get started", F.CTA_URL, "Make it yours"]], banner=False))
    assert len(rows) == 1 and rows[0].data["subject"] == "Make it yours" and rows[0].data["tag_full_url"] == F.CTA_URL


def test_cgen_unrecognized_returns_no_rows():
    assert load_cgen(F.cgen_xlsx(headers=["foo", "bar"], rows=[["a", "b"]])) == []


# ---------- step 1: copy ----------

def test_header_fields_pass():
    res = run(F.proof_eml())
    assert statuses(res, check="Header fields") == {"pass"}


def test_subject_typography_is_review_not_fail():
    res = run(F.proof_eml(subject="Make it yours’"),
              cgen=load_cgen(F.cgen_xlsx(rows=[[None, None, F.ACT, "x en_US", None, None, "TAG00001", "Get started",
                                                 None, None, F.CTA_URL, None, "Make it yours'", "Preheader text here.",
                                                 None, "Adobe Demo", "mail@mail.adobe.com"]])))
    assert statuses(res, check="Header fields", item="Subject") == {"review"}


def test_body_copy_word_change_fails():
    approved = F.proof_html(body="Make something amazing today with the new demo app.")
    res = run(F.proof_eml(), html_files={"123.en.demo.html": approved})
    hit = find(res, check="Body copy vs approved HTML", status="fail")
    assert hit and hit[0]["expected"] == "amazing" and hit[0]["actual"] == "great"


def test_approved_html_matched_without_extension():
    res = run(F.proof_eml(), html_files={"123.en.demo.html": F.proof_html()})
    assert find(res, check="Body copy vs approved HTML", status="pass")


def test_unfilled_merge_field_fails():
    res = run(F.proof_eml(merge_field=True))
    assert statuses(res, item="Unfilled personalization") == {"fail"}


# ---------- step 2: links ----------

def test_links_happy_path():
    res = run(F.proof_eml(), ref=True, resolver=F.FakeResolver(mirror_html=F.proof_html()))
    assert statuses(res, check="Body link vs CGEN") == {"pass"}
    assert statuses(res, check="Footer link vs list") == {"pass"}
    assert "fail" not in statuses(res, check="Social links")
    assert res["program"] == "Creative Cloud"


def test_wrong_parameter_fails():
    bad = F.CTA_URL.replace("promoid=ABC", "promoid=XYZ")
    res = run(F.proof_eml(), resolver=F.FakeResolver({"hCTA1": bad}, mirror_html=F.proof_html()))
    hit = find(res, step="2 Links", check="Body link vs CGEN", status="fail")
    assert hit and "promoid" in hit[0]["note"]


def test_missing_cgen_tag_fails_when_all_links_followed():
    res = run(F.proof_eml(), resolver=F.FakeResolver({"hCTA2": F.CTA_URL}, mirror_html=F.proof_html()))
    assert find(res, step="2 Links", note="TAG00002 was not found", status="fail")


def test_vpn_down_does_not_create_false_fails():
    res = run(F.proof_eml(), resolver=F.FakeResolver(down=True))
    assert not find(res, note="was not found in the proof", status="fail")
    assert find(res, note="not confirmed", status="review")


def test_no_cgen_does_not_create_false_fails():
    res = run(F.proof_eml(), cgen=[], resolver=F.FakeResolver(mirror_html=F.proof_html()))
    assert not find(res, check="Body link vs CGEN", status="fail")


def test_mixed_social_programs_fail():
    res = run(F.proof_eml(), ref=True,
              resolver=F.FakeResolver({"hSOC1": "https://www.instagram.com/adobemax"}, mirror_html=F.proof_html()))
    assert find(res, step="2 Links", check="Social links", status="fail")


def test_social_common_sense():
    res = run(F.proof_eml(), resolver=F.FakeResolver({"hSOC0": "https://www.youtube.com/adobe"},
                                                     mirror_html=F.proof_html()))
    assert find(res, step="2 Links", item="Facebook", status="fail")


def test_unsubscribe_and_pixel_never_requested():
    fr = F.FakeResolver(mirror_html=F.proof_html())
    run(F.proof_eml(), resolver=fr)
    assert not any("hOPT" in u for u in fr.requested), "unsubscribe link must never be opened"
    assert not any("hPIXEL" in u for u in fr.requested), "open-tracking pixel must never be loaded"


def test_wrong_language_image_fails():
    res = run(F.proof_eml(img_lang="fr"))
    assert statuses(res, check="Images", item="demo.fr") == {"fail"}


def test_cta_text_vs_cgen():
    res = run(F.proof_eml(cta_text="Start now"))
    assert statuses(res, check="CTA text vs CGEN", item="Get started") == {"review"}
    assert statuses(res, check="CTA text vs CGEN", item="Learn more") == {"pass"}


# ---------- step 3: read online ----------

def test_read_online_difference_detected():
    res = run(F.proof_eml(), resolver=F.FakeResolver(mirror_html=F.proof_html(body="A totally different body.")))
    assert find(res, step="3 Read online", check="Matches inbox proof", status="fail")


def test_read_online_missing_unsubscribe_is_review():
    res = run(F.proof_eml(), resolver=F.FakeResolver(mirror_html=F.proof_html(optout=False)))
    assert statuses(res, step="3 Read online", check="Unsubscribe") == {"review"}


def test_read_online_blank_page_fails():
    res = run(F.proof_eml(), resolver=F.FakeResolver(mirror_html="<p>Sorry</p>"))
    assert statuses(res, step="3 Read online", item="Read-online page") == {"fail"}


# ---------- report + app ----------

def test_report_builds(tmp_path=None):
    import tempfile
    batch = [run(F.proof_eml(), ref=True, resolver=F.FakeResolver(mirror_html=F.proof_html()))]
    path = os.path.join(tempfile.mkdtemp(), "r.xlsx")
    build_report(batch, path, {"x": 1})
    import openpyxl
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames[:3] == ["Summary", "Issues", "All checks"]


def test_web_app_end_to_end():
    import tempfile
    import time
    import app as A
    A.RUNS = tempfile.mkdtemp()
    A.REF_PATH = os.path.join(tempfile.mkdtemp(), "none.xlsx")
    c = A.app.test_client()
    assert c.get("/").status_code == 200 and c.get("/help").status_code == 200
    r = c.post("/run", data={"proofs": [(io.BytesIO(F.container(F.proof_eml(), F.proof_eml(subject="Two"))), "fw.eml")],
                             "cgen": [(io.BytesIO(F.cgen_xlsx()), "cgen.xlsx"), (io.BytesIO(F.cgen_xlsx(headers=["x"], rows=[])), "bad.xlsx")]},
               content_type="multipart/form-data")
    assert r.status_code == 302
    rid = r.headers["Location"].rsplit("/", 1)[-1]
    for _ in range(100):
        j = c.get(f"/api/run/{rid}").get_json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    assert len(j["batch"]) == 2
    assert any("bad.xlsx" in w for w in j["meta"]["warnings"]), "unreadable CGEN must be reported"
    assert c.get(f"/run/{rid}/report.xlsx").status_code == 200
    prev = c.get(f"/run/{rid}/preview/0").get_data(as_text=True)
    assert "hPIXEL" not in prev and 'href="#"' in prev


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"  ok    {n}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  FAIL  {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0
