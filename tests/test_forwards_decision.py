"""forwards.decision pure-logic tests: one-shot decision tickets."""

from __future__ import annotations

import inspect

from repeater_plugin.forwards.decision import DecisionTickets


def test_issued_ticket_can_be_consumed_exactly_once():
    tickets = DecisionTickets(ttl_seconds=60.0)
    snapshot = {"message_id": "9001", "summary": "材料"}
    ticket = tickets.issue("9001", "20001", snapshot, now=100.0)
    assert isinstance(ticket, str) and ticket
    assert tickets.consume("9001", "20001", now=101.0) is snapshot
    assert tickets.consume("9001", "20001", now=101.0) is None


def test_expired_ticket_is_rejected():
    tickets = DecisionTickets(ttl_seconds=30.0)
    tickets.issue("9001", "20001", {"snapshot": True}, now=100.0)
    assert tickets.consume("9001", "20001", now=130.0) is None


def test_target_group_mismatch_is_rejected_without_burning_the_ticket():
    tickets = DecisionTickets(ttl_seconds=30.0)
    snapshot = {"snapshot": True}
    tickets.issue("9001", "20001", snapshot, now=100.0)
    assert tickets.consume("9001", "20002", now=101.0) is None
    assert tickets.consume("9001", "20001", now=101.0) is snapshot


def test_unknown_or_incomplete_key_is_rejected():
    tickets = DecisionTickets(ttl_seconds=30.0)
    tickets.issue("9001", "20001", {"snapshot": True}, now=100.0)
    assert tickets.consume("", "20001", now=100.0) is None
    assert tickets.consume("9001", "", now=100.0) is None
    assert tickets.consume("9002", "20001", now=100.0) is None
    assert tickets.consume(None, None, now=100.0) is None


def test_issue_without_message_target_or_snapshot_returns_no_ticket():
    tickets = DecisionTickets(ttl_seconds=30.0)
    assert tickets.issue("", "20001", {"snapshot": True}, now=100.0) is None
    assert tickets.issue("9001", "", {"snapshot": True}, now=100.0) is None
    # 快照缺失无法与「无票」区分，一律拒绝签发（消费成功必返回快照）。
    assert tickets.issue("9001", "20001", None, now=100.0) is None
    assert tickets.consume("9001", "20001", now=100.0) is None


def test_disabled_registry_issues_nothing():
    tickets = DecisionTickets(ttl_seconds=0.0)
    assert tickets.issue("9001", "20001", {"snapshot": True}, now=100.0) is None


def test_discard_expired_returns_and_removes_only_expired_tickets():
    """过期未消费的票由 discard_expired 返回并移除；未过期票不受影响。"""
    tickets = DecisionTickets(ttl_seconds=30.0)
    tickets.issue("9001", "20001", {"snapshot": True}, now=100.0)
    tickets.issue("9001", "20002", {"snapshot": True}, now=110.0)
    assert tickets.discard_expired(now=131.0) == [("9001", "20001")]
    assert tickets.discard_expired(now=131.0) == []
    assert tickets.consume("9001", "20002", now=131.0) == {"snapshot": True}


def test_discard_expired_ignores_consumed_tickets():
    """已消费的票早已出表，不会出现在超时返回值里（返回值天然只含超时未消费）。"""
    tickets = DecisionTickets(ttl_seconds=30.0)
    tickets.issue("9001", "20001", {"snapshot": True}, now=100.0)
    assert tickets.consume("9001", "20001", now=101.0) == {"snapshot": True}
    assert tickets.discard_expired(now=200.0) == []


def test_consume_is_synchronous():
    assert inspect.iscoroutinefunction(DecisionTickets.consume) is False
    assert inspect.iscoroutinefunction(DecisionTickets.issue) is False
    assert inspect.iscoroutinefunction(DecisionTickets.discard_expired) is False
