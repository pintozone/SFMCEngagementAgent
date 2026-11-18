"""Data Extension specs: shared by the live (SOAP) and mock (SQLite) backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..client.soap import x

FIELD_TYPES = {"Text", "Number", "Date", "Boolean", "EmailAddress", "Phone", "Decimal", "Locale"}


@dataclass
class FieldSpec:
    name: str
    type: str = "Text"
    length: int | None = None
    pk: bool = False
    required: bool = False
    default: Any = None
    precision: int | None = None  # Decimal: total digits
    scale: int | None = None      # Decimal: digits after point

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"Field {self.name}: unknown type {self.type}; use one of {sorted(FIELD_TYPES)}")
        if self.pk:
            self.required = True
        if self.type == "Text" and self.length is None:
            self.length = 100
        if self.type == "EmailAddress" and self.length is None:
            self.length = 254
        if self.type == "Decimal":
            self.precision = self.precision or 18
            self.scale = self.scale if self.scale is not None else 2

    def soap_xml(self) -> str:
        parts = [f"<CustomerKey>{x(self.name)}</CustomerKey>", f"<Name>{x(self.name)}</Name>",
                 f"<FieldType>{self.type}</FieldType>",
                 f"<IsRequired>{x(self.required)}</IsRequired>", f"<IsPrimaryKey>{x(self.pk)}</IsPrimaryKey>"]
        if self.type in ("Text", "EmailAddress", "Phone", "Locale") and self.length:
            parts.append(f"<MaxLength>{self.length}</MaxLength>")
        if self.type == "Decimal":
            parts.append(f"<MaxLength>{self.precision}</MaxLength><Scale>{self.scale}</Scale>")
        if self.default is not None:
            parts.append(f"<DefaultValue>{x(self.default)}</DefaultValue>")
        return "<Field>" + "".join(parts) + "</Field>"

    @property
    def sqlite_type(self) -> str:
        return {"Number": "INTEGER", "Decimal": "REAL", "Boolean": "INTEGER"}.get(self.type, "TEXT")


@dataclass
class DESpec:
    key: str
    name: str
    fields: list[FieldSpec]
    sendable: bool = False
    sendable_field: str = "SubscriberKey"
    description: str = ""
    folder: str = "CampaignKit"
    retention_days: int | None = None

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"DE {self.name}: duplicate field names")
        if self.sendable and self.sendable_field not in names:
            raise ValueError(f"DE {self.name}: sendable field {self.sendable_field} not in fields")

    @property
    def pk_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.pk]

    @classmethod
    def from_dict(cls, d: dict) -> "DESpec":
        fields = [FieldSpec(**f) for f in d["fields"]]
        rest = {k: v for k, v in d.items() if k != "fields"}
        rest.setdefault("name", rest.get("key"))
        rest.setdefault("key", rest.get("name"))
        return cls(fields=fields, **rest)

    def soap_xml(self, category_id: int | None) -> str:
        parts = [f"<CustomerKey>{x(self.key)}</CustomerKey>", f"<Name>{x(self.name)}</Name>"]
        if self.description:
            parts.append(f"<Description>{x(self.description)}</Description>")
        if category_id:
            parts.append(f"<CategoryID>{category_id}</CategoryID>")
        parts.append(f"<IsSendable>{x(self.sendable)}</IsSendable>")
        if self.sendable:
            parts.append(
                f"<SendableDataExtensionField><CustomerKey>{x(self.sendable_field)}</CustomerKey>"
                f"<Name>{x(self.sendable_field)}</Name><FieldType>Text</FieldType></SendableDataExtensionField>"
                "<SendableSubscriberField><Name>Subscriber Key</Name><Value></Value></SendableSubscriberField>"
            )
        if self.retention_days:
            parts.append(
                f"<DataRetentionPeriodLength>{self.retention_days}</DataRetentionPeriodLength>"
                "<DataRetentionPeriod>Days</DataRetentionPeriod><RowBasedRetention>true</RowBasedRetention>"
                "<ResetRetentionPeriodOnImport>false</ResetRetentionPeriodOnImport>"
            )
        parts.append("<Fields>" + "".join(f.soap_xml() for f in self.fields) + "</Fields>")
        return '<Objects xsi:type="DataExtension">' + "".join(parts) + "</Objects>"
