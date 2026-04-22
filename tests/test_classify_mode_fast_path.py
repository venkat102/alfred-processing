"""Fast-path extensions: analytics verbs route to Insights; deploy verbs beat them."""

import pytest

from alfred.orchestrator import _fast_path


@pytest.mark.parametrize("prompt", [
	"show top 10 customers by revenue this quarter",
	"show the top 5 suppliers",
	"show me top 20 items by quantity",
	"list the top 3 sales orders",
	"count of customers by group",
	"summarize attendance for this month",
	"summary of open sales invoices",
	"report on purchase orders",
])
def test_analytics_prompts_fast_path_to_insights(prompt):
	assert _fast_path(prompt) == "insights"


@pytest.mark.parametrize("prompt", [
	"build a Report DocType for top customers",
	"create a report that lists our top suppliers",
	"add a Report DocType summarising attendance",
	"make a report counting invoices",
])
def test_deploy_verbs_beat_analytics_prompts(prompt):
	assert _fast_path(prompt) == "dev"


def test_existing_how_many_still_routes_to_insights():
	assert _fast_path("how many sales orders this month") == "insights"


def test_existing_list_all_still_routes_to_insights():
	assert _fast_path("list all suppliers") == "insights"


def test_truly_ambiguous_falls_through_to_llm():
	# These need the LLM to read the whole sentence.
	assert _fast_path("I need something for revenue reporting") is None
