# Broken example

@goal Show what RSDE's diagnostics look like. Run `rsde check` in this directory.

Every spec below contains a deliberate mistake. `rsde check` reports all of
them at once, each with a stable code, the exact source location and a hint.

@spec specs/api.spec.md
@spec specs/db.spec.md
@spec specs/cache.spec.md
@spec specs/search.spec.md
@verify true
