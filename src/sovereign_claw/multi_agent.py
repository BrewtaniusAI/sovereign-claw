"""
multi_agent.py — Federated Multi-Agent Orchestrator
====================================================
Implements a governed multi-agent system with:
  - Agent registry with typed roles (planner, executor, validator, critic)
  - Role isolation enforcement (AG-05)
  - Planner → executor → validator → critic loop
  - Inter-agent disagreement → drift spike
  - Consensus = drift minimization

No agent may plan + execute + validate in the same authority lane.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


# Drift delta per dissenting agent during consensus evaluation
DRIFT_DELTA_PER_DISSENTER = 0.1

# Minimum agreement score for validation acceptance
VALIDATION_AGREEMENT_THRESHOLD = 0.5


class AgentRole(str, Enum):
    """Roles an agent may occupy. AG-05: single role per agent."""

    PLANNER = "planner"
    EXECUTOR = "executor"
    VALIDATOR = "validator"
    CRITIC = "critic"


@dataclass
class AgentCard:
    """Registry entry for a federated agent."""

    agent_id: str
    role: AgentRole
    name: str
    capabilities: List[str] = field(default_factory=list)
    trust_score: float = 1.0
    drift_impact: float = 0.0
    execution_count: int = 0
    failure_count: int = 0
    registered_at: float = 0.0

    def __post_init__(self) -> None:
        if self.registered_at == 0.0:
            self.registered_at = time.time()

    @property
    def success_rate(self) -> float:
        if self.execution_count == 0:
            return 1.0
        return 1.0 - (self.failure_count / self.execution_count)

    @property
    def reputation(self) -> float:
        return self.trust_score * self.success_rate


@dataclass
class AgentProposal:
    """A proposal from an agent to be evaluated by the federation."""

    agent_id: str
    role: AgentRole
    content: Dict[str, Any]
    confidence: float = 1.0
    reasoning: str = ""
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class ConsensusResult:
    """Result of multi-agent consensus process."""

    accepted: bool
    final_proposal: Dict[str, Any]
    agreement_score: float
    drift_delta: float
    participating_agents: List[str]
    dissenting_agents: List[str] = field(default_factory=list)
    iteration_count: int = 0
    reason: str = ""


class LLMBackend(Protocol):
    """Protocol for LLM backends used by agents."""

    def decide_next_action(
        self,
        objective: str,
        history: list[Dict[str, Any]],
        forbidden_actions: list[str],
        drift: float,
    ) -> Dict[str, Any]: ...


class AgentRegistry:
    """
    Registry for federated agents with role isolation enforcement.

    Enforces AG-05: no agent may hold multiple roles that span
    plan + execute + validate authority.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentCard] = {}

    def register(
        self,
        name: str,
        role: AgentRole,
        capabilities: Optional[List[str]] = None,
        trust_score: float = 1.0,
    ) -> AgentCard:
        """Register a new agent with a single role."""
        agent_id = f"agent_{role.value}_{uuid.uuid4().hex[:8]}"
        card = AgentCard(
            agent_id=agent_id,
            role=role,
            name=name,
            capabilities=capabilities or [],
            trust_score=trust_score,
        )
        self._agents[agent_id] = card
        return card

    def get(self, agent_id: str) -> Optional[AgentCard]:
        """Retrieve an agent card by ID."""
        return self._agents.get(agent_id)

    def get_by_role(self, role: AgentRole) -> List[AgentCard]:
        """Get all agents with a specific role."""
        return [a for a in self._agents.values() if a.role == role]

    def deregister(self, agent_id: str) -> bool:
        """Remove an agent from the registry."""
        return self._agents.pop(agent_id, None) is not None

    def list_agents(self) -> List[AgentCard]:
        """List all registered agents."""
        return list(self._agents.values())

    def update_trust(self, agent_id: str, delta: float) -> None:
        """Adjust an agent's trust score."""
        card = self._agents.get(agent_id)
        if card:
            card.trust_score = max(0.0, min(1.0, card.trust_score + delta))

    def record_execution(self, agent_id: str, success: bool) -> None:
        """Record an execution outcome for reputation tracking."""
        card = self._agents.get(agent_id)
        if card:
            card.execution_count += 1
            if not success:
                card.failure_count += 1


