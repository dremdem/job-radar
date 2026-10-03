"""What Claude is told about the user and the vacancy, and the answer's schema."""

from typing import Any

from ..stack import Profile

PROMPT_VERSION = "1"
# Never inferred from the CV: stated by the user (docs/claude.md#what-claude-gets).
FACTS = [
    "I want full remote work only.",
    "I live in Thailand and will not relocate.",
    "Any timezone is fine for me; working hours are not a problem.",
    "I speak Russian (native) and English (professional, B2+); Russian is not a problem.",
]

_QUOTED = {
    "type": "object",
    "properties": {"point": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["point", "quote"],
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["apply", "maybe", "skip"]},
        "match": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "location": {
            "type": "object",
            "properties": {
                "verdict": {"enum": ["ok", "risk", "blocker"]},
                "note": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": ["verdict", "note", "quote"],
        },
        "strengths": {"type": "array", "items": _QUOTED},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "requirement": {"enum": ["must_have", "nice_to_have"]},
                    "blocker": {"type": "boolean"},
                    "note": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["skill", "requirement", "blocker", "note", "quote"],
            },
        },
        "red_flags": {"type": "array", "items": _QUOTED},
        "questions": {"type": "array", "items": {"type": "string"}},
        "cv_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "verdict",
        "match",
        "summary",
        "location",
        "strengths",
        "gaps",
        "red_flags",
        "questions",
        "cv_keywords",
    ],
}


def system_prompt(language: str) -> str:
    return (
        "You help a senior Python backend developer decide which job vacancies are worth "
        "applying to. Judge only from the vacancy text, the CV and the candidate's rules; "
        "never invent facts about the company or the job. Every quote must be copied exactly "
        "from the vacancy text (one sentence or line); use an empty string when there is none. "
        f"Write all explanations in {language}; keep quotes in the original language."
    )


def chat_system_prompt(language: str) -> str:
    return (
        "You are continuing a conversation about one job vacancy with the candidate whose CV, "
        "rules and the vacancy you saw at the start. Answer the candidate's question in plain "
        "text (short paragraphs and lists are fine), based only on the vacancy, the CV and what "
        f"was said before; say so when something is unknown. Answer in {language}."
    )


def _list(title: str, items: list[str]) -> str:
    return f"{title}:\n" + ("\n".join(f"- {i}" for i in items) if items else "- (none)")


def analysis_prompt(
    *,
    cv: str | None,
    profile: Profile | None,
    title: str,
    company: str,
    location: str | None,
    salary: str | None,
    description: str,
) -> str:
    profile = profile or Profile()
    parts = [
        "# About me",
        _list("Facts", FACTS),
        _list("My rules", profile.rules),
        _list("Technologies I know", profile.known),
        _list("Technologies I can pick up quickly (not a blocker)", profile.learnable),
        _list("Blockers if required at a professional level", profile.blockers),
        "# My CV",
        cv.strip() if cv else "(no CV given: judge from the lists above)",
        "# The vacancy",
        f"Title: {title}",
        f"Company: {company}",
        f"Location: {location or 'unknown'}",
        f"Salary: {salary or 'unknown'}",
        "Description:",
        description.strip(),
        "# Task",
        "Decide whether I should apply. verdict: apply / maybe / skip. match: 0-100. "
        "location: can I do this job fully remote from Thailand? gaps: required skills I lack "
        "or know weakly; requirement must_have or nice_to_have as the text says; blocker when "
        "it breaks one of my rules or is a blocker above. red_flags: anything else against it "
        "(language, contract type, seniority, salary). questions: what to ask the recruiter. "
        "cv_keywords: words from the vacancy worth having in my CV.",
    ]
    return "\n\n".join(parts)
