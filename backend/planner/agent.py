"""
Financial Planner Orchestrator Agent - coordinates portfolio analysis across specialized agents.

Implements two key infrastructure patterns:
1. Request Routing: A deterministic route_request() function evaluates each request's properties
   (portfolio size, retirement goals) and decides which agents to invoke and which model tier to use.
2. Distributed Orchestration: Selected agents are dispatched in parallel via async Lambda invocations
   and coordinated through shared database state polling.
"""

import os
import json
import boto3
import asyncio
import logging
import time
from typing import Dict, List, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field

from agents import function_tool, RunContextWrapper
from agents.extensions.models.litellm_model import LitellmModel

logger = logging.getLogger()

# Initialize Lambda client
lambda_client = boto3.client("lambda")

# Lambda function names from environment
TAGGER_FUNCTION = os.getenv("TAGGER_FUNCTION", "alex-tagger")
REPORTER_FUNCTION = os.getenv("REPORTER_FUNCTION", "alex-reporter")
CHARTER_FUNCTION = os.getenv("CHARTER_FUNCTION", "alex-charter")
RETIREMENT_FUNCTION = os.getenv("RETIREMENT_FUNCTION", "alex-retirement")
MOCK_LAMBDAS = os.getenv("MOCK_LAMBDAS", "false").lower() == "true"

# Agent name to Lambda function name mapping
AGENT_FUNCTION_MAP = {
    "reporter": REPORTER_FUNCTION,
    "charter": CHARTER_FUNCTION,
    "retirement": RETIREMENT_FUNCTION,
}

# Agent name to DB payload column mapping (for completion polling)
AGENT_PAYLOAD_COLUMNS = {
    "reporter": "report_payload",
    "charter": "charts_payload",
    "retirement": "retirement_payload",
}

# Polling configuration
POLL_INTERVAL_SECONDS = 5
POLL_MAX_WAIT_SECONDS = 600  # 10 minutes


# ========================================
# Request Routing
# ========================================

@dataclass
class RoutingDecision:
    """Result of the request routing function."""
    agents: List[str]
    model_tier: str  # "lite" or "full"
    reasoning: str


def route_request(portfolio_summary: Dict[str, Any]) -> RoutingDecision:
    """
    Deterministic request routing function.
    
    Evaluates the portfolio summary properties and decides:
    1. Which agents to invoke (routing)
    2. Which model tier to use for the planner itself (cost optimization)
    
    This runs BEFORE the LLM agent starts — the routing decision is not made by the LLM.
    """
    agents = []
    reasons = []

    num_positions = portfolio_summary.get("num_positions", 0)
    years_until_retirement = portfolio_summary.get("years_until_retirement")
    
    # Reporter: invoke if portfolio has any positions
    if num_positions > 0:
        agents.append("reporter")
        reasons.append(f"reporter: portfolio has {num_positions} positions")
    else:
        reasons.append("reporter: SKIPPED (no positions)")

    # Charter: invoke if portfolio has enough positions for meaningful charts
    if num_positions >= 2:
        agents.append("charter")
        reasons.append(f"charter: {num_positions} positions sufficient for visualization")
    else:
        reasons.append(f"charter: SKIPPED ({num_positions} positions insufficient for charts)")

    # Retirement: invoke only if user has meaningful retirement goals
    # The frontend slider goes 0-50, so 0 means "not set" (no opt-out control).
    # Also require a positive target income for projections to be useful.
    target_income = portfolio_summary.get("target_retirement_income")
    has_retirement_goals = (
        years_until_retirement is not None
        and years_until_retirement > 0
        and target_income is not None
        and target_income > 0
    )
    if has_retirement_goals:
        agents.append("retirement")
        reasons.append(f"retirement: user has retirement goal ({years_until_retirement} years, ${target_income:,.0f}/yr)")
    else:
        reasons.append("retirement: SKIPPED (no retirement goals configured)")

    # Model tier routing: the planner's own orchestration work is lightweight
    # (just dispatch + wait), so always use lite model for the planner itself.
    # Complex portfolios (5+ positions) are handled by the sub-agents which 
    # use their own model configuration.
    model_tier = "lite"
    reasons.append(f"model_tier: lite (planner only dispatches and waits)")

    decision = RoutingDecision(
        agents=agents,
        model_tier=model_tier,
        reasoning="; ".join(reasons)
    )

    logger.info(json.dumps({
        "event": "ROUTING_DECISION",
        "agents": decision.agents,
        "model_tier": decision.model_tier,
        "reasoning": decision.reasoning,
        "num_positions": num_positions,
        "has_retirement_goals": has_retirement_goals,
    }))

    return decision


# ========================================
# Planner Context
# ========================================

@dataclass
class PlannerContext:
    """Context for planner agent tools."""
    job_id: str
    routed_agents: List[str] = field(default_factory=list)
    db: Any = None


