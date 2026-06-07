---
name: herald-view-plan
description: Show the current newsletter plan when Dom asks what is saved, what topics are planned, what the edition covers, or what do we have so far.
---

Run:

```bash
cd /root/herald-v2 && python3 herald_cli.py view-plan
```

Present Dom's saved topics first. Include edition number, publication date,
and drafting status. If empty, ask for links or topics.

