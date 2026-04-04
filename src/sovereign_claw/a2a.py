"""
a2a.py — Agent2Agent Protocol Support
======================================
Implements the A2A (Agent2Agent) protocol for peer-to-peer interoperability
between opaque, multi-vendor AI agent systems.

A2A governs how agents negotiate, plan, and collaborate with other autonomous
agents — complementing MCP (which governs agent-to-tool/data interaction).

Key concepts:
  - Agent Cards: JSON metadata for capability advertisement and discovery
  - Structured Tasks: Trackable work units with lifecycle states
  - Opaque Collaboration: Agents collaborate without exposing internals

Reference: Google A2A Protocol (2026 standard)
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskState(str, Enum):
    """A2A task lifecycle states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


# Terminal states — once reached, no further transitions allowed.
_TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED})

# Valid state transitions
_VALID_TRANSITIONS: Dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: frozenset({TaskState.WORKING, TaskState.FAILED, TaskState.CANCELED}),
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset({TaskState.WORKING, TaskState.FAILED, TaskState.CANCELED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
}


@dataclass
class AgentSkill:
    """A capability advertised by an agent."""

    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    input_modes: List[str] = field(default_factory=lambda: ["text"])
    output_modes: List[str] = field(default_factory=lambda: ["text"])


@dataclass
class AgentCard:
    """
    A2A Agent Card — standardized JSON document advertising an agent's
    capabilities, authentication requirements, and supported modalities.

    Published at ``/.well-known/agent.json`` for discovery.
    """

    name: str
    description: str = ""
    version: str = "1.0.0"
    url: str = ""
    skills: List[AgentSkill] = field(default_factory=list)
    auth_schemes: List[str] = field(default_factory=list)
    supported_protocols: List[str] = field(default_factory=lambda: ["a2a/1.0"])
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to A2A agent card JSON."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "url": self.url,
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "tags": s.tags,
                    "input_modes": s.input_modes,
                    "output_modes": s.output_modes,
                }
                for s in self.skills
            ],
            "auth_schemes": self.auth_schemes,
            "supported_protocols": self.supported_protocols,
            "metadata": self.metadata,
        }

    def fingerprint(self) -> str:
        """SHA-256 fingerprint of the agent card for integrity checks."""
        canonical = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class MessagePart:
    """A single part of an A2A message (text, data, or file reference)."""

    kind: str = "text"  # text | data | file
    content: str = ""
    mime_type: str = "text/plain"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class A2AMessage:
    """A message exchanged within an A2A task."""

    role: str  # "user" | "agent"
    parts: List[MessagePart] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: float = field(default_factory=time.time)

    def text(self) -> str:
        """Extract combined text content from all text parts."""
        return " ".join(p.content for p in self.parts if p.kind == "text")


@dataclass
class Artifact:
    """Output artifact produced during task execution."""

    name: str
    content: str = ""
    mime_type: str = "application/octet-stream"
    metadata: Dict[str, Any] = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])


@dataclass
class A2ATask:
    """
    A2A Task — a discrete, trackable unit of work with full lifecycle.

    Tasks carry context history, track execution progress, and store
    output artifacts, preventing context loss during long-running
    asynchronous operations.
    """

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: TaskState = TaskState.SUBMITTED
    messages: List[A2AMessage] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def transition(self, new_state: TaskState) -> None:
        """
        Transition to a new state with validation.

        Raises ValueError on invalid transitions.
        """
        if new_state not in _VALID_TRANSITIONS.get(self.state, frozenset()):
            raise ValueError(f"Invalid transition: {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.updated_at = time.time()

    def add_message(self, role: str, text: str) -> A2AMessage:
        """Add a text message to the task."""
        msg = A2AMessage(role=role, parts=[MessagePart(content=text)])
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def add_artifact(self, name: str, content: str, mime_type: str = "text/plain") -> Artifact:
        """Add an output artifact to the task."""
        art = Artifact(name=name, content=content, mime_type=mime_type)
        self.artifacts.append(art)
        self.updated_at = time.time()
        return art


class A2AServer:
    """
    A2A Protocol server for handling inter-agent task lifecycle.

    Manages task creation, state transitions, message exchange,
    and agent card publication. Enforces opaque collaboration —
    internal state is never exposed to remote agents.
    """

    def __init__(self, agent_card: AgentCard) -> None:
        self._card = agent_card
        self._tasks: Dict[str, A2ATask] = {}

    @property
    def agent_card(self) -> AgentCard:
        return self._card

    def get_agent_card(self) -> Dict[str, Any]:
        """Return the agent card as a JSON-serializable dict."""
        return self._card.to_dict()

    def create_task(
        self, initial_message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> A2ATask:
        """Create a new task from an initial user message."""
        task = A2ATask(metadata=metadata or {})
        task.add_message("user", initial_message)
        self._tasks[task.task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[A2ATask]:
        """Retrieve a task by ID."""
        return self._tasks.get(task_id)

    def list_tasks(self, state: Optional[TaskState] = None) -> List[A2ATask]:
        """List tasks, optionally filtered by state."""
        tasks = list(self._tasks.values())
        if state is not None:
            tasks = [t for t in tasks if t.state == state]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def send_message(self, task_id: str, role: str, text: str) -> A2AMessage:
        """Send a message within an existing task."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.is_terminal:
            raise ValueError(f"Cannot send message to terminal task: {task.state.value}")
        return task.add_message(role, text)

    def transition_task(self, task_id: str, new_state: TaskState) -> None:
        """Transition a task to a new state."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        task.transition(new_state)

    def complete_task(self, task_id: str, result_text: str) -> A2ATask:
        """Complete a task with a final result message."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        # Ensure we're in WORKING state first
        if task.state in (TaskState.SUBMITTED, TaskState.INPUT_REQUIRED):
            task.transition(TaskState.WORKING)
        task.add_message("agent", result_text)
        task.transition(TaskState.COMPLETED)
        return task

    def fail_task(self, task_id: str, error: str) -> A2ATask:
        """Fail a task with an error message."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.state in (TaskState.SUBMITTED, TaskState.INPUT_REQUIRED):
            task.transition(TaskState.WORKING)
        task.add_message("agent", f"Error: {error}")
        task.transition(TaskState.FAILED)
        return task

    def cancel_task(self, task_id: str) -> A2ATask:
        """Cancel a task."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        task.transition(TaskState.CANCELED)
        return task

    def stats(self) -> Dict[str, int]:
        """Return task count per state."""
        counts: Dict[str, int] = {}
        for task in self._tasks.values():
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        return counts
