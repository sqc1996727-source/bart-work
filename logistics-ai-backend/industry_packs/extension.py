"""Small, code-owned extension boundary for new industry adapters.

An adapter supplies actual schemas and handlers, not prompt-only business claims.
Legacy logistics uses the existing, separately tested implementation.
"""
from collections.abc import Mapping
from . import get_pack


def _allowed(profile, user, module):
    pack = get_pack(profile["industry"])
    return (module in profile["modules"] and module in pack["modules"]
            and user["role"] in pack["modules"][module]["roles"])


def list_tools(profile, user):
    pack = get_pack(profile["industry"])
    items = []
    for module, entries in pack.get("adapter_tools", {}).items():
        if not _allowed(profile, user, module):
            continue
        for name, entry in entries.items():
            spec = entry["schema"]
            if name not in pack["tool_groups"].get(module, ()):
                raise ValueError("adapter tool must belong to a declared module")
            if spec["function"]["name"] != name or not callable(entry["execute"]):
                raise ValueError("invalid adapter tool registration")
            items.append(spec)
    return items


def execute(profile, host, user, name, arguments, key):
    pack = get_pack(profile["industry"])
    for module, entries in pack.get("adapter_tools", {}).items():
        if name not in entries:
            continue
        if not _allowed(profile, user, module) or name not in pack["tool_groups"].get(module, ()):
            return True, {"error": "forbidden"}
        entry = entries[name]
        params = entry["schema"]["function"]["parameters"]
        if not isinstance(arguments, dict) or set(arguments) - set(params.get("properties", {})) or any(
                required not in arguments for required in params.get("required", ())):
            return True, {"error": "invalid_request"}
        # Adapter handlers enforce entity permissions, business invariants and
        # transactional idempotency. The host never assumes a write succeeded.
        result = entry["execute"](host, user, arguments, key)
        if not isinstance(result, Mapping):
            raise TypeError("adapter must return a structured result")
        return True, dict(result)
    return False, None


def initialize(profile, host):
    """Run a registered adapter's idempotent, additive schema initialization."""
    pack = get_pack(profile["industry"])
    if profile["modules"] and pack.get("initialize"):
        pack["initialize"](host, profile)


def route(profile, host, user, method, path, query, body, key):
    """Authenticated, namespaced industry routes; never override shared routes."""
    prefix = "/api/industry/" + profile["industry"] + "/"
    if not path.startswith(prefix):
        return 404, {"error": "not_found"}
    pack = get_pack(profile["industry"])
    entry = pack.get("adapter_routes", {}).get((method, path))
    if not entry:
        return 404, {"error": "not_found"}
    module = entry["module"]
    if not _allowed(profile, user, module):
        return 404, {"error": "not_found"}
    if not any(path.startswith(p) for p in pack["route_groups"].get(module, ())):
        return 404, {"error": "not_found"}
    status, result = entry["execute"](host, user, query, body, key)
    if not isinstance(status, int) or not isinstance(result, Mapping):
        raise TypeError("adapter route must return status and structured result")
    return status, dict(result)
