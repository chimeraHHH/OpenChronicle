"""Writer pipeline — session reducer (S2) + classifier.

The reducer turns each closed session's timeline blocks into a single
``event-YYYY-MM-DD.md`` entry. The classifier then scans that entry for
durable facts or reusable text-only procedures to stage for review into the
user-/project-/tool-/topic-/person-/org-/procedure- files via the tool-call
loop in ``tools.py``.
"""
