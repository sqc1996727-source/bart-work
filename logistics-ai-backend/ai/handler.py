"""Framework-neutral HTTP adapter for the host's authenticated AI route."""

from __future__ import annotations

from typing import Any, Mapping

from .service import AIService, AIServiceError, ChatRequest


ERROR_STATUS = {
    "IDENTITY_REQUIRED": 401,
    "INVALID_MESSAGE": 400,
    "INVALID_MEMORY_SCOPE": 400,
    "CONVERSATION_NOT_FOUND": 404,
    "CONVERSATION_SCOPE_MISMATCH": 404,
    "TOOL_NOT_AUTHORIZED": 403,
    "MODEL_NOT_CONFIGURED": 503,
    "MODEL_HTTP_ERROR": 502,
    "MODEL_CONNECTION_ERROR": 503,
    "CONTEXT_RETRIEVAL_FAILED": 503,
    "TOOL_DISCOVERY_FAILED": 503,
    "TOOL_EXECUTION_FAILED": 502,
    "TOOL_RECOVERY_BLOCKED": 409,
    "TOOL_ROUND_LIMIT": 502,
}


def handle_chat_request(
    service: AIService,
    payload: Any,
    authenticated_user: Mapping[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    """Return ``(http_status, json_body)`` for an already authenticated user.

    Identity always comes from the session. Request bodies cannot choose a
    tenant or principal. The host must authenticate the cookie before calling
    this function and supply an active user row with stable ``id`` and
    ``tenant_id``.
    """
    if not isinstance(authenticated_user, Mapping):
        return _error(401, "IDENTITY_REQUIRED", "请先登录。")
    tenant_id = authenticated_user.get("tenant_id")
    principal_id = authenticated_user.get("id")
    if not tenant_id or not principal_id:
        return _error(401, "IDENTITY_REQUIRED", "当前会话缺少企业或用户身份。")
    if not isinstance(payload, Mapping):
        return _error(400, "INVALID_MESSAGE", "请求格式无效。")
    message = payload.get("message")
    conversation_id = payload.get("conversation_id")
    memory_scope = payload.get("memory_scope", "enterprise")
    if not isinstance(message, str):
        return _error(400, "INVALID_MESSAGE", "消息必须是文本。")
    if conversation_id is not None and not isinstance(conversation_id, str):
        return _error(400, "INVALID_MESSAGE", "会话标识格式无效。")
    try:
        response = service.chat(ChatRequest(
            tenant_id=str(tenant_id),
            principal_id=str(principal_id),
            message=message,
            conversation_id=conversation_id,
            memory_scope=memory_scope if isinstance(memory_scope, str) else "invalid",
        ))
        return 200, response.as_dict()
    except AIServiceError as exc:
        status = ERROR_STATUS.get(exc.code, 400 if exc.code.startswith("INVALID_") else 502)
        result = {
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            "conversation_id": exc.conversation_id,
        }
        if exc.model_metrics:
            result.update(exc.model_metrics)
            result["cost"] = {"amount": None, "currency": None, "status": "UNKNOWN"}
        return status, result
    except Exception:
        # The host should log the exception privately; never return database,
        # provider, or credential-bearing details to the caller.
        return _error(500, "AI_INTERNAL_ERROR", "AI服务内部错误。")


def handle_list_conversations(
    service: AIService,
    authenticated_user: Mapping[str, Any] | None,
    query: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """List only conversations belonging to the authenticated tenant/user.

    Query accepts ``scope``, ``limit`` and ``offset``. Any client supplied
    ``user_id`` or ``tenant_id`` is ignored; identity is session-derived.
    """
    identity = _identity(authenticated_user)
    if identity is None:
        return _error(401, "IDENTITY_REQUIRED", "请先登录。")
    query = query if isinstance(query, Mapping) else {}
    scope = _query_value(query, "scope")
    if scope == "":
        scope = None
    try:
        limit = int(_query_value(query, "limit") or 30)
        offset = int(_query_value(query, "offset") or 0)
    except (TypeError, ValueError):
        return _error(400, "INVALID_PAGINATION", "分页参数格式无效。")
    if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        return _error(400, "INVALID_PAGINATION", "分页参数超出允许范围。")
    try:
        items = service.list_conversations(
            tenant_id=identity["tenant_id"], principal_id=identity["principal_id"],
            memory_scope=scope, limit=limit, offset=offset,
        )
        return 200, {"items": items, "limit": limit, "offset": offset}
    except AIServiceError as exc:
        return _error(400, exc.code, exc.message)
    except Exception:
        return _error(500, "AI_INTERNAL_ERROR", "AI服务内部错误。")


def handle_get_conversation(
    service: AIService,
    conversation_id: str,
    authenticated_user: Mapping[str, Any] | None,
    query: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return one owned conversation page; foreign IDs deliberately look absent."""
    identity = _identity(authenticated_user)
    if identity is None:
        return _error(401, "IDENTITY_REQUIRED", "请先登录。")
    if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 128:
        return _error(404, "CONVERSATION_NOT_FOUND", "会话不存在或无权访问。")
    query = query if isinstance(query, Mapping) else {}
    try:
        limit = int(_query_value(query, "limit") or 50)
        before_raw = _query_value(query, "before_message_id")
        before_id = int(before_raw) if before_raw not in (None, "") else None
    except (TypeError, ValueError):
        return _error(400, "INVALID_PAGINATION", "分页参数格式无效。")
    if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
        return _error(400, "INVALID_PAGINATION", "分页参数超出允许范围。")
    try:
        conversation = service.get_conversation(
            tenant_id=identity["tenant_id"], principal_id=identity["principal_id"],
            conversation_id=conversation_id, limit=limit, before_message_id=before_id,
        )
        if conversation is None:
            return _error(404, "CONVERSATION_NOT_FOUND", "会话不存在或无权访问。")
        return 200, {"conversation": conversation}
    except Exception:
        return _error(500, "AI_INTERNAL_ERROR", "AI服务内部错误。")


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message, "retryable": False}, "conversation_id": None}


def _identity(authenticated_user: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(authenticated_user, Mapping):
        return None
    tenant_id = authenticated_user.get("tenant_id")
    principal_id = authenticated_user.get("id")
    if not tenant_id or not principal_id:
        return None
    return {"tenant_id": str(tenant_id), "principal_id": str(principal_id)}


def _query_value(query: Mapping[str, Any], name: str) -> Any:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value
