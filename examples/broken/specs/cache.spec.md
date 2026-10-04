# User cache

@goal Cache user lookups.
@provides db.users
@implement cache/
@verify python3 -m unittest discover -s cache
