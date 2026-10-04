# Database

@goal Store users.
@provides db.users
@requires http.api — needed for webhooks (this creates a cycle)
@implement db/
@verify {python} -m unittest discover -s db
