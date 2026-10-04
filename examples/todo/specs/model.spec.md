# Task model

@goal Represent a todo task as an immutable, validated value.
@provides todo.model — the `Task` dataclass and its (de)serialisation

@behavior `Task(id, title, done=False)` is a frozen dataclass; `title` is stripped of surrounding whitespace.
@behavior Creating a task with an empty or whitespace-only title raises `ValueError`.
@behavior Creating a task whose id is not a positive integer (including `bool`) raises `ValueError`.
@behavior `task.complete()` returns a new `Task` with `done=True`; the original is unchanged.
@behavior `Task.from_dict(task.to_dict()) == task` for every task.
@invariant A `Task` never changes after construction.

@implement todo/__init__.py, todo/model.py, tests/test_model.py
@verify python3 -m unittest tests.test_model
@done Model tests cover validation edge cases and the dict round-trip.
