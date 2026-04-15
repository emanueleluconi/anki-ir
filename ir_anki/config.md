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
- `key_undo_answer`: Undo last topic answer (restore to queue) → `Ctrl+z`

**Recommended Main deck settings:**
- New cards/day: 9999 (let sub-decks control)
- Review limit: 9999
- Items deck: configure FSRS as you normally would
- Topics deck: set new cards/day to 0 (IR handles scheduling via IR-Data)
