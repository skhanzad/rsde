# User cache

@goal Cache user lookups.
@provides db.users
@implement cache/
@verify {python} -m unittest discover -s cache
