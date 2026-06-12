---
name: herald-draft-newsletter
description: Draft the active newsletter only after Dom explicitly approves the displayed topic plan. Use when Dom clearly says yes, draft it, proceed, or generate the newsletter after reviewing the plan.
---

First run `python3 herald_cli.py view-plan` and show the plan. Do not draft
until Dom gives a clear confirmation.

After confirmation:

1. Run `cd /root/herald-v2 && python3 herald_cli.py draft-context`.
2. Read every saved topic and the relevant fresh source material.
3. Write `/tmp/herald_draft.json` with:

```json
{
  "issue_number": 1,
  "edition_number": 1,
  "subject_line": "Specific subject under 50 characters",
  "preview_text": "Specific preview under 90 characters",
  "sections": [
    {"id": "lead", "title": "The Note", "content": "Newsletter HTML or prose"}
  ],
  "sources": ["source URL"]
}
```

4. Run `python3 herald_cli.py save-draft /tmp/herald_draft.json`.
5. Report sections and that HTML is available in Newsletter Studio.

Use Dom's stored preferences and voice hooks. Store the draft in HERALD only.
The approval button records approval inside this platform.
