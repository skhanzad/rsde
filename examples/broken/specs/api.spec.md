# HTTP API

@goal Serve the application over HTTP.
@requires db.users
@requires auth.session
@provides http.api
@implment api/
@verify python3 -m unittest discover -s api
