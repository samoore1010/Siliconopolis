from .base import Agent, AgentOutcome  # noqa: F401

# run-all order; the Editor sits between the Creator and QA because its output goes back through QA
AGENT_NAMES = ("researcher", "creator", "editor", "qa", "lister", "analyst", "fulfiller")
DAILY_CHAIN = ("creator", "qa", "lister")
WEEKLY_RESEARCH = ("researcher",)
WEEKLY_ANALYSIS = ("analyst",)


def registry() -> dict[str, type[Agent]]:
    from .analyst import AnalystAgent
    from .creator import CreatorAgent
    from .editor import EditorAgent
    from .fulfiller import FulfillerAgent
    from .lister import ListerAgent
    from .qa import QAAgent
    from .researcher import ResearcherAgent

    return {
        "researcher": ResearcherAgent,
        "creator": CreatorAgent,
        "editor": EditorAgent,
        "qa": QAAgent,
        "lister": ListerAgent,
        "analyst": AnalystAgent,
        "fulfiller": FulfillerAgent,
    }


def get_agent(name: str) -> Agent:
    reg = registry()
    if name not in reg:
        raise KeyError(f"unknown agent {name!r}; choose from {', '.join(reg)}")
    return reg[name]()


def prompt_file_for(name: str) -> str:
    """The prompts/*.md file an agent uses (without instantiating it)."""
    return f"prompts/{registry()[name].prompt_file}.md"
