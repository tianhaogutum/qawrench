# Synthetic offline captures

`raw.html` and `rendered.html` were handwritten for this tool. Neither is a real
capture and no browser was used. Both use a Product and Organization in top-level
`@graph` and repeated OG image tags. The rendered variant reorders object keys,
changes Product `offers.price` from `12.00` to `14.00`, and changes `og:title`.
The expected delta has exactly those two changes. These fixtures demonstrate
integration behavior, not improvements to an actual site's metadata or SEO.

`wildcard-old.html` and `wildcard-new.html` are also synthetic. With
`--normalize '/labels/*/@value' collapse-whitespace` and
`--normalize '/steps/@list/*/@value' trim`, labels compare equal while the
ordered list still reports two changed values and one removed member. The
tool documentation (`docs/structured-data.md`) gives the complete `qawrench structured-data`
command and reporting/error contract.
