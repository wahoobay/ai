"""Code shared by the services, vendored into each image (`COPY services/common /app/common`).

Locally `services/` is on the path (the launchers, tests/conftest.py), so the same
`from common.storage import ...` works in the repository and in a container.
"""
