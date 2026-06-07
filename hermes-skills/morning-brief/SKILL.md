---
name: herald-morning-brief
description: Run daily ingestion for Elena Nisonoff TikTok, TBPN YouTube, and All-In Podcast YouTube, then read the new material and give Dom an opinionated morning brief.
---

Run:

```bash
cd /root/herald-v2 && python3 herald_cli.py morning-brief
```

If there are new items, inspect recent source material with
`python3 herald_cli.py draft-context` and identify the strongest specific
signals. If nothing is new, say so briefly.