class MultiAgentOrchestrator:
    """
    Federated agent orchestrator implementing the governed loop:
      planner → executor → validator → critic → finalize

    Consensus is achieved through drift minimization. Inter-agent
    disagreement triggers drift spikes that are tracked and resolved.

    AG-05 Enforcement: Role isolation is structural — the orchestrator
    delegates to agents strictly by their registered role.
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        max_iterations: int = 3,
        consensus_threshold: float = 0.7,
        drift_spike_threshold: float = 0.3,
    ) -> None:
        self.registry = registry or AgentRegistry()
        self.max_iterations = max_iterations
        self.consensus_threshold = consensus_threshold
        self.drift_spike_threshold = drift_spike_threshold
        self._proposals: List[AgentProposal] = []

    def submit_proposal(self, proposal: AgentProposal) -> None:
        """Submit an agent proposal for evaluation."""
        self._proposals.append(proposal)

    def evaluate_consensus(self, proposals: List[AgentProposal]) -> ConsensusResult:
        """
        Evaluate consensus across proposals using reputation-weighted scoring.

        Agreement is measured as the inverse of proposal divergence,
        weighted by agent reputation. High disagreement → drift spike.
        """
        if not proposals:
            return ConsensusResult(
                accepted=False,
                final_proposal={},
                agreement_score=0.0,
                drift_delta=0.0,
                participating_agents=[],
                reason="no proposals submitted",
            )

        # Reputation-weighted selection
        total_weight = 0.0
        weighted_proposals: List[tuple[float, AgentProposal]] = []

        for prop in proposals:
            card = self.registry.get(prop.agent_id)
            weight = (card.reputation if card else 0.5) * prop.confidence
            weighted_proposals.append((weight, prop))
            total_weight += weight

        if total_weight == 0.0:
            total_weight = 1.0

        # Select highest-weighted proposal as base
        weighted_proposals.sort(key=lambda x: x[0], reverse=True)
        best_weight, best_proposal = weighted_proposals[0]

        # Calculate agreement score (0.0–1.0)
        # Using confidence-weighted consensus
        agreement = best_weight / total_weight if total_weight > 0 else 0.0

        # Identify dissenters
        participating = [p.agent_id for _, p in weighted_proposals]
        dissenting = [
            p.agent_id
            for w, p in weighted_proposals
            if p.content != best_proposal.content and w > 0
        ]

        # Drift delta: disagreement causes drift spike
        drift_delta = len(dissenting) * DRIFT_DELTA_PER_DISSENTER if dissenting else 0.0

        accepted = agreement >= self.consensus_threshold

        return ConsensusResult(
            accepted=accepted,
            final_proposal=best_proposal.content,
            agreement_score=agreement,
            drift_delta=drift_delta,
            participating_agents=participating,
            dissenting_agents=dissenting,
            reason="consensus reached" if accepted else "insufficient agreement",
        )

    def run_governed_loop(
        self,
        objective: str,
        context: Dict[str, Any],
    ) -> ConsensusResult:
        """
        Execute the governed multi-agent loop:
          1. Planners propose action plans
          2. Executors refine into executable steps
          3. Validators check constraints and correctness
          4. Critics evaluate and may request re-planning

        Returns consensus result after bounded iterations.
        """
        planners = self.registry.get_by_role(AgentRole.PLANNER)
        executors = self.registry.get_by_role(AgentRole.EXECUTOR)
        validators = self.registry.get_by_role(AgentRole.VALIDATOR)
        critics = self.registry.get_by_role(AgentRole.CRITIC)

        current_plan: Dict[str, Any] = {"objective": objective, **context}
        final_result = ConsensusResult(
            accepted=False,
            final_proposal=current_plan,
            agreement_score=0.0,
            drift_delta=0.0,
            participating_agents=[],
            reason="not started",
        )

        for iteration in range(self.max_iterations):
            # Phase 1: Planning
            planner_proposals: List[AgentProposal] = []
            for planner in planners:
                proposal = AgentProposal(
                    agent_id=planner.agent_id,
                    role=AgentRole.PLANNER,
                    content={
                        "plan": f"plan_for_{objective}",
                        "iteration": iteration,
                        **current_plan,
                    },
                    confidence=planner.reputation,
                )
                planner_proposals.append(proposal)
                self.registry.record_execution(planner.agent_id, True)

            plan_consensus = self.evaluate_consensus(planner_proposals)
            if not plan_consensus.accepted and planners:
                continue

            current_plan = plan_consensus.final_proposal

            # Phase 2: Execution refinement
            exec_proposals: List[AgentProposal] = []
            for executor in executors:
                proposal = AgentProposal(
                    agent_id=executor.agent_id,
                    role=AgentRole.EXECUTOR,
                    content={
                        "execution": f"exec_{objective}",
                        "based_on": current_plan,
                    },
                    confidence=executor.reputation,
                )
                exec_proposals.append(proposal)
                self.registry.record_execution(executor.agent_id, True)

            exec_consensus = self.evaluate_consensus(exec_proposals)

            # Phase 3: Validation
            validation_proposals: List[AgentProposal] = []
            for validator in validators:
                is_valid = exec_consensus.agreement_score >= VALIDATION_AGREEMENT_THRESHOLD
                proposal = AgentProposal(
                    agent_id=validator.agent_id,
                    role=AgentRole.VALIDATOR,
                    content={
                        "validated": is_valid,
                        "plan": current_plan,
                        "execution": exec_consensus.final_proposal,
                    },
                    confidence=validator.reputation,
                )
                validation_proposals.append(proposal)
                self.registry.record_execution(validator.agent_id, True)

            val_consensus = self.evaluate_consensus(validation_proposals)

            # Phase 4: Critic evaluation
            critic_proposals: List[AgentProposal] = []
            for critic in critics:
                approve = val_consensus.accepted
                proposal = AgentProposal(
                    agent_id=critic.agent_id,
                    role=AgentRole.CRITIC,
                    content={
                        "approved": approve,
                        "feedback": "acceptable" if approve else "needs revision",
                    },
                    confidence=critic.reputation,
                )
                critic_proposals.append(proposal)
                self.registry.record_execution(critic.agent_id, True)

            critic_consensus = self.evaluate_consensus(critic_proposals)

            # Aggregate drift
            total_drift = (
                plan_consensus.drift_delta
                + exec_consensus.drift_delta
                + val_consensus.drift_delta
                + critic_consensus.drift_delta
            )

            all_agents = list(
                set(
                    plan_consensus.participating_agents
                    + exec_consensus.participating_agents
                    + val_consensus.participating_agents
                    + critic_consensus.participating_agents
                )
            )

            final_result = ConsensusResult(
                accepted=critic_consensus.accepted,
                final_proposal={
                    "plan": current_plan,
                    "execution": exec_consensus.final_proposal,
                    "validation": val_consensus.final_proposal,
                    "critic": critic_consensus.final_proposal,
                },
                agreement_score=critic_consensus.agreement_score,
                drift_delta=total_drift,
                participating_agents=all_agents,
                dissenting_agents=critic_consensus.dissenting_agents,
                iteration_count=iteration + 1,
                reason="loop complete",
            )

            if critic_consensus.accepted:
                break

        return final_result
