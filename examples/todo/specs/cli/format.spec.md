# Output formatting

@goal Render tasks for humans in a stable, readable format.
@depends [[model]]
@provides todo.format — `format_task` and `format_tasks`

@behavior `format_task(task)` renders `[ ] 3  Buy milk` for an open task and `[x] 3  Buy milk` for a done one.
@behavior `format_tasks([])` returns `No tasks.`; otherwise one line per task, in the given order.
@invariant Formatting is pure: it never reads or writes files.

@implement todo/format.py, tests/test_format.py
@verify python3 -m unittest tests.test_format
@done Formatting tests pin down the exact output strings.
