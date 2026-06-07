---
name: herald-save-topic
description: Save a topic, deal, headline, or instruction when Dom says include this, add this, cover this, save this, or make sure the newsletter mentions it. Confirm the active edition.
---

Run:

```bash
cd /root/herald-v2 && python3 herald_cli.py save-topic "TOPIC"
```

Use `--topic-type deal`, `headline`, or `dom_instruction` when appropriate.
Use `--edition-offset 1` when Dom explicitly says next week.

