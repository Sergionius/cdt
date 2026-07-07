import json


class FakeResponse:
    """Minimal fake for urllib.request.urlopen return value."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def github_latest_release_json(tag: str) -> bytes:
    return json.dumps({"tag_name": tag}).encode("utf-8")
