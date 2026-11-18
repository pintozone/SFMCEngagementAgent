"""LiveBackend against a fake HTTP session: checks request shapes without touching SFMC."""
import json
import xml.etree.ElementTree as ET

from sfmc_kit.client.live import LiveBackend
from sfmc_kit.config import SFMCConfig
from sfmc_kit.modules.data_extensions import DESpec, FieldSpec

SOAP_OK = """<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body><{verb}ResponseMsg xmlns="http://exacttarget.com/wsdl/partnerAPI">{body}</{verb}ResponseMsg></soap:Body></soap:Envelope>"""


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.des = {}

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json if json is not None else data))
        if url.endswith("/v2/token"):
            return Resp(200, {"access_token": "t", "expires_in": 1080, "scope": "data_extensions_read",
                              "rest_instance_url": "https://sub.rest.marketingcloudapis.com/",
                              "soap_instance_url": "https://sub.soap.marketingcloudapis.com/"})
        body = data.decode()
        ET.fromstring(body)  # every SOAP envelope must be well-formed XML
        if "<RetrieveRequestMsg" in body:
            if "<ObjectType>DataFolder</ObjectType>" in body:
                res = "<Results><ID>55</ID><Name>Data Extensions</Name></Results>"
            elif "<ObjectType>DataExtension</ObjectType>" in body:
                key = body.split("<Value>")[1].split("</Value>")[0]
                res = (f"<Results><ObjectID>oid-{key}</ObjectID><CustomerKey>{key}</CustomerKey><Name>{key}</Name>"
                       "<CategoryID>55</CategoryID><IsSendable>true</IsSendable></Results>") if key in self.des else ""
            else:
                res = ""
            return Resp(200, SOAP_OK.format(verb="Retrieve", body=f"<OverallStatus>OK</OverallStatus>{res}"))
        if "<CreateRequest" in body:
            key = body.split("<CustomerKey>")[1].split("</CustomerKey>")[0]
            self.des[key] = True
            return Resp(200, SOAP_OK.format(verb="Create",
                                            body="<Results><StatusCode>OK</StatusCode><NewID>1</NewID></Results>"))
        raise AssertionError(body[:200])

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        assert headers["Authorization"] == "Bearer t"
        if "/hub/v1/dataevents/" in url:
            assert all("keys" in r and "values" in r for r in json)
            return Resp(200, json)
        raise AssertionError(url)


def test_live_de_create_and_upsert(monkeypatch):
    be = LiveBackend(SFMCConfig("sub", "id", "secret", "123"))
    fake = FakeSession()
    be.tm.session = be.rest.session = be.soap.session = fake
    spec = DESpec("CK_T_DE", "CK_T_DE", [FieldSpec("SubscriberKey", pk=True), FieldSpec("Opt", "Boolean")],
                  folder="CampaignKit/T")
    be._ensure_data_folder = lambda ct, path: 55
    de, created = be.ensure_de(spec)
    assert created and de["object_id"] == "oid-CK_T_DE"
    token_call = fake.calls[0]
    assert token_call[2]["account_id"] == "123" and token_call[2]["grant_type"] == "client_credentials"
    n = be.upsert_rows("CK_T_DE", [{"SubscriberKey": f"k{i}", "Opt": True} for i in range(1203)], ["SubscriberKey"])
    assert n == 1203
    batches = [c for c in fake.calls if "/hub/v1/dataevents/" in c[1]]
    assert [len(b[2]) for b in batches] == [500, 500, 203]
    assert batches[0][1] == "https://sub.rest.marketingcloudapis.com/hub/v1/dataevents/key:CK_T_DE/rowset"
    assert batches[0][2][0]["values"]["Opt"] == "true"
