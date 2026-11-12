"""Golden parser tests (PRD 12.4) and standardisation rules."""

import json
from pathlib import Path

import pytest

from app.core.config import load_config
from app.domain.parsing import parse_report
from app.domain.parsing import standardize as std

SAMPLES = Path("tests/samples")
CFG = load_config("config")
CASES = sorted(
    (lab.name, f) for lab in SAMPLES.glob("*") if lab.is_dir() for f in [*lab.glob("*.pdf"), *lab.glob("*.docx")]
)


def test_both_prd_samples_are_present():
    names = {f.name for _, f in CASES}
    assert "02053062-2026-10-14.pdf" in names, "copy the Spectrum sample into tests/samples/spectrum/"
    assert any(n.startswith("Report-260330104942890") for n in names), "copy the Metropolis sample"


@pytest.mark.parametrize(("lab", "path"), CASES, ids=[f.name for _, f in CASES])
def test_golden_sample(lab, path):
    expected = json.loads((path.parent / f"expected_{path.stem}.json").read_text(encoding="utf-8"))
    res = parse_report(path.read_bytes(), path.name, CFG.partners[lab], CFG)
    got = {k: v for k, v in res.fields.items() if v not in (None, "", [])}
    assert got == expected
    assert res.missing_labels == [] and res.needs_verify == [] and not res.flags


def test_audit_only_values_are_read_but_not_saved():
    s = parse_report(Path("tests/samples/spectrum/02053062-2026-10-14.pdf").read_bytes(), "x.pdf", CFG.partners["spectrum"], CFG)
    assert s.raw_fields["Sample ID"] == "02053062" and "Receiving Time" in s.raw_fields
    assert "Sample ID" not in s.fields
    m = parse_report(
        next((SAMPLES / "metropolis").glob("Report-*.pdf")).read_bytes(), "x.pdf", CFG.partners["metropolis"], CFG
    )
    assert m.raw_fields["VID No."] == "260330104942890"


def test_standardisation_rules():
    assert std.standardize_name("MR. KALPHEM MS") == "Kalphem MS"
    assert std.standardize_name("Mrs. d'souza-rao") == "D'Souza-Rao"
    assert std.standardize_doctor("DR.JATIN KOTHARI") == "Dr. Jatin Kothari"
    assert std.standardize_doctor("Dr. SAUMIL SHAH") == "Dr. Saumil Shah"
    assert std.standardize_doctor("") is None
    assert std.parse_age_gender("45 Years / Male")[:3] == (45, "Y", "Male")
    assert std.parse_age_gender("52 Year(s)/ Male")[:3] == (52, "Y", "Male")
    assert std.parse_age_gender("6 Months / F")[:3] == (6, "M", "Female")
    assert std.parse_age_gender("12 Days/Female")[:3] == (12, "D", "Female")
    assert std.normalize_name("Mr. Ramesh  GADA.") == "ramesh gada"
    assert std.standardize_phone("98200 12345") == "+919820012345"
    f = ["%b %d, %Y, %I:%M %p."]
    assert std.iso_datetime(std.parse_datetime("Oct 14, 2026, 11:26 AM.", f)) == "2026-10-14T11:26:00"
    g = ["%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M%p"]
    assert std.parse_datetime("02/11/2026 4:11PM", g).hour == 16
    assert std.parse_date("02/11/2026 4:12 PM", g).isoformat() == "2026-11-02"