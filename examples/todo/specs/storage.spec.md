# JSON task storage

@goal Persist tasks between runs in a single JSON file.
@requires todo.model
@provides todo.storage — `TaskStore`, a repository of tasks backed by one JSON file

@constraint The file location comes from the `TODO_FILE` environment variable, defaulting to `todo.json` in the current directory.

- @behavior `TaskStore(path).load()` returns an empty list when the file does not exist.
- @behavior `TaskStore.add(title)` assigns the next id — one greater than the highest id
  ever issued — saves, and returns the new task.
- @behavior `TaskStore.complete(task_id)` marks the task done and saves; an unknown id raises `KeyError`.
- @behavior `TaskStore.remove(task_id)` deletes the task and saves; an unknown id raises `KeyError`.

@invariant Saving is atomic: data is written to a temporary file in the same directory and moved into place with `os.replace`.
@invariant The highest id ever issued is stored in the file, so ids are never reused after a removal.

@implement todo/storage.py, tests/test_storage.py
@verify {python} -m unittest tests.test_storage
@done Storage tests cover a missing file, id allocation after removal, unknown ids and atomic replacement.
