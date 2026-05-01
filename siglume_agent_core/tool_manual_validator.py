"""Validate tool manuals and schemas for CapabilityRelease publishing.

Ensures seller-defined tool manuals meet runtime contract requirements
before a CapabilityRelease is published to the API Store.

Also provides content quality scoring to help sellers write manuals that
are specific enough for agents to reliably select.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_PERMISSION_CLASSES = {"read_only", "action", "payment"}

PLATFORM_INJECTED_FIELDS = frozenset(
    {
        "execution_id",
        "trace_id",
        "connected_account_id",
        "dry_run",
        "idempotency_key",
        "budget_snapshot",
    }
)

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_MAX_NESTED_DEPTH = 8
_COMPOSITION_KEYWORDS = frozenset({"oneOf", "anyOf", "allOf"})

# v0.2.3 prompt-injection hardening (review item #7).
# Property descriptions in input_schema get embedded in the LLM's tool
# catalog block via generate_compact_prompt → _flatten_input_schema.
# A malicious publisher could otherwise stuff long-form instructions or
# explicit injection text into a description and influence buyer-side
# turns. Cap length and reject obvious injection markers at submission
# time. Length is set generously so legitimate descriptions
# (units, formats, examples) still fit; the bar is "no manual page
# disguised as a description".
MAX_PROPERTY_DESCRIPTION_LEN = 500

# Case-insensitive substring patterns that strongly indicate prompt
# injection rather than legitimate field documentation. Conservative —
# we want zero false positives on well-written publisher copy. Each
# pattern reflects a known jailbreak / prompt-leak technique. Add new
# patterns sparingly and only when seen in the wild.
_INJECTION_PATTERNS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore the above",
    "ignore all prior",
    "disregard previous",
    "disregard the above",
    "system prompt",
    "developer message",
    "reveal the prompt",
    "reveal the system",
    "print the prompt",
    "print the system",
    "show me your prompt",
    "show your prompt",
    "bypass safety",
    "bypass guardrails",
    "you are now",
    # Removed "act as if" — appears in benign technical copy like
    # "if omitted, treat as if value is 0". Codex review on PR #5
    # flagged the false-positive risk; the validator hard-fails on
    # any match so this would reject legitimate publisher manuals.
    "pretend you are",
    "<|im_start|>",
    "<|im_end|>",
    "[INST]",
    "[/INST]",
    "</s>",
    # Common JA jailbreak phrasings — mirror the EN list at the marker
    # frequencies we observe most.
    "前の指示を無視",
    "上記を無視",
    "システムプロンプト",
)

VALID_SETTLEMENT_MODES = {
    "stripe_checkout",
    "stripe_payment_intent",
    "polygon_mandate",
    "embedded_wallet_charge",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    code: str
    message: str
    field: str | None = None


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_tool_manual(manual: dict) -> ValidationResult:
    """Validate the complete tool manual JSON against required fields and rules."""
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []

    if not isinstance(manual, dict):
        return ValidationResult(
            ok=False,
            errors=[ValidationError("INVALID_ROOT", "Manual must be a JSON object")],
        )

    # -- tool_name --
    _validate_str(manual, "tool_name", 3, 64, errors)
    tool_name = manual.get("tool_name")
    if isinstance(tool_name, str) and not _TOOL_NAME_RE.match(tool_name):
        errors.append(
            ValidationError(
                "INVALID_TOOL_NAME",
                "tool_name must be alphanumeric + underscore, 3-64 chars",
                "tool_name",
            )
        )

    # -- text fields with length bounds --
    _validate_str(manual, "job_to_be_done", 10, 500, errors)
    _validate_str(manual, "summary_for_model", 10, 300, errors)

    # -- trigger_conditions --
    _validate_str_list(manual, "trigger_conditions", 3, 8, errors, item_min=10, item_max=200)

    # -- do_not_use_when --
    _validate_str_list(manual, "do_not_use_when", 1, 5, errors)

    # -- permission_class --
    perm = manual.get("permission_class")
    if perm is None:
        errors.append(
            ValidationError("MISSING_FIELD", "permission_class is required", "permission_class")
        )
    elif perm not in VALID_PERMISSION_CLASSES:
        errors.append(
            ValidationError(
                "INVALID_PERMISSION_CLASS",
                f"permission_class must be one of {sorted(VALID_PERMISSION_CLASSES)}",
                "permission_class",
            )
        )

    # -- dry_run_supported --
    _validate_bool(manual, "dry_run_supported", errors)

    # -- requires_connected_accounts --
    rca = manual.get("requires_connected_accounts")
    if rca is None:
        errors.append(
            ValidationError(
                "MISSING_FIELD",
                "requires_connected_accounts is required",
                "requires_connected_accounts",
            )
        )
    elif not isinstance(rca, list):
        errors.append(
            ValidationError(
                "INVALID_TYPE",
                "requires_connected_accounts must be a list",
                "requires_connected_accounts",
            )
        )

    # -- input_schema --
    input_schema = manual.get("input_schema")
    if input_schema is None:
        errors.append(ValidationError("MISSING_FIELD", "input_schema is required", "input_schema"))
    elif not isinstance(input_schema, dict):
        errors.append(
            ValidationError("INVALID_TYPE", "input_schema must be an object", "input_schema")
        )
    else:
        for err_msg in validate_input_schema(input_schema):
            errors.append(ValidationError("INPUT_SCHEMA", err_msg, "input_schema"))

    # -- output_schema --
    output_schema = manual.get("output_schema")
    if output_schema is None:
        errors.append(
            ValidationError("MISSING_FIELD", "output_schema is required", "output_schema")
        )
    elif not isinstance(output_schema, dict):
        errors.append(
            ValidationError("INVALID_TYPE", "output_schema must be an object", "output_schema")
        )
    else:
        for err_msg in validate_output_schema(output_schema, permission_class=perm):
            errors.append(ValidationError("OUTPUT_SCHEMA", err_msg, "output_schema"))

    # -- hint lists (loosely validated) --
    for hint_field in ("usage_hints", "result_hints", "error_hints"):
        val = manual.get(hint_field)
        if val is None:
            errors.append(ValidationError("MISSING_FIELD", f"{hint_field} is required", hint_field))
        elif not isinstance(val, list):
            errors.append(
                ValidationError("INVALID_TYPE", f"{hint_field} must be a list", hint_field)
            )
        elif not all(isinstance(item, str) for item in val):
            errors.append(
                ValidationError(
                    "INVALID_TYPE", f"All items in {hint_field} must be strings", hint_field
                )
            )

    # -- action / payment extras --
    if perm in ("action", "payment"):
        _validate_str(manual, "approval_summary_template", 1, None, errors)
        _validate_json_schema_field(manual, "preview_schema", errors)

        idem = manual.get("idempotency_support")
        if idem is None:
            errors.append(
                ValidationError(
                    "MISSING_FIELD",
                    "idempotency_support is required for action/payment",
                    "idempotency_support",
                )
            )
        elif not isinstance(idem, bool):
            errors.append(
                ValidationError(
                    "INVALID_TYPE", "idempotency_support must be a bool", "idempotency_support"
                )
            )
        elif idem is not True:
            errors.append(
                ValidationError(
                    "IDEMPOTENCY_REQUIRED",
                    "idempotency_support must be true for action/payment permission class",
                    "idempotency_support",
                )
            )

        _validate_str(manual, "side_effect_summary", 1, None, errors)

    # -- payment extras --
    if perm == "payment":
        _validate_json_schema_field(manual, "quote_schema", errors)

        currency = manual.get("currency")
        if currency is None:
            errors.append(
                ValidationError("MISSING_FIELD", "currency is required for payment", "currency")
            )
        elif currency != "USD":
            errors.append(ValidationError("INVALID_CURRENCY", "currency must be 'USD'", "currency"))

        sm = manual.get("settlement_mode")
        if sm is None:
            errors.append(
                ValidationError(
                    "MISSING_FIELD", "settlement_mode is required for payment", "settlement_mode"
                )
            )
        elif sm not in VALID_SETTLEMENT_MODES:
            errors.append(
                ValidationError(
                    "INVALID_SETTLEMENT_MODE",
                    f"settlement_mode must be one of {sorted(VALID_SETTLEMENT_MODES)}",
                    "settlement_mode",
                )
            )

        _validate_str(manual, "refund_or_cancellation_note", 1, None, errors)

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)


def validate_input_schema(schema: dict) -> list[str]:
    """Validate input schema rules.

    Returns a list of human-readable error strings (empty if valid).
    """
    errs: list[str] = []

    # Root type must be object
    if schema.get("type") != "object":
        errs.append("Root type must be 'object'")

    # additionalProperties must be false
    if schema.get("additionalProperties") is not False:
        errs.append("additionalProperties must be false")

    # Composition keywords are allowed, but branches must stay structured and
    # are still scanned for forbidden keys, $ref, and depth limits.
    _check_composition_keywords(schema, errs, path="")

    # No patternProperties at any level
    _check_forbidden_key(schema, "patternProperties", errs, path="")

    # No recursive $ref schemas
    _check_recursive_ref(schema, errs)

    # Max nested depth
    _check_nested_depth(schema, errs, current_depth=0)

    # No platform-injected fields anywhere in the schema (recursive).
    # Previously root-only; v0.2.3 widens the check to nested object
    # schemas, array items, and composition branches so a malicious
    # publisher cannot smuggle a `trace_id` / `connected_account_id`
    # property under a nested object or `oneOf` branch and have it
    # collide with platform-set values at runtime (review item #8).
    _check_platform_injected_recursive(schema, errs, path="")

    # Property description length + prompt-injection pattern check
    # (review item #7).
    _check_property_descriptions(schema, errs, path="")

    return errs


def validate_output_schema(schema: dict, *, permission_class: str | None = None) -> list[str]:
    """Validate output schema rules.

    Returns a list of human-readable error strings (empty if valid).
    """
    errs: list[str] = []

    required = schema.get("required")
    if not isinstance(required, list) or len(required) == 0:
        errs.append("Output schema must have at least one stable required key")

    # Must include summary field
    props = schema.get("properties", {})
    if not isinstance(props, dict) or "summary" not in props:
        errs.append("Output schema must include a 'summary' field in properties")

    # Payment-specific checks
    if permission_class == "payment":
        if isinstance(required, list):
            if "amount_usd" not in required:
                errs.append("Payment output schema must require 'amount_usd'")
            if "currency" not in required:
                errs.append("Payment output schema must require 'currency'")
        if isinstance(props, dict):
            if "amount_usd" not in props:
                errs.append("Payment output schema must include 'amount_usd' in properties")
            if "currency" not in props:
                errs.append("Payment output schema must include 'currency' in properties")

    return errs


def generate_compact_prompt(manual: dict) -> str:
    """Generate the compact tool catalog block for LLM prompts.

    Assumes the manual has already passed validation.
    """
    lines: list[str] = []
    lines.append("[Installed Tool]")
    lines.append(f"name: {manual.get('tool_name', 'unknown')}")
    lines.append(f"job: {manual.get('job_to_be_done', 'no description')}")

    # use_when
    lines.append("use_when:")
    for cond in manual.get("trigger_conditions", []):
        lines.append(f"- {cond}")

    # avoid_when
    lines.append("avoid_when:")
    for cond in manual.get("do_not_use_when", []):
        lines.append(f"- {cond}")

    perm = manual.get("permission_class", "read_only")
    lines.append(f"permission: {perm}")

    # approval mode
    if perm == "read_only":
        approval = "auto"
    elif perm == "action":
        approval = "user_confirm"
    else:  # payment
        approval = "user_confirm+quote"
    lines.append(f"approval: {approval}")

    # requires_accounts
    accounts = manual.get("requires_connected_accounts", [])
    account_labels: list[str] = []
    for account in accounts if isinstance(accounts, list) else []:
        if isinstance(account, dict):
            label = str(
                account.get("provider_key")
                or account.get("provider")
                or account.get("account_type")
                or account.get("name")
                or ""
            ).strip()
        else:
            label = str(account or "").strip()
        if label:
            account_labels.append(label)
    lines.append(f"requires_accounts: {', '.join(account_labels) if account_labels else 'none'}")

    # input fields
    lines.append("input:")
    input_schema = manual.get("input_schema", {})
    input_props = input_schema.get("properties", {})
    input_required = set(input_schema.get("required", []))
    for fname, fdef in input_props.items():
        ftype = _schema_type_label(fdef)
        req_label = "required" if fname in input_required else "optional"
        desc = fdef.get("description", "")
        desc_suffix = f" {desc}" if desc else ""
        lines.append(f"- {fname}:{ftype}({req_label}){desc_suffix}")

    # returns
    lines.append("returns:")
    output_schema = manual.get("output_schema", {})
    output_props = output_schema.get("properties", {})
    for fname in output_props:
        lines.append(f"- {fname}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_str(
    manual: dict,
    field_name: str,
    min_len: int,
    max_len: int | None,
    errors: list[ValidationError],
) -> None:
    val = manual.get(field_name)
    if val is None:
        errors.append(ValidationError("MISSING_FIELD", f"{field_name} is required", field_name))
        return
    if not isinstance(val, str):
        errors.append(ValidationError("INVALID_TYPE", f"{field_name} must be a string", field_name))
        return
    if len(val) < min_len:
        errors.append(
            ValidationError(
                "TOO_SHORT", f"{field_name} must be at least {min_len} chars", field_name
            )
        )
    if max_len is not None and len(val) > max_len:
        errors.append(
            ValidationError("TOO_LONG", f"{field_name} must be at most {max_len} chars", field_name)
        )


def _validate_str_list(
    manual: dict,
    field_name: str,
    min_items: int,
    max_items: int,
    errors: list[ValidationError],
    *,
    item_min: int | None = None,
    item_max: int | None = None,
) -> None:
    val = manual.get(field_name)
    if val is None:
        errors.append(ValidationError("MISSING_FIELD", f"{field_name} is required", field_name))
        return
    if not isinstance(val, list):
        errors.append(ValidationError("INVALID_TYPE", f"{field_name} must be a list", field_name))
        return
    if len(val) < min_items:
        errors.append(
            ValidationError(
                "TOO_FEW_ITEMS", f"{field_name} must have at least {min_items} items", field_name
            )
        )
    if len(val) > max_items:
        errors.append(
            ValidationError(
                "TOO_MANY_ITEMS", f"{field_name} must have at most {max_items} items", field_name
            )
        )
    for i, item in enumerate(val):
        if not isinstance(item, str):
            errors.append(
                ValidationError("INVALID_TYPE", f"{field_name}[{i}] must be a string", field_name)
            )
        elif item_min is not None and len(item) < item_min:
            errors.append(
                ValidationError(
                    "ITEM_TOO_SHORT",
                    f"{field_name}[{i}] must be at least {item_min} chars",
                    field_name,
                )
            )
        elif item_max is not None and len(item) > item_max:
            errors.append(
                ValidationError(
                    "ITEM_TOO_LONG",
                    f"{field_name}[{i}] must be at most {item_max} chars",
                    field_name,
                )
            )


def _validate_bool(
    manual: dict,
    field_name: str,
    errors: list[ValidationError],
) -> None:
    val = manual.get(field_name)
    if val is None:
        errors.append(ValidationError("MISSING_FIELD", f"{field_name} is required", field_name))
    elif not isinstance(val, bool):
        errors.append(ValidationError("INVALID_TYPE", f"{field_name} must be a bool", field_name))


def _validate_json_schema_field(
    manual: dict,
    field_name: str,
    errors: list[ValidationError],
) -> None:
    val = manual.get(field_name)
    if val is None:
        errors.append(ValidationError("MISSING_FIELD", f"{field_name} is required", field_name))
    elif not isinstance(val, dict):
        errors.append(
            ValidationError(
                "INVALID_TYPE", f"{field_name} must be a JSON Schema object", field_name
            )
        )


def _check_composition_keywords(
    schema: Any,
    errs: list[str],
    path: str,
) -> None:
    if not isinstance(schema, dict):
        return
    for kw in _COMPOSITION_KEYWORDS:
        if kw not in schema:
            continue
        branches = schema.get(kw)
        if not isinstance(branches, list) or not branches:
            errs.append(f"{kw} must be a non-empty array{' at ' + path if path else ''}")
            continue
        for index, branch in enumerate(branches):
            branch_path = f"{path}.{kw}[{index}]" if path else f"{kw}[{index}]"
            if not isinstance(branch, dict):
                errs.append(f"{kw}[{index}] must be an object{' at ' + path if path else ''}")
                continue
            _check_composition_keywords(branch, errs, path=branch_path)
    for key, val in schema.items():
        if key == "properties" and isinstance(val, dict):
            for pname, pdef in val.items():
                _check_composition_keywords(pdef, errs, path=f"{path}.{pname}" if path else pname)
        elif key == "items" and isinstance(val, dict):
            _check_composition_keywords(val, errs, path=f"{path}.items" if path else "items")


def _check_forbidden_key(
    schema: Any,
    forbidden: str,
    errs: list[str],
    path: str,
) -> None:
    if not isinstance(schema, dict):
        return
    if forbidden in schema:
        errs.append(f"{forbidden} is not allowed{' at ' + path if path else ''}")
    for key, val in schema.items():
        if key == "properties" and isinstance(val, dict):
            for pname, pdef in val.items():
                _check_forbidden_key(
                    pdef, forbidden, errs, path=f"{path}.{pname}" if path else pname
                )
        elif key == "items" and isinstance(val, dict):
            _check_forbidden_key(val, forbidden, errs, path=f"{path}.items" if path else "items")
        elif key in _COMPOSITION_KEYWORDS and isinstance(val, list):
            for index, branch in enumerate(val):
                _check_forbidden_key(
                    branch,
                    forbidden,
                    errs,
                    path=f"{path}.{key}[{index}]" if path else f"{key}[{index}]",
                )


def _check_platform_injected_recursive(
    schema: Any,
    errs: list[str],
    path: str,
) -> None:
    """Walk every level of the schema flagging any property whose name
    collides with a platform-injected field. Mirrors the traversal of
    ``_check_forbidden_key`` so nested object schemas, array items, and
    composition branches all get the same protection as the root.
    """
    if not isinstance(schema, dict):
        return
    for key, val in schema.items():
        if key == "properties" and isinstance(val, dict):
            for pname, pdef in val.items():
                if pname in PLATFORM_INJECTED_FIELDS:
                    location = f" at {path}.{pname}" if path else f" at {pname}"
                    errs.append(
                        f"Property '{pname}' is platform-injected and must not appear in input_schema{location}"
                    )
                _check_platform_injected_recursive(
                    pdef, errs, path=f"{path}.{pname}" if path else pname
                )
        elif key == "items" and isinstance(val, dict):
            _check_platform_injected_recursive(val, errs, path=f"{path}.items" if path else "items")
        elif key in _COMPOSITION_KEYWORDS and isinstance(val, list):
            for index, branch in enumerate(val):
                _check_platform_injected_recursive(
                    branch, errs, path=f"{path}.{key}[{index}]" if path else f"{key}[{index}]"
                )


def _check_one_description(text: Any, path: str, errs: list[str]) -> None:
    """Apply length cap + injection-pattern check to a single description
    string. Factored out so callers can decide WHICH descriptions to
    pass in (we deliberately do not check the root input_schema's own
    `description` — only descriptions on inner property / array-items /
    composition-branch schemas, which are what get embedded in the LLM
    tool catalog block at runtime).
    """
    if not isinstance(text, str):
        return
    if len(text) > MAX_PROPERTY_DESCRIPTION_LEN:
        location = f" at {path}" if path else ""
        errs.append(
            f"Property description exceeds {MAX_PROPERTY_DESCRIPTION_LEN} chars "
            f"(got {len(text)}){location}"
        )
    lowered = text.lower()
    for marker in _INJECTION_PATTERNS:
        if marker.lower() in lowered:
            location = f" at {path}" if path else ""
            errs.append(
                f"Property description contains a known prompt-injection "
                f"pattern ({marker!r}){location}"
            )
            return  # one error per description is enough; don't spam


def _check_property_descriptions(
    schema: Any,
    errs: list[str],
    path: str,
) -> None:
    """Walk every property at every depth, flagging description fields
    that exceed ``MAX_PROPERTY_DESCRIPTION_LEN`` or contain known prompt-
    injection patterns.

    Deliberate scope: the root ``input_schema``'s OWN ``description`` is
    NOT checked — only descriptions on (a) properties at any depth,
    (b) array ``items`` schemas, and (c) composition-branch schemas
    (``oneOf`` / ``anyOf`` / ``allOf`` entries). Those are the values
    that actually get embedded in the LLM tool catalog block at runtime
    via ``generate_compact_prompt``. Checking the root's description
    would be a backward-compatibility regression — it was always
    accepted before v0.2.3 and never reaches the prompt surface.
    Codex review on PR #5 flagged the over-broad scope.
    """
    if not isinstance(schema, dict):
        return
    for key, val in schema.items():
        if key == "properties" and isinstance(val, dict):
            for pname, pdef in val.items():
                pdef_path = f"{path}.{pname}" if path else pname
                if isinstance(pdef, dict):
                    _check_one_description(pdef.get("description"), pdef_path, errs)
                _check_property_descriptions(pdef, errs, path=pdef_path)
        elif key == "items" and isinstance(val, dict):
            items_path = f"{path}.items" if path else "items"
            _check_one_description(val.get("description"), items_path, errs)
            _check_property_descriptions(val, errs, path=items_path)
        elif key in _COMPOSITION_KEYWORDS and isinstance(val, list):
            for index, branch in enumerate(val):
                branch_path = f"{path}.{key}[{index}]" if path else f"{key}[{index}]"
                if isinstance(branch, dict):
                    _check_one_description(branch.get("description"), branch_path, errs)
                _check_property_descriptions(branch, errs, path=branch_path)


def _check_recursive_ref(schema: dict, errs: list[str]) -> None:
    """Detect $ref usage which may indicate recursive schemas."""
    _walk_for_ref(schema, errs, seen=set())


def _walk_for_ref(node: Any, errs: list[str], seen: set[int]) -> None:
    if not isinstance(node, dict):
        return
    node_id = id(node)
    if node_id in seen:
        errs.append("Recursive schema detected (circular $ref)")
        return
    seen.add(node_id)
    if "$ref" in node:
        errs.append("$ref is not allowed (no recursive schemas)")
    for val in node.values():
        if isinstance(val, dict):
            _walk_for_ref(val, errs, seen)
        elif isinstance(val, list):
            for item in val:
                _walk_for_ref(item, errs, seen)
    seen.discard(node_id)


def _check_nested_depth(
    schema: Any,
    errs: list[str],
    current_depth: int,
) -> None:
    if not isinstance(schema, dict):
        return
    if current_depth > _MAX_NESTED_DEPTH:
        errs.append(f"Schema exceeds max nested depth of {_MAX_NESTED_DEPTH}")
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        for pdef in props.values():
            _check_nested_depth(pdef, errs, current_depth + 1)
    items = schema.get("items")
    if isinstance(items, dict):
        _check_nested_depth(items, errs, current_depth + 1)
    for key in _COMPOSITION_KEYWORDS:
        branches = schema.get(key)
        if isinstance(branches, list):
            for branch in branches:
                _check_nested_depth(branch, errs, current_depth + 1)


def _schema_type_label(field_def: dict) -> str:
    """Return a compact type label for a JSON Schema property."""
    ftype = field_def.get("type", "any")
    if ftype == "array":
        items = field_def.get("items", {})
        inner = items.get("type", "any") if isinstance(items, dict) else "any"
        return f"array[{inner}]"
    return str(ftype)


# ---------------------------------------------------------------------------
# Content quality scoring — constants
# ---------------------------------------------------------------------------

AMBIGUOUS_PHRASES = [
    "use when helpful",
    "use for productivity",
    "use this tool",
    "for many tasks",
    "general purpose",
    "various uses",
    "when needed",
    "as needed",
    "if appropriate",
    "for convenience",
    "to help",
    "to assist",
    "便利な時",
    "必要に応じて",
    "適宜",
    "いろいろな場面で",
    "役に立つ時",
    "困った時",
]

MARKETING_FLUFF = [
    "ultimate",
    "revolutionary",
    "cutting-edge",
    "best-in-class",
    "world-class",
    "game-changing",
    "next-generation",
    "powerful",
    "amazing",
    "incredible",
    "awesome",
    "unbeatable",
    "最高の",
    "革命的な",
    "画期的な",
    "究極の",
    "最強の",
]

STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "it",
        "they",
        "them",
        "their",
        "this",
        "that",
        "these",
        "those",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "no",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "also",
        "only",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "up",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "any",
        "own",
        "same",
        "s",
        "t",
        "don",
        "doesn",
        "didn",
        "won",
        "wouldn",
        "shouldn",
        "isn",
        "aren",
        "wasn",
        "weren",
        "hasn",
        "haven",
        "hadn",
    }
)

_IMPERATIVE_PREFIXES = [
    "use this",
    "use the",
    "call this",
    "call the",
    "invoke this",
    "run this",
    "execute this",
]

_WORD_RE = re.compile(r"[A-Za-z\u3040-\u9fff]{2,}")


# ---------------------------------------------------------------------------
# Content quality scoring — data classes
# ---------------------------------------------------------------------------


@dataclass
class QualityIssue:
    category: (
        str  # "trigger_specificity" | "description_quality" | "schema_completeness" | "ambiguity"
    )
    severity: str  # "critical" | "warning" | "suggestion"
    message: str
    field: str | None = None
    suggestion: str | None = None


@dataclass
class QualityScore:
    overall_score: int  # 0-100
    grade: str  # "A" | "B" | "C" | "D" | "F"
    issues: list[QualityIssue] = field(default_factory=list)
    keyword_coverage_estimate: int = 0
    improvement_suggestions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Content quality scoring — public API
# ---------------------------------------------------------------------------


def score_manual_quality(manual: dict) -> QualityScore:
    """Evaluate content quality of a tool manual and return actionable feedback.

    This goes beyond structural validation: it checks whether trigger_conditions
    are specific enough, descriptions are meaningful, and input_schema fields
    are documented. A structurally valid but content-poor manual will score low.
    """
    if not isinstance(manual, dict):
        return QualityScore(
            overall_score=0,
            grade="F",
            issues=[QualityIssue("ambiguity", "critical", "Manual is not a dict")],
            keyword_coverage_estimate=0,
            improvement_suggestions=["Provide a valid manual dict"],
        )

    issues: list[QualityIssue] = []

    # -- Collect sub-scores --
    trigger_score = _score_trigger_conditions(manual, issues)  # /30
    do_not_use_score = _score_do_not_use_when(manual, issues)  # /10
    summary_score = _score_summary_for_model(manual, issues)  # /10
    input_schema_score = _score_input_schema_descriptions(manual, issues)  # /20
    output_schema_score = _score_output_schema_completeness(manual, issues)  # /10
    hints_score = _score_hints(manual, issues)  # /10
    keyword_count = _estimate_keyword_coverage(manual)
    keyword_score = _score_keyword_coverage(keyword_count)  # /10

    overall = (
        trigger_score
        + do_not_use_score
        + summary_score
        + input_schema_score
        + output_schema_score
        + hints_score
        + keyword_score
    )
    overall = max(0, min(100, overall))

    grade = _overall_to_grade(overall)

    suggestions = _build_improvement_suggestions(
        overall,
        trigger_score,
        do_not_use_score,
        summary_score,
        input_schema_score,
        output_schema_score,
        hints_score,
        keyword_count,
        issues,
    )

    return QualityScore(
        overall_score=overall,
        grade=grade,
        issues=issues,
        keyword_coverage_estimate=keyword_count,
        improvement_suggestions=suggestions,
    )


# ---------------------------------------------------------------------------
# Content quality scoring — sub-scorers
# ---------------------------------------------------------------------------


def _score_trigger_conditions(manual: dict, issues: list[QualityIssue]) -> int:
    """Score trigger_conditions quality (max 30 points)."""
    conditions = manual.get("trigger_conditions")
    if not isinstance(conditions, list) or len(conditions) == 0:
        issues.append(
            QualityIssue(
                "trigger_specificity",
                "critical",
                "No trigger_conditions provided",
                field="trigger_conditions",
            )
        )
        return 0

    score = 30
    penalty_per_issue = 5

    for i, cond in enumerate(conditions):
        if not isinstance(cond, str):
            continue
        field_ref = f"trigger_conditions[{i}]"

        # Length check
        if len(cond) < 15:
            issues.append(
                QualityIssue(
                    "trigger_specificity",
                    "warning",
                    f"Trigger condition is too short ({len(cond)} chars) — be more specific",
                    field=field_ref,
                    suggestion="Describe a concrete situation, e.g. 'When the owner asks for a weather forecast for a specific city'",
                )
            )
            score -= penalty_per_issue

        # Ambiguous phrase check
        cond_lower = cond.lower()
        for phrase in AMBIGUOUS_PHRASES:
            if phrase.lower() in cond_lower:
                issues.append(
                    QualityIssue(
                        "ambiguity",
                        "warning",
                        f"Contains vague phrase '{phrase}' — agents cannot reliably match on this",
                        field=field_ref,
                        suggestion="Replace with a concrete situation description",
                    )
                )
                score -= penalty_per_issue
                break  # one penalty per condition for ambiguity

        # Marketing fluff in triggers
        for fluff in MARKETING_FLUFF:
            if fluff.lower() in cond_lower:
                issues.append(
                    QualityIssue(
                        "description_quality",
                        "warning",
                        f"Marketing language '{fluff}' in trigger condition reduces selection accuracy",
                        field=field_ref,
                        suggestion="Use factual, situation-based language instead",
                    )
                )
                score -= 3
                break

        # Imperative check — triggers should describe situations, not commands
        for prefix in _IMPERATIVE_PREFIXES:
            if cond_lower.startswith(prefix):
                issues.append(
                    QualityIssue(
                        "trigger_specificity",
                        "suggestion",
                        "Trigger reads as an imperative command rather than a situation description",
                        field=field_ref,
                        suggestion="Rewrite as a situation: 'When the user needs...' or 'The agent encounters...'",
                    )
                )
                score -= 2
                break

    # Variety bonus: penalize if fewer than 3 conditions
    if len(conditions) < 3:
        issues.append(
            QualityIssue(
                "trigger_specificity",
                "suggestion",
                f"Only {len(conditions)} trigger condition(s) — 3+ increases selection chances",
                field="trigger_conditions",
            )
        )
        score -= 5

    return max(0, score)


def _score_do_not_use_when(manual: dict, issues: list[QualityIssue]) -> int:
    """Score do_not_use_when quality (max 10 points)."""
    items = manual.get("do_not_use_when")
    if not isinstance(items, list) or len(items) == 0:
        issues.append(
            QualityIssue(
                "description_quality",
                "warning",
                "No do_not_use_when items — agents need negative conditions to avoid false positives",
                field="do_not_use_when",
            )
        )
        return 0

    score = 10
    triggers = manual.get("trigger_conditions", [])
    trigger_texts = [t.lower() for t in triggers if isinstance(t, str)]

    for i, item in enumerate(items):
        if not isinstance(item, str):
            continue
        field_ref = f"do_not_use_when[{i}]"
        item_lower = item.lower()

        # Check for items that are just negations of trigger_conditions
        for t_text in trigger_texts:
            # Simple heuristic: if >60% of words overlap, likely redundant
            item_words = set(_extract_words(item_lower))
            trigger_words = set(_extract_words(t_text))
            if item_words and trigger_words:
                overlap = len(item_words & trigger_words) / max(len(item_words), 1)
                if overlap > 0.6:
                    issues.append(
                        QualityIssue(
                            "ambiguity",
                            "suggestion",
                            "This do_not_use_when item closely mirrors a trigger_condition — add a genuinely different negative case",
                            field=field_ref,
                        )
                    )
                    score -= 3
                    break

        # Check for vague negatives
        if len(item) < 10:
            issues.append(
                QualityIssue(
                    "description_quality",
                    "suggestion",
                    "do_not_use_when item is very short — describe a concrete negative condition",
                    field=field_ref,
                )
            )
            score -= 2

    return max(0, score)


def _score_summary_for_model(manual: dict, issues: list[QualityIssue]) -> int:
    """Score summary_for_model quality (max 10 points)."""
    summary = manual.get("summary_for_model")
    if not isinstance(summary, str) or len(summary) == 0:
        return 0

    score = 10
    summary_lower = summary.lower()

    # Marketing language check (cap at one penalty to avoid stacking)
    fluff_found = False
    for fluff in MARKETING_FLUFF:
        if fluff.lower() in summary_lower:
            issues.append(
                QualityIssue(
                    "description_quality",
                    "warning",
                    f"Marketing language '{fluff}' in summary_for_model — agents ignore hype, use factual descriptions",
                    field="summary_for_model",
                    suggestion="Describe what the tool actually does in plain terms",
                )
            )
            if not fluff_found:
                score -= 3
                fluff_found = True

    # Too short to be useful
    if len(summary) < 20:
        issues.append(
            QualityIssue(
                "description_quality",
                "suggestion",
                "summary_for_model is very brief — a longer factual description helps agent selection",
                field="summary_for_model",
            )
        )
        score -= 3

    return max(0, score)


def _score_input_schema_descriptions(manual: dict, issues: list[QualityIssue]) -> int:
    """Score input_schema field descriptions (max 20 points)."""
    schema = manual.get("input_schema")
    if not isinstance(schema, dict):
        return 0

    schema_issues = _check_schema_descriptions(schema)
    issues.extend(schema_issues)

    if not schema_issues:
        return 20

    # Deduct based on severity
    score = 20
    for si in schema_issues:
        if si.severity == "warning":
            score -= 5
        elif si.severity == "suggestion":
            score -= 2

    return max(0, score)


def _score_output_schema_completeness(manual: dict, issues: list[QualityIssue]) -> int:
    """Score output_schema completeness (max 10 points)."""
    schema = manual.get("output_schema")
    if not isinstance(schema, dict):
        return 0

    score = 10
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return 2

    if len(props) == 0:
        issues.append(
            QualityIssue(
                "schema_completeness",
                "warning",
                "output_schema has no properties defined",
                field="output_schema",
            )
        )
        return 0

    # Check that output properties have descriptions
    undescribed = 0
    for pname, pdef in props.items():
        if isinstance(pdef, dict) and not pdef.get("description"):
            undescribed += 1

    if undescribed > 0:
        issues.append(
            QualityIssue(
                "schema_completeness",
                "suggestion",
                f"{undescribed} output field(s) lack descriptions",
                field="output_schema",
                suggestion="Add description to each output property so agents know what to expect",
            )
        )
        score -= min(undescribed * 2, 6)

    return max(0, score)


def _score_hints(manual: dict, issues: list[QualityIssue]) -> int:
    """Score usage_hints and result_hints quality (max 10 points)."""
    score = 10

    for hint_field in ("usage_hints", "result_hints"):
        hints = manual.get(hint_field)
        if not isinstance(hints, list):
            score -= 5
            continue
        if len(hints) == 0:
            issues.append(
                QualityIssue(
                    "description_quality",
                    "suggestion",
                    f"{hint_field} is empty — hints help agents use the tool correctly",
                    field=hint_field,
                )
            )
            score -= 3
        else:
            short_count = sum(1 for h in hints if isinstance(h, str) and len(h) < 10)
            if short_count > 0:
                issues.append(
                    QualityIssue(
                        "description_quality",
                        "suggestion",
                        f"{short_count} item(s) in {hint_field} are very short — provide actionable guidance",
                        field=hint_field,
                    )
                )
                score -= min(short_count * 1, 3)

    return max(0, score)


def _score_keyword_coverage(keyword_count: int) -> int:
    """Score based on estimated keyword coverage (max 10 points)."""
    if keyword_count >= 20:
        return 10
    if keyword_count >= 15:
        return 8
    if keyword_count >= 10:
        return 6
    if keyword_count >= 5:
        return 4
    return max(0, keyword_count)


# ---------------------------------------------------------------------------
# Content quality scoring — helpers
# ---------------------------------------------------------------------------


def _check_schema_descriptions(schema: dict) -> list[QualityIssue]:
    """Check that input_schema properties have adequate descriptions."""
    issues: list[QualityIssue] = []
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return issues

    for pname, pdef in props.items():
        if not isinstance(pdef, dict):
            continue

        field_ref = f"input_schema.properties.{pname}"
        desc = pdef.get("description")

        if desc is None or (isinstance(desc, str) and len(desc.strip()) == 0):
            issues.append(
                QualityIssue(
                    "schema_completeness",
                    "warning",
                    f"Field '{pname}' has no description — agents will not know what to pass",
                    field=field_ref,
                    suggestion=f"Add a description explaining what '{pname}' represents and any constraints",
                )
            )
        elif isinstance(desc, str) and len(desc.strip()) < 10:
            issues.append(
                QualityIssue(
                    "schema_completeness",
                    "suggestion",
                    f"Field '{pname}' has a very short description ({len(desc.strip())} chars)",
                    field=field_ref,
                    suggestion="Expand the description to at least 10 characters for clarity",
                )
            )

        # Check enum values for meaningfulness
        enum_vals = pdef.get("enum")
        if isinstance(enum_vals, list):
            trivial = [v for v in enum_vals if isinstance(v, str) and len(v) <= 1]
            if len(trivial) > 0 and len(trivial) == len(enum_vals):
                issues.append(
                    QualityIssue(
                        "schema_completeness",
                        "warning",
                        f"Field '{pname}' has only single-character enum values — use meaningful names",
                        field=field_ref,
                        suggestion="Replace enum values like 'a','b','c' with descriptive names like 'celsius','fahrenheit'",
                    )
                )

        # Recurse into nested objects
        if pdef.get("type") == "object":
            nested_issues = _check_schema_descriptions(pdef)
            issues.extend(nested_issues)

        # Recurse into array items
        items = pdef.get("items")
        if isinstance(items, dict) and items.get("type") == "object":
            nested_issues = _check_schema_descriptions(items)
            issues.extend(nested_issues)

    return issues


def _estimate_keyword_coverage(manual: dict) -> int:
    """Extract unique meaningful keywords from manual text fields.

    Counts distinct non-stop-word tokens from trigger_conditions,
    job_to_be_done, summary_for_model, and usage_hints.
    """
    text_parts: list[str] = []

    # trigger_conditions
    conditions = manual.get("trigger_conditions")
    if isinstance(conditions, list):
        text_parts.extend(c for c in conditions if isinstance(c, str))

    # job_to_be_done
    job = manual.get("job_to_be_done")
    if isinstance(job, str):
        text_parts.append(job)

    # summary_for_model
    summary = manual.get("summary_for_model")
    if isinstance(summary, str):
        text_parts.append(summary)

    # usage_hints
    hints = manual.get("usage_hints")
    if isinstance(hints, list):
        text_parts.extend(h for h in hints if isinstance(h, str))

    combined = " ".join(text_parts)
    words = _extract_words(combined.lower())
    meaningful = {w for w in words if w not in STOP_WORDS and len(w) >= 2}
    return len(meaningful)


def _extract_words(text: str) -> list[str]:
    """Extract word tokens from text (ASCII + CJK)."""
    return _WORD_RE.findall(text)


def _overall_to_grade(score: int) -> str:
    """Convert numeric score to letter grade."""
    if score >= 90:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    if score >= 30:
        return "D"
    return "F"


def _build_improvement_suggestions(
    overall: int,
    trigger_score: int,
    do_not_use_score: int,
    summary_score: int,
    input_schema_score: int,
    output_schema_score: int,
    hints_score: int,
    keyword_count: int,
    issues: list[QualityIssue],
) -> list[str]:
    """Build a prioritized list of actionable improvement suggestions."""
    suggestions: list[str] = []

    if trigger_score < 20:
        suggestions.append(
            "Improve trigger_conditions: write 3-5 specific situations describing "
            "WHEN an agent should select this tool (e.g. 'When the user asks for current "
            "weather in a named city')."
        )
    if input_schema_score < 15:
        suggestions.append(
            "Add descriptions to all input_schema properties. Each description should "
            "be at least 10 characters and explain what the field represents."
        )
    if summary_score < 7:
        suggestions.append(
            "Rewrite summary_for_model with factual, plain language. Avoid marketing "
            "adjectives — describe what the tool does, not how great it is."
        )
    if do_not_use_score < 7:
        suggestions.append(
            "Add concrete do_not_use_when conditions that are genuinely different from "
            "your trigger_conditions. These help agents avoid false-positive matches."
        )
    if output_schema_score < 7:
        suggestions.append(
            "Add descriptions to output_schema properties so agents know what data "
            "they will receive."
        )
    if hints_score < 7:
        suggestions.append(
            "Expand usage_hints and result_hints with actionable guidance for agents."
        )
    if keyword_count < 10:
        suggestions.append(
            f"Keyword coverage is low ({keyword_count} unique terms). Use varied "
            "vocabulary across trigger_conditions and hints to cover more request phrasings."
        )

    return suggestions
