"""Minimal SOAP client for the SFMC partner API (used for DEs, folders, send classifications)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape

import requests

from ..auth import TokenManager
from .rest import SFMCError

NS = "http://exacttarget.com/wsdl/partnerAPI"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def x(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return escape(str(v))


def to_xml(tag: str, value: Any) -> str:
    """Render nested dict/list values to XML elements. Keys beginning with '@type' set xsi:type."""
    if isinstance(value, list):
        return "".join(to_xml(tag, v) for v in value)
    if isinstance(value, dict):
        xsi_type = value.get("@type")
        attr = f' xsi:type="{xsi_type}"' if xsi_type else ""
        inner = "".join(to_xml(k, v) for k, v in value.items() if not k.startswith("@") and v is not None)
        return f"<{tag}{attr}>{inner}</{tag}>"
    return f"<{tag}>{x(value)}</{tag}>"


def simple_filter(prop: str, op: str, value: Any) -> str:
    vals = value if isinstance(value, list) else [value]
    v = "".join(f"<Value>{x(i)}</Value>" for i in vals)
    return (f'<Filter xsi:type="SimpleFilterPart"><Property>{x(prop)}</Property>'
            f"<SimpleOperator>{op}</SimpleOperator>{v}</Filter>")


def complex_filter(left: str, logical: str, right: str) -> str:
    l = left.replace("<Filter ", "<LeftOperand ").replace("</Filter>", "</LeftOperand>")
    r = right.replace("<Filter ", "<RightOperand ").replace("</Filter>", "</RightOperand>")
    return f'<Filter xsi:type="ComplexFilterPart">{l}<LogicalOperator>{logical}</LogicalOperator>{r}</Filter>'


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _elem_to_obj(el: ET.Element) -> Any:
    children = list(el)
    if not children:
        return el.text
    out: dict[str, Any] = {}
    for c in children:
        k = _strip(c.tag)
        v = _elem_to_obj(c)
        if k in out:
            if not isinstance(out[k], list):
                out[k] = [out[k]]
            out[k].append(v)
        else:
            out[k] = v
    return out


class SoapClient:
    def __init__(self, tm: TokenManager, session: requests.Session | None = None, verbose: bool = False):
        self.tm = tm
        self.session = session or tm.session
        self.verbose = verbose

    def _envelope(self, action: str, body: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            f'xmlns:xsi="{XSI}" xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
            f'<s:Header><a:Action s:mustUnderstand="1">{action}</a:Action>'
            f'<a:To s:mustUnderstand="1">{self.tm.soap_instance_url}</a:To>'
            f'<fueloauth xmlns="http://exacttarget.com">{self.tm.token()}</fueloauth></s:Header>'
            f'<s:Body xmlns:xsi="{XSI}" xmlns:xsd="http://www.w3.org/2001/XMLSchema">{body}</s:Body></s:Envelope>'
        )

    def _call(self, action: str, body: str) -> ET.Element:
        r = self.session.post(
            self.tm.soap_instance_url,
            data=self._envelope(action, body).encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action},
            timeout=120,
        )
        if self.verbose:
            print(f"  [soap] {action} -> {r.status_code}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise SFMCError(f"SOAP {action}: unparseable response HTTP {r.status_code}: {r.text[:800]}") from e
        fault = root.find(f".//{{{SOAP_NS}}}Fault")
        if fault is not None:
            raise SFMCError(f"SOAP {action} fault: {ET.tostring(fault, encoding='unicode')[:1500]}", r.status_code)
        return root

    def retrieve(self, object_type: str, props: list[str], filter_xml: str = "") -> list[dict]:
        results: list[dict] = []
        request_id = None
        while True:
            if request_id:
                inner = f"<ContinueRequest>{request_id}</ContinueRequest>"
            else:
                p = "".join(f"<Properties>{x(i)}</Properties>" for i in props)
                inner = f"<ObjectType>{object_type}</ObjectType>{p}{filter_xml}"
            body = f'<RetrieveRequestMsg xmlns="{NS}"><RetrieveRequest>{inner}</RetrieveRequest></RetrieveRequestMsg>'
            root = self._call("Retrieve", body)
            status = root.findtext(f".//{{{NS}}}OverallStatus") or ""
            for el in root.iter(f"{{{NS}}}Results"):
                results.append(_elem_to_obj(el))
            if status == "MoreDataAvailable":
                request_id = root.findtext(f".//{{{NS}}}RequestID")
                continue
            if status not in ("OK", "MoreDataAvailable"):
                raise SFMCError(f"SOAP Retrieve {object_type}: {status}")
            return results

    def _mutate(self, verb: str, objects_xml: str, options: str = "<Options/>") -> list[dict]:
        body = f'<{verb}Request xmlns="{NS}">{options}{objects_xml}</{verb}Request>'
        root = self._call(verb, body)
        results = [_elem_to_obj(el) for el in root.iter(f"{{{NS}}}Results")]
        errors = [r for r in results if isinstance(r, dict) and r.get("StatusCode") not in ("OK", None)]
        if errors:
            raise SFMCError(f"SOAP {verb} error: " + "; ".join(
                f"{e.get('StatusCode')}: {e.get('StatusMessage')} (ErrorCode {e.get('ErrorCode')})" for e in errors))
        return results

    def create(self, objects_xml: str) -> list[dict]:
        return self._mutate("Create", objects_xml)

    def update(self, objects_xml: str) -> list[dict]:
        return self._mutate("Update", objects_xml)

    def delete(self, objects_xml: str) -> list[dict]:
        return self._mutate("Delete", objects_xml)
