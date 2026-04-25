"""Token usage tracking for CrewAI agent pipeline.

Captures token counts per agent step and aggregates them per conversation.
Sends usage data back to the client app for storage in Alfred Conversation.
"""

import json
import logging
import time

logger = logging.getLogger("alfred.tokens")


class TokenTracker:
	"""Tracks token usage across all agents in a conversation.

	Collects per-agent token counts and provides aggregated totals.
	Designed to be passed to CrewAI callbacks for automatic tracking.
	"""

	def __init__(self, conversation_id: str):
		self.conversation_id = conversation_id
		self.usage_by_agent: dict[str, dict[str, int]] = {}
		self.total_prompt_tokens = 0
		self.total_completion_tokens = 0
		self.total_tokens = 0
		self.started_at = time.time()

	def record_usage(self, agent_name: str, prompt_tokens: int, completion_tokens: int):
		"""Record token usage for a single agent call."""
		total = prompt_tokens + completion_tokens

		if agent_name not in self.usage_by_agent:
			self.usage_by_agent[agent_name] = {
				"prompt_tokens": 0,
				"completion_tokens": 0,
				"total_tokens": 0,
				"calls": 0,
			}

		self.usage_by_agent[agent_name]["prompt_tokens"] += prompt_tokens
		self.usage_by_agent[agent_name]["completion_tokens"] += completion_tokens
		self.usage_by_agent[agent_name]["total_tokens"] += total
		self.usage_by_agent[agent_name]["calls"] += 1

		self.total_prompt_tokens += prompt_tokens
		self.total_completion_tokens += completion_tokens
		self.total_tokens += total

		logger.debug(
			"Token usage: agent=%s, prompt=%d, completion=%d, total=%d (cumulative=%d)",
			agent_name, prompt_tokens, completion_tokens, total, self.total_tokens,
		)

	def get_summary(self) -> dict:
		"""Get aggregated usage summary for the conversation."""
		return {
			"conversation_id": self.conversation_id,
			"total_tokens": self.total_tokens,
			"prompt_tokens": self.total_prompt_tokens,
			"completion_tokens": self.total_completion_tokens,
			"by_agent": self.usage_by_agent,
			"duration_seconds": round(time.time() - self.started_at, 1),
		}

	def to_json(self) -> str:
		return json.dumps(self.get_summary())


def estimate_cost(total_tokens: int, model: str = "") -> dict:
	"""Estimate cost based on token usage and model.

	Rough estimates - actual costs depend on provider pricing.
	"""
	# Cost per 1M tokens (input/output averaged)
	model_costs = {
		"ollama": 0.0,  # Free, local
		"anthropic": 3.0,
		"openai": 2.5,
		"gemini": 1.25,
		"bedrock": 3.0,
	}

	provider = model.split("/")[0] if "/" in model else model
	cost_per_million = model_costs.get(provider, 2.5)
	estimated_cost = (total_tokens / 1_000_000) * cost_per_million

	return {
		"total_tokens": total_tokens,
		"model": model,
		"cost_per_million_tokens": cost_per_million,
		"estimated_cost_usd": round(estimated_cost, 6),
	}
