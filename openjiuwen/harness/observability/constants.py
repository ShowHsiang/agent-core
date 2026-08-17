# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared naming constants for single-agent observability."""

from __future__ import annotations

# Synthetic team name carried by a single agent and its sub-agents.
#
# The agent-tier rail keys its spans off ``agent.team_name`` and returns early
# for an agent without one, so a genuinely team-less run would produce no
# agent-tier span at all. Stamping this marker gives the single agent its own
# tier while keeping real team members (which already carry a team name)
# untouched.
SINGLE_AGENT_TEAM_NAME = "single-agent"

__all__ = ["SINGLE_AGENT_TEAM_NAME"]