# ========================================
# Distributed Dispatch & Completion Polling
# ========================================

async def dispatch_agent_async(agent_name: str, job_id: str) -> Dict[str, Any]:
    """
    Fire a single agent Lambda asynchronously (fire-and-forget).
    Uses InvocationType="Event" so the call returns immediately without waiting
    for the agent to complete. The agent writes its results directly to the DB.
    """
    function_name = AGENT_FUNCTION_MAP.get(agent_name)
    if not function_name:
        return {"agent": agent_name, "error": f"Unknown agent: {agent_name}"}

    if MOCK_LAMBDAS:
        logger.info(f"[MOCK] Would dispatch {agent_name} async for job {job_id}")
        return {"agent": agent_name, "status": "dispatched", "mock": True}

    try:
        logger.info(f"Dispatching {agent_name} async via Lambda: {function_name}")
        
        # InvocationType="Event" makes this fire-and-forget
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps({"job_id": job_id}),
        )
        
        status_code = response.get("StatusCode", 0)
        # Event invocations return 202 on successful dispatch
        if status_code == 202:
            logger.info(f"{agent_name} dispatched successfully (202 Accepted)")
            return {"agent": agent_name, "status": "dispatched"}
        else:
            logger.warning(f"{agent_name} dispatch returned unexpected status: {status_code}")
            return {"agent": agent_name, "status": "dispatched", "http_status": status_code}

    except Exception as e:
        logger.error(f"Error dispatching {agent_name}: {e}")
        return {"agent": agent_name, "error": str(e)}


def check_agent_completion(job_id: str, agents: List[str], db) -> Dict[str, bool]:
    """
    Check which agents have written their results to the database.
    Returns a dict mapping agent name to completion status.
    """
    job = db.jobs.find_by_id(job_id)
    if not job:
        return {agent: False for agent in agents}

    completion = {}
    for agent_name in agents:
        column = AGENT_PAYLOAD_COLUMNS.get(agent_name)
        if column:
            completion[agent_name] = job.get(column) is not None
        else:
            completion[agent_name] = False

    return completion


# ========================================
# Agent Tools (used by the Planner LLM)
# ========================================

@function_tool
async def dispatch_agents(wrapper: RunContextWrapper[PlannerContext], agents: List[str]) -> str:
    """
    Dispatch the specified agents to run in parallel.
    Each agent runs in its own isolated Lambda environment and writes results directly to the database.
    Only dispatch agents from the pre-approved routing list.

    Args:
        agents: List of agent names to dispatch. Valid values: "reporter", "charter", "retirement"
    """
    job_id = wrapper.context.job_id
    routed = wrapper.context.routed_agents

    # Enforce routing decision: only allow agents that were approved by the router
    approved = [a for a in agents if a in routed]
    rejected = [a for a in agents if a not in routed]

    if rejected:
        logger.warning(f"Planner tried to dispatch non-routed agents: {rejected}. Blocked by routing policy.")

    if not approved:
        return "No agents to dispatch. All requested agents were blocked by routing policy."

    # Dispatch all approved agents in parallel using asyncio.gather
    dispatch_tasks = [dispatch_agent_async(agent, job_id) for agent in approved]
    results = await asyncio.gather(*dispatch_tasks)

    dispatched = [r["agent"] for r in results if r.get("status") == "dispatched"]
    failed = [r for r in results if "error" in r]

    summary = f"Dispatched {len(dispatched)} agents in parallel: {dispatched}."
    if failed:
        summary += f" Failed to dispatch: {[f['agent'] for f in failed]}."

    logger.info(f"Planner: {summary}")
    return summary


@function_tool
async def wait_for_completion(wrapper: RunContextWrapper[PlannerContext], agents: List[str]) -> str:
    """
    Wait for the specified agents to complete by polling the database.
    Each agent writes its results to a dedicated column in the jobs table.
    Returns when all agents have completed or timeout is reached.

    Args:
        agents: List of agent names to wait for. Must match what was dispatched.
    """
    job_id = wrapper.context.job_id
    db = wrapper.context.db

    if MOCK_LAMBDAS:
        logger.info(f"[MOCK] Simulating completion wait for agents: {agents}")
        return f"All agents completed: {agents}"

    start_time = time.time()
    
    while True:
        elapsed = time.time() - start_time
        
        if elapsed > POLL_MAX_WAIT_SECONDS:
            completion = check_agent_completion(job_id, agents, db)
            incomplete = [a for a, done in completion.items() if not done]
            logger.error(f"Planner: Timeout waiting for agents: {incomplete}")
            return f"Timeout after {int(elapsed)}s. Completed: {[a for a, done in completion.items() if done]}. Still pending: {incomplete}."

        completion = check_agent_completion(job_id, agents, db)
        
        if all(completion.values()):
            logger.info(f"Planner: All agents completed in {int(elapsed)}s: {agents}")
            return f"All {len(agents)} agents completed successfully in {int(elapsed)} seconds: {agents}"

        completed = [a for a, done in completion.items() if done]
        pending = [a for a, done in completion.items() if not done]
        logger.info(f"Planner: Polling - completed: {completed}, pending: {pending} ({int(elapsed)}s elapsed)")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ========================================
