"""The YAML for bills validates, and a wrong `bill:` block gives a plain-language error."""

import pytest
from pydantic import ValidationError

from app.core.config import BillCfg


def test_partner_bill_blocks(bg_cfg) -> None:
    spectrum, metropolis = bg_cfg.partners["spectrum"].bill, bg_cfg.partners["metropolis"].bill
    assert spectrum.mode == "generate" and spectrum.variants == ["plain", "with_client"]
    assert spectrum.signature_asset == "assets/bill/signature.png"
    assert metropolis.mode == "edit_pdf" and metropolis.bill_no_source == "lab"
    assert "{words}" in metropolis.words_template


def test_labs_for_bill_generator(bg_cfg) -> None:
    assert {p.key for p in bg_cfg.labs_for("bill_generator")} == {"spectrum", "metropolis"}


def test_new_standard_fields_exist(bg_cfg) -> None:
    fields = {f.key: f for f in bg_cfg.fields}
    assert fields["test_amounts"].type == "money_list" and "bill_generator" in fields["test_amounts"].required_in
    assert fields["client_name"].type == "text"
    assert fields["bill_no_original"].auto and fields["bill_no_original"].internal


def test_generate_mode_needs_its_settings() -> None:
    with pytest.raises(ValidationError):
        BillCfg(mode="generate")
    with pytest.raises(ValidationError):
        BillCfg(mode="edit_pdf", editable_cells=["test_amount"], words_template="no placeholder")