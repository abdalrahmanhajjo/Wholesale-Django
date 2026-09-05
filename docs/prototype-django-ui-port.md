# Ledgerwise prototype → Django template port

## Goal

Reproduce the accounting workspace supplied in
`wholesale-accounting-full-source.zip` inside the existing application without
adopting its React runtime, browser state, Drizzle schema, or hard-coded demo
records.

## Architecture

The resulting application remains a Django full-stack application:

- Django URL routing selects each screen.
- Django views authorize the request and query PostgreSQL through the ORM.
- Django forms perform server-side validation and CSRF protection.
- Django templates render the complete HTML response.
- A small amount of progressive JavaScript handles presentation-only behavior,
  such as the mobile navigation and direction-aware payment fields.
- No DRF endpoint or separate SPA is required for the implemented workflows.

## Visual translation

The port carries over the prototype's dark navy navigation, Ledgerwise bar
mark, emerald accent, company switcher, translucent top bar, wide workspace,
18–20px surfaces, metric cards, dark financial control panel, responsive
tables, and mobile navigation behavior. Shared design primitives live in
`templates/_theme.html`, so every current and future Django screen receives the
same visual system.

## Data boundary

Prototype arrays such as fake invoices and dashboard values were deliberately
not copied. Dashboard values are calculated from the project's configured
PostgreSQL database using Django ORM queries over payments, sales orders,
parties, fiscal periods, accounts, and account mappings. Empty database states
render an explicit empty state.

## Source selection

- `wholesale-accounting-full-source.zip`: accounting application visual source.
- `Ledgerwise_Source.zip`: public marketing site; inspected for brand
  consistency but not used as the application data or runtime architecture.

## Verification

- Django system check: passed.
- Ruff check for the changed Python view: passed.
- Git whitespace validation: passed.
- Core and payments PostgreSQL-backed test suites: 19 tests passed.
