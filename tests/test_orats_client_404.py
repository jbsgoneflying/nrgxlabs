from backend.orats_client import OratsClient


class _FakeResp:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = {}

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, params=None, timeout=None):
        return self._resp


def test_hist_dailies_404_is_treated_as_empty():
    c = OratsClient(token="x", base_url="https://api.orats.io/datav2")
    c._session = _FakeSession(_FakeResp(404, json_body={"message": "Not Found."}, text='{"message":"Not Found."}'))

    out = c.get("/hist/dailies", {"ticker": "AAPL", "tradeDate": "2025-03-09", "fields": "ticker,tradeDate,clsPx,open"})
    assert out.rows == []


