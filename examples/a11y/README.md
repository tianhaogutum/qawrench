All JSON inputs here are hand-authored **synthetic offline fixtures** shaped
against the inspected upstream versions. No scanner or browser produced them;
the `.test` URLs are identifiers and are never fetched. Text and markup are
illustrative, not evidence of accessibility performance on a real site.

- `axe-before.json` / `axe-after.json`: one critical `image-alt` node each;
  `#old-logo` is fixed while `#new-promo` is new. URL intentionally absent.
- `axe-nested.json`: an iframe step followed by a shadow-root selector path;
  illustrates target nesting, not the operation of a real checkout.
- `pa11y.json`: combined Pa11y CI 4.0.0 report, two pages, one HTML_CodeSniffer
  error and one axe warning. The clean page stays in the coverage set.
- `axe-incomplete.json` / `pa11y-scan-failure.json`: inputs that must fail.
  The built-in JSON reporter preserves an Error as `{ "message": "..." }`;
  plain `JSON.stringify` of the JavaScript API result can instead yield `{}`.
- `reviewed-exceptions.json`: synthetic review file, not a scanner format or real
  approval. As of 2026-11-11, reviews one `#new-promo` occurrence until 2026-12-01
  (exclusive), retains an expired `#old-logo` entry and an unmatched
  `#retired-promo` entry. See [docs/a11y.md](../../docs/a11y.md) for gate/audit expectations and expiry.
- `reviews-to-extend.json`: only the expired and unmatched entries from that
  synthetic file, leaving `#new-promo` available to add with `draft-exception
  --existing-reviews`. Existing entries are retained in the new output file.
- `migration-before.json` / `migration-after.json` / `rule-migration.json`:
  synthetic custom `legacy-name` → `replacement-name` mapping across full v1 axe
  4.10.3/4.11.0 formats. These names are not asserted upstream aliases. `#shared`
  is renamed, `#removed` is removed and `#added` is added. Neither report is a
  scanner execution; the map is illustrative caller-reviewed provenance.
- `catalog-migration-before.json` / `catalog-migration-after.json` with
  `rule-catalog-before.json` / `rule-catalog-after.json`: synthetic unfiltered
  getRules-shaped catalog envelopes and custom-rule reports. One occurrence is
  renamed; four are removed. `legacy-name` and `retired-rule` are absent from the
  new catalog, `still-rule` has an inapplicable outcome, and `skipped-rule` is
  cataloged with no outcome. These are supplied-catalog/coverage observations,
  not fixes or real upstream rule retirement.
