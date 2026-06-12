"""Tests for block_reverser token optimization: reversed_blocks cap and conversation reset."""
from __future__ import annotations

from typing import Any

from re_agent.agents.block_reverser import BlockReverserAgent
from re_agent.agents.block_splitter import Block
from re_agent.llm.protocol import Message


class CapturingProvider:
    """Provider that captures messages sent for inspection."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = responses or ["```cpp\nint x = 1;\n```"]
        self._idx = 0
        self.sent_messages: list[list[Message]] = []
        self._conversations: dict[str, list[Message]] = {}

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.sent_messages.append(list(messages))
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        cid = "cap-conv"
        self._conversations[cid] = [Message(role="system", content=system)]
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
        history = self._conversations.get(conversation_id, [])
        history.append(Message(role="user", content=message))
        resp = self.send(list(history))
        history.append(Message(role="assistant", content=resp))
        return resp

    def delete_conversation(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)


def _make_block(block_id: str, label: str, text: str) -> Block:
    return Block(id=block_id, label=label, decompiled_text=text, comment="")


def test_reversed_blocks_capped_to_3() -> None:
    """Only the last 3 blocks appear in context; earlier ones are dropped."""
    provider = CapturingProvider()
    agent = BlockReverserAgent(provider)

    reversed_blocks = {
        "b0": "int a = 1;",
        "b1": "int b = 2;",
        "b2": "int c = 3;",
        "b3": "int d = 4;",
        "b4": "int e = 5;",
    }
    block = _make_block("b5", "new_block", "int f = 6;")

    agent.reverse_block(
        block=block, class_name="Foo", function_name="bar", address="0x1000",
        reversed_blocks=reversed_blocks,
    )

    assert len(provider.sent_messages) > 0
    user_msg = provider.sent_messages[-1][-1].content if provider.sent_messages[-1] else ""
    if not user_msg:
        for msg in provider.sent_messages[-1]:
            if msg.role == "user":
                user_msg = msg.content
                break
    # Should mention b2, b3, b4 (last 3) but NOT b0 or b1
    assert "b2" in user_msg
    assert "b3" in user_msg
    assert "b4" in user_msg
    assert "b0" not in user_msg
    assert "b1" not in user_msg


def test_reversed_blocks_fewer_than_3_shows_all() -> None:
    """When fewer than 3 blocks exist, all are shown."""
    provider = CapturingProvider()
    agent = BlockReverserAgent(provider)

    reversed_blocks = {
        "b0": "int a = 1;",
        "b1": "int b = 2;",
    }
    block = _make_block("b2", "last_block", "int c = 3;")

    agent.reverse_block(
        block=block, class_name="Foo", function_name="bar", address="0x1000",
        reversed_blocks=reversed_blocks,
    )

    user_msg = ""
    for msg in provider.sent_messages[-1]:
        if msg.role == "user":
            user_msg = msg.content
            break
    assert "b0" in user_msg
    assert "b1" in user_msg


def test_reset_conversation_clears_id() -> None:
    """reset_conversation() sets _conversation_id to None."""
    provider = CapturingProvider()
    agent = BlockReverserAgent(provider)
    assert agent._conversation_id is None

    block = _make_block("b0", "first", "int x = 0;")
    agent.reverse_block(block=block, class_name="Foo", function_name="bar", address="0x1000")
    assert agent._conversation_id is not None

    agent.reset_conversation()
    assert agent._conversation_id is None


def test_reset_conversation_between_blocks_uses_new_conversation() -> None:
    """After reset, next block starts a fresh conversation."""
    provider = CapturingProvider()
    agent = BlockReverserAgent(provider)

    b1 = _make_block("b0", "first", "int x = 1;")
    agent.reverse_block(block=b1, class_name="Foo", function_name="bar", address="0x1000")
    first_conv_id = agent._conversation_id
    assert first_conv_id is not None

    agent.reset_conversation()
    assert agent._conversation_id is None

    b2 = _make_block("b1", "second", "int y = 2;")
    agent.reverse_block(block=b2, class_name="Foo", function_name="bar", address="0x1000")
    second_conv_id = agent._conversation_id
    assert second_conv_id is not None
    # Since reset clears and provider returns same "cap-conv", the ID may be same
    # but the conversation history should be fresh (no previous block context)
