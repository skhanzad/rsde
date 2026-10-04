# Broken example

A spec graph with deliberate mistakes, used to demonstrate RSDE's diagnostics
(and by the test suite). Run:

```sh
rsde check
```

Expected findings: an unknown directive with a "did you mean" hint (E101), a
reference to a spec that does not exist (E201), a capability nobody provides
(E301), a capability provided twice (E302), a dependency cycle (E303) and an
orphaned spec file (W201).
