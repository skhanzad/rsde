# Sub-commands

@goal Implement the behaviour of each sub-command independently of argument parsing.
@requires todo.storage, todo.model
@provides todo.commands — `add`, `list_tasks`, `done` and `remove`

@constraint Commands never print; they return values or raise, so they stay easy to test.

@behavior `add(store, title)` adds a task and returns it.
@behavior `list_tasks(store, include_done=False)` returns open tasks ordered by id; with `include_done=True` it returns every task.
@behavior `done(store, task_id)` and `remove(store, task_id)` raise `TaskNotFound` (a subclass of `LookupError`) for unknown ids.

@implement todo/commands.py, tests/test_commands.py
@verify {python} -m unittest tests.test_commands
@done Commands are covered for both the success and the unknown-id paths.
