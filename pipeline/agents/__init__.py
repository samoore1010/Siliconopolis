from .base import Agent, AgentOutcome  # noqa: F401

AGENT_NAMES = ("researcher", "creator", "qa", "lister", "analyst", "fulfiller")
DAILY_CHAIN = ("creator", "qa", "lister")
WEEKLY_RESEARCH = ("researcher",)
WEEKLY_ANALYSIS = ("analyst",)


def get_agent(name: str) -> Agent:
    from .analyst import AnalystAgent
    from .creator import CreatorAgent
    from .fulfiller import FulfillerAgent
    from .lister import ListerAgent
    from .qa import QAAgent
    from .researcher import ResearcherAgent

    registry = {
        "researcher": ResearcherAgent,
        "creator": CreatorAgent,
        "qa": QAAgent,
        "lister": ListerAgent,
        "analyst": AnalystAgent,
        "fulfiller": FulfillerAgent,
    }
    if name not in registry:
        raise KeyError(f"unknown agent {name!r}; choose from {', '.join(registry)}")
    return registry[name]()
