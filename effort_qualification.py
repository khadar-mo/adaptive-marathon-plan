"""Explicit effort declarations; never a trace-quality or physiological verdict."""
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "effort-qualification-v1"
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["source", "athlete_attestation", "document"]
    method: str = Field(min_length=1, max_length=1000)
    reference: str | None = Field(None, max_length=2000)
    raw_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    message_group: str | None = Field(None, max_length=80)
    index: int | None = Field(None, ge=0, le=2**31 - 1, strict=True)

    @field_validator("method", "reference", "message_group")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Evidence text must not be blank.")
        return value


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maximality: Literal["CONFIRMED", "NOT_MAXIMAL", "UNKNOWN"] = "UNKNOWN"
    rest_state: Literal["RESTED", "FATIGUED", "UNKNOWN"] = "UNKNOWN"
    control_state: Literal["CONTROLLED", "UNCONTROLLED", "UNKNOWN"] = "UNKNOWN"
    race_distance_verification: Literal["ATTESTED", "DOCUMENTED", "UNKNOWN"] = "UNKNOWN"
    race_time_verification: Literal["ATTESTED", "DOCUMENTED", "UNKNOWN"] = "UNKNOWN"
    evidence_refs: list[Evidence] = Field(default_factory=list, max_length=40)


def validate(value, maximal_confirmed):
    if value is None:
        return None
    result = Qualification.model_validate(value).model_dump()
    if maximal_confirmed != (result["maximality"] == "CONFIRMED"):
        raise ValueError("Maximal confirmation and the explicit maximality declaration must agree.")
    for ref in result["evidence_refs"]:
        if ref["kind"] == "source" and (not ref["raw_sha256"] or not ref["message_group"] or ref["index"] is None):
            raise ValueError("Source evidence needs its checksum, message group and original index.")
        if ref["kind"] != "source" and not ref["reference"]:
            raise ValueError("An attestation or document needs a recorded reference or explanation.")
    if any(result[k] == "DOCUMENTED" for k in ("race_distance_verification", "race_time_verification")) and not any(r["kind"] in {"source", "document"} for r in result["evidence_refs"]):
        raise ValueError("DOCUMENTED race evidence needs an owned source message or recorded document reference.")
    return result


def read(context=None):
    context = context or {}
    value = deepcopy(context.get("qualification") or Qualification().model_dump())
    # False in the original API meant unconfirmed, not a deliberate nonmaximal report.
    if not context.get("qualification") and context.get("maximal_confirmed") is True:
        value["maximality"] = "CONFIRMED"
    supported = bool(value["evidence_refs"])
    protocol = bool((context.get("protocol") or "").strip())
    controlled = value["rest_state"] == "RESTED" or value["control_state"] == "CONTROLLED"
    test = context.get("category") == "VERIFIED_TEST" and value["maximality"] == "CONFIRMED" and protocol and controlled and supported
    race = context.get("category") == "RACE" and all(value[k] != "UNKNOWN" for k in ("race_distance_verification", "race_time_verification")) and supported
    conflict = value["rest_state"] == "RESTED" and "FATIGUED_BLOCK" in context.get("tags", [])
    value.update(schema_version=SCHEMA_VERSION, context_id=context.get("id"), known_at=context.get("known_at"),
                 actor=context.get("actor"), explanation=context.get("note"),
                 evaluation_code_sha256=CODE_SHA256, declaration_code_sha256=context.get("qualification_code_sha256"),
                 test_evidence_status="CONFLICTED" if conflict else "SUPPORTED_ATTESTATION" if test else "UNVERIFIED",
                 race_evidence_status="CONFLICTED" if conflict else "SUPPORTED_ATTESTATION" if race else "UNVERIFIED",
                 scientific_validation="UNVERIFIED", trace_quality="SEPARATE",
                 interpretation="Recorded evidence/declarations; DOCUMENTED does not mean independently verified by this application.")
    return value


def validate_sources(store, result, refs):
    from fit_data import read_messages
    import canonical
    decoded = {}
    for ref in refs:
        if ref["kind"] != "source":
            continue
        sha = ref["raw_sha256"]
        source = next((s for s in result["sources"] if s["fit_sha256"] == sha), None)
        if source is None:
            raise ValueError("Evidence source does not belong to this extraction.")
        if sha not in decoded:
            raw = canonical.source_bytes(store.archive, source)
            if raw is None:
                raise ValueError("Evidence source is unavailable; retain an explicit attestation instead.")
            decoded[sha] = read_messages(raw)
        rows = decoded[sha].get(ref["message_group"])
        if not isinstance(rows, list) or ref["index"] >= len(rows):
            raise ValueError("Evidence references an absent original message.")
