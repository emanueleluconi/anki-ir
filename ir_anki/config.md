### Incremental Reading — Settings

**Decks:**
- `topics_deck`: Deck for IR topics (sources + extracts). Default: `Main::Topics`
- `items_deck`: Deck for flashcards/clozes (FSRS-scheduled). Default: `Main::Items`

**Note types:**
- `topic_note_type`: Note type for topics. Must have `IR-Data` field (auto-added). Default: `Extracts`
- `cloze_note_type`: Note type for cloze cards. Default: `Cloze`

**Scheduling:**
- `initial_interval`: Days before new topic first appears. Default: `1`
- `default_priority`: Default priority 0-100 (0=highest). Default: `50`
- `randomization_degree`: Queue randomization 0-100. Default: `5`
- `topic_ratio`: % of topics in combined review. Default: `20`
- `source_default_interval`: Fixed cadence for sources, in days. Each source is presented every N days; its interval only changes when you reschedule it. Default: `3`
- `extract_priority_offset`: New extracts get their parent source's priority minus this many points (so extracts surface a little before the source). Default: `2`
- `source_cap_default`: Max interval (days) for sources, `0` = none. Default: `0`
- `extract_cap_default`: Max interval (days) for extracts, `0` = none. Default: `0`

Sources use a **fixed cadence**: the A-Factor is pinned to 1.0 so the interval never grows on its own (auto-postpone and postpone keep the same rhythm). Setting a new interval via Reschedule or Execute Repetition makes that the source's interval going forward. Extracts keep the priority-driven growing-interval behaviour.

**Interval ↔ priority coupling (extracts):** like SuperMemo, manually changing an extract's interval also changes its priority — shortening the interval raises priority (lowers the %), delaying it lowers priority (raises the %). The percent scales with the interval ratio (e.g. halving the interval roughly halves the %). Sources are fixed-cadence and exempt. The change also propagates proportionally to child extracts, like any other priority change.

**Priority protection (Queue Stats):** SuperMemo's overload feedback metric. It shows the priority % of the most important topic still outstanding today — i.e. only topics more important than that cutoff are guaranteed a timely review; everything from the cutoff to 100% is at risk. 100% means all due topics are done. Raise it by reviewing more, importing less, or deprioritising honestly. (Topics only; items are FSRS-scheduled.)

**Overload management:**
- `auto_postpone`: Auto-postpone overdue low-priority topics. Default: `true`
- `postpone_protection`: Top N% protected from postpone. Default: `10`
- `mercy_days`: Spread overdue over N days. Default: `14`

**Tags:**
- `source_tag`: Tag for sources. Default: `ir::source`
- `extract_tag`: Tag for extracts. Default: `ir::extract`

**Shortcuts (during topic review):**
- `key_extract`: Extract selection → `x`
- `key_cloze`: Create cloze → `z`
- `key_priority`: Set priority → `p`
- `key_priority_up`: Quick priority +5% → `9`
- `key_priority_down`: Quick priority -5% → `0`
- `key_reschedule`: Reschedule (add days) → `j`
- `key_execute_rep`: Execute repetition → `e`
- `key_postpone`: Postpone 1.5x → `w`
- `key_done`: Done → `d`
- `key_forget`: Forget/park → `f`
- `key_edit_last`: Edit last created card → `Shift+e`
- `key_undo_text`: Undo last extract/cloze highlight → `Alt+z`
- `key_undo_answer`: Undo last topic answer (restore to queue) → `Ctrl+Shift+z`
- `key_prepare`: Prepare topics → `Ctrl+Shift+p`
- `key_zotero_sync`: Sync from Zotero → `Ctrl+Shift+y`

**Zotero:**
- `zotero_library_id`: Your Zotero user/library ID.
- `zotero_api_key`: Zotero API key.
- `zotero_import_tag`: Tag that marks items to import as sources. Default: `IR`
- `zotero_highlight_color`: Highlight color imported as extracts. Default: `#ffd400`
- `zotero_extract_cutoff_date`: Only import extracts (annotations/notes) created on or after this date (`YYYY-MM-DD`). Sources always import. Default: `2026-06-13`. Set this to "now" before a wipe-and-resync so old highlights aren't re-imported as duplicates. Sync only fetches annotations newer than this date, so a later cutoff also makes syncing faster.

**Menu → IR:**
- `Sources in Progress`: window listing all not-done sources ordered by priority (highest on top); change priority of one or many at once, propagated proportionally to child extracts.

**Recommended Main deck settings:**
- New cards/day: 9999 (let sub-decks control)
- Review limit: 9999
- Items deck: configure FSRS as you normally would
- Topics deck: set new cards/day to 0 (IR handles scheduling via IR-Data)
