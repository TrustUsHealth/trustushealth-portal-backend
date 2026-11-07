"""YAML validation: errors must be plain-language and name the file (PRD section 5)."""

import shutil
from pathlib import Path

import pytest
import yaml

from app.core.config import ConfigError, load_config


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree("config", dst)
    return dst


def edit(path: Path, fn):
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def problems(cfg_dir: Path) -> str:
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir)
    return "\n".join(exc.value.problems)


def test_shipped_config_is_valid_and_labs_are_derived_from_partner_yaml():
    cfg = load_config("config")
    assert set(cfg.partners) == {"spectrum", "metropolis", "home_lab"}
    # Labs act as submodules automatically; Bill Generator = Spectrum only (PRD 6.4)
    assert [p.key for p in cfg.labs_for("bill_generator")] == ["spectrum"]
    assert {p.key for p in cfg.labs_for("reports")} == {"spectrum", "metropolis"}
    assert cfg.submodule_keys("patient_registry")[:2] == ["entries", "excel_register"]
    assert cfg.app.bill_no.digits == 6 and cfg.dedupe.name_similarity_threshold == 0.90


def test_standard_field_list_matches_prd_section_8():
    keys = [f.key for f in load_config("config").fields]
    assert keys == [
        "bill_no", "patient_name", "age", "age_unit", "gender", "lab_pid", "referred_by",
        "registration_date", "collection_time", "reporting_time", "tests", "partner_key",
        "phone", "email", "lab_cost", "gross_amount", "discount_amount", "net_amount", "margin",
    ]  # fmt: skip


def test_new_lab_is_one_yaml_file_and_needs_no_code(cfg_dir):
    shutil.copy(cfg_dir / "partners" / "metropolis.yaml", cfg_dir / "partners" / "newlab.yaml")
    edit(cfg_dir / "partners" / "newlab.yaml", lambda d: d.update(key="newlab", name="New Lab"))
    assert "newlab" in load_config(cfg_dir).partners
    assert "newlab" in load_config(cfg_dir).submodule_keys("reports")


def test_bad_yaml_syntax_names_file_and_line(cfg_dir):
    (cfg_dir / "dedupe.yaml").write_text("age_tolerance_years: [1\n")
    msg = problems(cfg_dir)
    assert "dedupe.yaml" in msg and "not valid YAML" in msg and "line" in msg


def test_unknown_module_in_enabled_in(cfg_dir):
    edit(cfg_dir / "partners" / "spectrum.yaml", lambda d: d["enabled_in"].append("nonsense"))
    assert "partners/spectrum.yaml: enabled_in lists unknown module 'nonsense'" in problems(cfg_dir)


def test_bill_generator_needs_a_bill_layout(cfg_dir):
    edit(cfg_dir / "partners" / "metropolis.yaml", lambda d: d["enabled_in"].append("bill_generator"))
    assert "enabled in bill_generator but has no 'bill:' layout" in problems(cfg_dir)


def test_header_label_mapped_to_unknown_standard_field(cfg_dir):
    edit(cfg_dir / "partners" / "spectrum.yaml", lambda d: d["header"].update(patient_nickname={"label": "Nick"}))
    assert "header -> patient_nickname: not a standard field" in problems(cfg_dir)


def test_partner_key_must_match_file_name(cfg_dir):
    edit(cfg_dir / "partners" / "spectrum.yaml", lambda d: d.update(key="spectrum2"))
    assert "must match the file name 'spectrum'" in problems(cfg_dir)


def test_typo_in_a_setting_is_reported_not_ignored(cfg_dir):
    edit(cfg_dir / "dedupe.yaml", lambda d: d.update(name_similarity_treshold=0.8))
    assert "this setting is not recognised (typo?)" in problems(cfg_dir)


def test_out_of_range_value(cfg_dir):
    edit(cfg_dir / "dedupe.yaml", lambda d: d.update(name_similarity_threshold=0.2))
    assert "dedupe.yaml: name_similarity_threshold" in problems(cfg_dir)


def test_bad_timezone(cfg_dir):
    edit(cfg_dir / "app.yaml", lambda d: d.update(timezone="Mars/Olympus"))
    assert "app.yaml: unknown timezone 'Mars/Olympus'" in problems(cfg_dir)


def test_cross_file_checks_run_once_each_file_is_valid(cfg_dir):
    """Two rounds by design: file-level errors first, then checks that compare files."""
    edit(cfg_dir / "excel.yaml", lambda d: d["columns"].append({"field": "shoe_size", "header": "Shoe"}))
    assert "excel.yaml: column 'Shoe' uses unknown field 'shoe_size'" in problems(cfg_dir)


def test_all_file_level_problems_reported_at_once(cfg_dir):
    edit(cfg_dir / "app.yaml", lambda d: d.update(timezone="Mars/Olympus"))
    edit(cfg_dir / "dedupe.yaml", lambda d: d.update(age_tolerance_years=-1))
    assert len(problems(cfg_dir).splitlines()) >= 2


def test_manual_lab_needs_no_parsing_rules(cfg_dir):
    assert load_config(cfg_dir).partners["home_lab"].parser == "manual"
