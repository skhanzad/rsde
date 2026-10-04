# Database

@goal Store users.
@provides db.users
@requires http.api — needed for webhooks (this creates a cycle)
@implement db/
@verify python3 -m unittest discover -s db
