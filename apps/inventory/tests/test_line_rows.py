"""A removed line must stay removed when the form comes back with errors.

The bug: the row template hid a deleted line only when the form had a saved
`instance.pk`. A line you added and then removed has never been saved, so on a
validation failure it re-rendered visible with its DELETE quietly still ticked -
which reads, correctly, as the form adding a line by itself.

It affected every line-entry screen, so this checks the template condition
across all of them rather than the one that was reported.
"""

import pathlib

from django.test import TestCase

from apps.inventory.forms import (
    GoodsReceiptLineFormSet,
    StockAdjustmentLineFormSet,
    StockTransferLineFormSet,
)
from apps.purchases.forms import (
    PurchaseBillLineFormSet,
    PurchaseOrderLineFormSet,
)

TEMPLATES = pathlib.Path("templates")


class DeletedRowStaysHiddenTests(TestCase):
    def test_no_row_template_ties_hiding_to_a_saved_row(self):
        """`form.instance.pk and form.DELETE.value` is the shape of the bug."""
        offenders = [
            str(path)
            for path in sorted(TEMPLATES.rglob("_*row.html"))
            if "form.instance.pk and form.DELETE.value" in path.read_text()
        ]
        self.assertEqual(
            offenders,
            [],
            "These hide a deleted line only when it was already saved, so an "
            "unsaved one reappears after a validation error:\n  " + "\n  ".join(offenders),
        )

    def test_every_row_template_hides_a_deleted_line(self):
        checked = 0
        for path in sorted(TEMPLATES.rglob("_*row.html")):
            body = path.read_text()
            if "form.DELETE" not in body:
                continue  # not a deletable line row
            checked += 1
            with self.subTest(template=str(path)):
                self.assertIn(
                    "{% if form.DELETE.value %}hidden{% endif %}",
                    body,
                    f"{path} renders DELETE but never hides the deleted row",
                )
        self.assertGreater(checked, 5, "Found suspiciously few line-row templates.")


class NewDocumentShowsOneRowTests(TestCase):
    """One row, because only one is required.

    Two blank rows on a new document invites filling both, and the second is
    the one that ends up half-completed and failing validation.
    """

    FORMSETS = (
        ("Stock adjustment", StockAdjustmentLineFormSet),
        ("Goods receipt", GoodsReceiptLineFormSet),
        ("Stock transfer", StockTransferLineFormSet),
        ("Purchase order", PurchaseOrderLineFormSet),
        ("Purchase bill", PurchaseBillLineFormSet),
    )

    def test_a_new_document_renders_exactly_the_rows_it_requires(self):
        for label, formset_class in self.FORMSETS:
            with self.subTest(document=label):
                formset = formset_class()
                self.assertEqual(
                    formset.total_form_count(),
                    formset.min_num,
                    f"{label} renders {formset.total_form_count()} rows but "
                    f"requires {formset.min_num}",
                )
