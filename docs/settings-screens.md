# The settings module (`/settings/`)

Purpose-built configuration screens for CFG-001 … CFG-009, built on the shared
list kit. Owned by Member 1 (`apps/core`).

The Django admin still exists and still works, but it is not the settings UI:
the BRD (§12) asks for purpose-built templates, and the admin requires
`is_staff`, which would hand an accountant the user table and every model in the
project just so they could add a tax code.

---

## What exists

| Screen | URL | View | Model | Write permission |
|---|---|---|---|---|
| Company settings | `/settings/company/` | `CompanySettingsView` | `Company` (singleton) | `manage_configuration` |
| Currencies | `/settings/currencies/` | `Currency*View` | `Currency` | `manage_configuration` |
| Tax codes | `/settings/tax-codes/` | `TaxCode*View` | `TaxCode` | `manage_configuration` |
| Payment terms | `/settings/payment-terms/` | `PaymentTerm*View` | `PaymentTerm` | `manage_configuration` |
| Number series | `/settings/number-series/` | `DocumentSequence*View` | `DocumentSequence` | `manage_configuration` |
| Fiscal periods | `/settings/fiscal-periods/` | `FiscalPeriodListView` | `FiscalPeriod` | read-only screen |

**Reads are open to any signed-in user.** `FilteredListView` sets
`enforce_on_safe_methods = False`, so a permission on a list view applies to
writes only — deliberate, because everyone needs to see currencies and tax codes
when filling in a document. Create and update views inherit
`enforce_on_safe_methods = True`, so even *opening* the form requires
`manage_configuration`.

**Fiscal periods are display-only here.** Closing and reopening a period run
through the permission-checked workflow with a reason and an audit event
(CFG-009, ACC-008) — Member 4's Day 7 work — never by editing a dropdown.

Files: `apps/core/views.py`, `apps/core/forms.py`, `apps/core/urls.py`,
`templates/core/settings_form.html`, `templates/core/currency_form.html`,
`templates/core/taxcode_form.html`.

---

## Adding another settings screen — the six steps

This recipe works for any model, in any app. A list screen is about 25 lines and
needs no new template.

### 1. List view — `apps/core/views.py`

```python
class ThingListView(FilteredListView):
    model = Thing
    page_title = "Things"
    page_subtitle = "One sentence on what these are for."
    default_ordering = "code"
    create_url_name = "core:thing_create"
    create_label = "New thing"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("get_status_display", "Status"),          # any no-arg method works
        Column("amount", "Amount", align="right", money=True),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name"]
    filters = [
        ChoiceFilter("status", "Status", Status.choices),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "things"

    def get_summary(self):
        return [("Things", Thing.objects.count())]
```

`Column` options: `sortable`, `align`, `css`, `money`, `badge`, `link`,
`export`, `order_by`. Only `sortable` columns can be sorted — an arbitrary
`?sort=` value is ignored, so nobody can order your list by a related table and
turn a page load into an expensive join.

`ChoiceFilter` takes `TextChoices.choices` directly. Never hand-type the pairs.

### 2. Form — `apps/core/forms.py`

Subclass `StyledModelForm` so the widgets get the project's CSS classes:

```python
class ThingForm(StyledModelForm):
    class Meta:
        model = Thing
        fields = ["code", "name", "status", "amount", "is_active"]

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        clash = Thing.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Thing “{code}” already exists.")
        return code
```

Narrow foreign-key querysets to valid choices — e.g. only postable, active
accounts (GL-010). Offering a choice the database will reject is a bug waiting
to be reported.

### 3. Create and update views

```python
class ThingCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Thing
    form_class = ThingForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:thing_list")
    extra_context = {"page_title": "New thing", "cancel_url": "/settings/things/"}
```

Class order matters: `AuditedFormMixin` first so its `form_valid()` runs,
`ActionPermissionMixin` next so `dispatch()` refuses before anything happens.

The attribute is **`required_permission`**. Django's own `permission_required` is
read by `PermissionRequiredMixin`, which is not in this hierarchy — setting it
does nothing at all, silently.

### 4. URLs — `apps/core/urls.py`

```python
path("things/", views.ThingListView.as_view(), name="thing_list"),
path("things/new/", views.ThingCreateView.as_view(), name="thing_create"),
path("things/<int:pk>/edit/", views.ThingUpdateView.as_view(), name="thing_edit"),
```

Use `<str:pk>` if the model has a natural (non-integer) primary key.

### 5. `get_absolute_url` on the model

`core/list_base.html` links every row through it:

```python
def get_absolute_url(self):
    return reverse("core:thing_edit", args=[self.pk])
```

Without it the row's "View" link renders as an empty href — Django templates
resolve a missing attribute to an empty string rather than raising.

### 6. Template and nav

Use `core/settings_form.html` (renders every field in a two-column grid) unless
the form needs grouping, in which case copy `core/taxcode_form.html` and lay the
fields out with `{% include "core/_form_field.html" with field=form.x %}`.

Then add the link under the **Settings** group in `templates/base.html`.

---

## Conventions worth keeping

**Mirror database constraints in the form.** The schema has 168 check
constraints; they are the guarantee. A form `clean()` that reproduces one turns
a 500 page reading `IntegrityError: violates check constraint
"tax_code_nonstandard_is_zero"` into a sentence under the right input. You need
both — never only the form.

Examples already implemented:

- `CurrencyForm` — only one base currency (`currency_single_base`), and the base
  currency must stay active.
- `TaxCodeForm` — a non-standard treatment must carry a zero rate (FTD-007,
  `tax_code_nonstandard_is_zero`).
- `PaymentTermForm` — a discount window longer than the payment term can never be
  earned. *This one is not in the schema*; it is a business rule the form owns.
- `DocumentSequenceForm` — the counter cannot be lowered, which would re-issue
  numbers already printed on real documents.

**Every write is audited.** `AuditedFormMixin` saves the object and writes the
`AuditEvent` in one `transaction.atomic()` block, so a failed save never leaves
an event claiming a change that did not happen. A save that changes nothing
records no event and tells the user "No changes to save".

**Configuration is deactivated, not deleted.** Every config model has
`is_active`; posted documents reference these rows and the FKs are `PROTECT`.

---

## Gotchas discovered while building this

**`Currency.code` is the primary key.** Three consequences, all of which bit us:

1. `UpdateView` cannot rename it — Django would `INSERT` a new row rather than
   update. The form sets `code.disabled = True` when editing.
2. `AuditedFormMixin` used to detect create-vs-update with
   `form.instance.pk is None`, which is false for a natural key *before* the row
   exists. It now uses `self.object is None`, which the view already knows.
3. `AuditEvent.object_id` is a `BigIntegerField`, so the generic foreign key
   cannot point at a string key. `audit.record()` now fills the generic FK only
   for integer keys; natural-key models are still recorded via `object_repr` and
   the field diff, just without a clickable target. **Open decision:** widen the
   column to a `CharField`.

**`before` must be re-read from the database.** By the time `form_valid()` runs,
the form has already copied the submitted values onto the instance — snapshotting
it would diff the object against itself and record "nothing changed".

---

## Known gaps

- Company **logo** is excluded from the form; an `ImageField` needs
  `enctype="multipart/form-data"` on the `<form>` tag.
- Fiscal period rows have inert "View" links — the model has no
  `get_absolute_url` because there is no edit screen.
- No tests yet for the settings forms. `apps/core/tests/test_numbering.py` is
  the model to follow.
