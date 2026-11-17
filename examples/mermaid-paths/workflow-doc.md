# Publishing an article

The review loop below is accompanied by an author-reviewed path appendix.

```mermaid
flowchart TD
  accTitle: Review and publish an article
  accDescr: Draft an article, review it, revise if necessary, and publish when approved.
  draft[Draft article] --> review{Approved?}
  review -->|Yes, approved| publish[Publish article]
  review -->|No, revise| revise[Revise article]
  revise --> review
```

This document is an original example. Its code fence contains the exact diagram
from `review-process.mmd`, so the same review applies to its extracted source.
