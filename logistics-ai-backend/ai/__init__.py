"""AI service package for the isolated Julong enterprise test workbench."""

from .service import (
    AIService, AIServiceError, ChatRequest, ChatResponse, Evidence, ModelConfig,
    build_service,
)
from .handler import handle_chat_request, handle_get_conversation, handle_list_conversations

__all__ = ["AIService", "AIServiceError", "ChatRequest", "ChatResponse", "Evidence", "ModelConfig", "build_service", "handle_chat_request", "handle_list_conversations", "handle_get_conversation"]