# Pre-processing (runs before LLM agent)
# ========================================

def handle_missing_instruments(job_id: str, db) -> None:
    """
    Check for and tag any instruments missing allocation data.
    This is done automatically before the agent runs.
    """
    logger.info("Planner: Checking for instruments missing allocation data...")

    job = db.jobs.find_by_id(job_id)
    if not job:
        logger.error(f"Job {job_id} not found")
        return

    user_id = job["clerk_user_id"]
    accounts = db.accounts.find_by_user(user_id)

    missing = []
    for account in accounts:
        positions = db.positions.find_by_account(account["id"])
        for position in positions:
            instrument = db.instruments.find_by_symbol(position["symbol"])
            if instrument:
                has_allocations = bool(
                    instrument.get("allocation_regions")
                    and instrument.get("allocation_sectors")
                    and instrument.get("allocation_asset_class")
                )
                if not has_allocations:
                    missing.append(
                        {"symbol": position["symbol"], "name": instrument.get("name", "")}
                    )
            else:
                missing.append({"symbol": position["symbol"], "name": ""})

    if missing:
        logger.info(
            f"Planner: Found {len(missing)} instruments needing classification: {[m['symbol'] for m in missing]}"
        )

        try:
            response = lambda_client.invoke(
                FunctionName=TAGGER_FUNCTION,
                InvocationType="RequestResponse",
                Payload=json.dumps({"instruments": missing}),
            )

            result = json.loads(response["Payload"].read())

            if isinstance(result, dict) and "statusCode" in result:
                if result["statusCode"] == 200:
                    logger.info(
                        f"Planner: InstrumentTagger completed - Tagged {len(missing)} instruments"
                    )
                else:
                    logger.error(
                        f"Planner: InstrumentTagger failed with status {result['statusCode']}"
                    )

        except Exception as e:
            logger.error(f"Planner: Error tagging instruments: {e}")
    else:
        logger.info("Planner: All instruments have allocation data")


def load_portfolio_summary(job_id: str, db) -> Dict[str, Any]:
    """Load basic portfolio summary statistics only."""
    try:
        job = db.jobs.find_by_id(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        user_id = job["clerk_user_id"]
        user = db.users.find_by_clerk_id(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        accounts = db.accounts.find_by_user(user_id)
        
        # Calculate simple summary statistics
        total_value = 0.0
        total_positions = 0
        total_cash = 0.0
        
        for account in accounts:
            total_cash += float(account.get("cash_balance", 0))
            positions = db.positions.find_by_account(account["id"])
            total_positions += len(positions)
            
            # Add position values
            for position in positions:
                instrument = db.instruments.find_by_symbol(position["symbol"])
                if instrument and instrument.get("current_price"):
                    price = float(instrument["current_price"])
                    quantity = float(position["quantity"])
                    total_value += price * quantity
        
        total_value += total_cash
        
        # Return only summary statistics - preserve None for missing retirement goals
        # so the routing function can distinguish "no goal" from "default goal"
        raw_years = user.get("years_until_retirement")
        raw_target = user.get("target_retirement_income")
        
        return {
            "total_value": total_value,
            "num_accounts": len(accounts),
            "num_positions": total_positions,
            "years_until_retirement": int(raw_years) if raw_years is not None else None,
            "target_retirement_income": float(raw_target) if raw_target is not None else None,
        }

    except Exception as e:
        logger.error(f"Error loading portfolio summary: {e}")
        raise


# ========================================
# Agent Creation
# ========================================

def create_agent(job_id: str, portfolio_summary: Dict[str, Any], routing: RoutingDecision, db):
    """Create the orchestrator agent with tools and routing context."""
    
    # Create context with routing decision and DB reference for polling
    context = PlannerContext(
        job_id=job_id,
        routed_agents=routing.agents,
        db=db,
    )

    # Model selection: planner uses lite model (only dispatches and waits)
    lite_model_id = os.getenv("BEDROCK_LITE_MODEL_ID", os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0"))
    bedrock_region = os.getenv("BEDROCK_REGION", "us-west-2")
    os.environ["AWS_REGION_NAME"] = bedrock_region

    logger.info(f"Planner: Using model={lite_model_id}, region={bedrock_region}")
    model = LitellmModel(model=f"bedrock/{lite_model_id}")

    tools = [
        dispatch_agents,
        wait_for_completion,
    ]

    # Task includes the routing decision so the LLM knows exactly what to dispatch
    agents_str = ", ".join(routing.agents) if routing.agents else "none"
    task = f"Job {job_id}: dispatch agents [{agents_str}] and wait for completion."

    return model, tools, task, context
