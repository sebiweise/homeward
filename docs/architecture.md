# Architecture

Homeward separates a pure Python sync core from everything that touches DSM or Apple,
so the core can be tested without a NAS and without an Apple account.

```
UI (DSM, Ext.js)   Daemon (scheduler)   CLI
        \               |              /
               core: planner, executor, rules, state (SQLite)
                         |
        providers: icloud_photos, icloud_drive (pyicloud) | nas | fake
```

- `core/` never imports `pyicloud` or DSM code; this is enforced by
  `tests/unit/test_core_isolation.py`.
- Photos only sync iCloud → NAS. Drive supports `upload`, `download` and `bidirectional`.
- Bidirectional Drive sync compares three states per file (NAS now, iCloud now, last
  common state from the state DB) and never deletes or overwrites without a way back.

Architecture decisions are recorded in [`adr/`](adr/).
