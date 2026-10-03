"""Technologies a vacancy may require and the user's own stack. See docs/ai.md#stack-vs-your-cv."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Tech:
    name: str
    pattern: str  # regex; case-insensitive unless `case` is set
    what: str  # how the model is told what is meant
    case: bool = False

    def search(self, text: str) -> re.Match[str] | None:
        return re.search(self.pattern, text, 0 if self.case else re.IGNORECASE)


def _t(name: str, what: str, pattern: str | None = None, case: bool = False) -> Tech:
    return Tech(name, pattern or rf"(?<![\w.]){re.escape(name)}(?![\w#+])", what, case)


# Common in backend / data vacancies. Basics (Git, SQL, REST, Linux) are left out on purpose.
CATALOGUE: list[Tech] = [
    # Languages
    _t("Go", "the Go / Golang programming language", r"\bGo\b(?![!-])|(?i:\bgolang\b)", case=True),
    _t("Java", "the Java programming language", r"\bJava\b(?!\s*Script)"),
    _t("Kotlin", "the Kotlin programming language"),
    _t("Scala", "the Scala programming language"),
    _t("Rust", "the Rust programming language", r"\bRust\b", case=True),
    _t("C++", "the C++ programming language", r"(?<!\w)C\+\+"),
    _t("C#", "the C# / .NET platform", r"(?<!\w)C#|\.NET\b"),
    _t("Ruby", "the Ruby language / Ruby on Rails", r"\bRuby\b|\bRails\b"),
    _t("PHP", "the PHP language", r"\bPHP\b"),
    _t("Node.js", "Node.js on the backend", r"\bNode(\.?js)?\b"),
    _t("Elixir", "the Elixir language"),
    _t("Swift", "Swift / iOS development", r"\bSwift\b|\biOS\b"),
    _t("Solidity", "Solidity smart contracts"),
    # Frontend
    _t("Vue", "the Vue.js framework", r"\bVue(\.?js)?\b"),
    _t("Angular", "the Angular framework", r"\bAngular\b"),
    # Data stores
    _t("Redis", "Redis"),
    _t("Cassandra", "Apache Cassandra"),
    _t("DynamoDB", "Amazon DynamoDB"),
    _t("ClickHouse", "ClickHouse"),
    _t("Snowflake", "the Snowflake data warehouse"),
    _t("BigQuery", "Google BigQuery"),
    _t("Neo4j", "the Neo4j graph database"),
    # Messaging and APIs
    _t("Kafka", "Apache Kafka"),
    _t("RabbitMQ", "RabbitMQ"),
    _t("gRPC", "gRPC"),
    # Cloud and infrastructure
    _t("Azure", "Microsoft Azure"),
    _t("Terraform", "HashiCorp Terraform"),
    _t("Ansible", "Ansible"),
    _t("Helm", "Helm charts for Kubernetes", r"\bHelm\b"),
    _t("Istio", "the Istio service mesh"),
    _t("Kubernetes", "Kubernetes", r"\bKubernetes\b|\bK8s\b"),
    # Observability
    _t("Prometheus", "Prometheus monitoring"),
    _t("Grafana", "Grafana"),
    _t("Datadog", "Datadog"),
    _t("OpenTelemetry", "OpenTelemetry"),
    # Data and ML
    _t("Spark", "Apache Spark / PySpark", r"\b(Py)?Spark\b"),
    _t("Flink", "Apache Flink"),
    _t("Databricks", "Databricks"),
    _t("dbt", "the dbt data build tool", r"\bdbt\b"),
    _t("Hadoop", "Hadoop"),
    _t("PyTorch", "PyTorch"),
    _t("TensorFlow", "TensorFlow"),
    _t("CUDA", "CUDA / GPU programming"),
]


LISTS = ("known", "learnable", "blockers", "rules")
_HEADER = "# Your profile: edit here or on /profile. See docs/ai.md#stack-vs-your-cv.\n"


@dataclass(frozen=True)
class Profile:
    known: list[str] = field(default_factory=list)
    learnable: list[str] = field(default_factory=list)  # a required one costs less
    blockers: list[str] = field(default_factory=list)  # a required one is a blocker
    rules: list[str] = field(default_factory=list)  # free text for Claude (#105)

    @property
    def known_set(self) -> set[str]:
        return {name.lower() for name in self.known}

    def level(self, skill: str) -> str:
        """known / learnable / blocker / unknown."""
        name = skill.lower()
        if name in self.known_set:
            return "known"
        if name in {s.lower() for s in self.blockers}:
            return "blocker"
        if name in {s.lower() for s in self.learnable}:
            return "learnable"
        return "unknown"

    def extra_techs(self) -> list[Tech]:
        """Learnable and blocker items the catalogue does not know, matched by name."""
        catalogue = {t.name.lower() for t in CATALOGUE}
        return [
            _t(name, name)
            for name in [*self.learnable, *self.blockers]
            if name.lower() not in catalogue
        ]


def _items(value) -> list[str]:
    return [str(item).strip() for item in value or [] if str(item).strip()]


def load_profile(path: Path) -> Profile | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    return Profile(**{name: _items(data.get(name)) for name in LISTS})


def save_profile(path: Path, profile: Profile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {name: list(getattr(profile, name)) for name in LISTS}
    path.write_text(_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


_cache: dict[Path, tuple[float, Profile | None]] = {}


def current_profile(path: Path) -> Profile | None:
    """load_profile, re-read only when the file changed (the queue asks for every row)."""
    mtime = path.stat().st_mtime if path.exists() else -1.0
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        _cache[path] = (mtime, load_profile(path))
    return _cache[path][1]


def load_known(path: Path) -> set[str] | None:
    """Lower-cased technology names from the profile; None when there is no profile."""
    profile = load_profile(path)
    return profile.known_set if profile else None


def is_known(tech: Tech, known: set[str]) -> bool:
    return tech.name.lower() in known


def unknown_mentions(text: str, known: set[str], extra: list[Tech] | None = None) -> list[Tech]:
    """Catalogue (and extra) technologies the text mentions and the profile lacks, in order."""
    techs = [*CATALOGUE, *(extra or [])]
    found = [(m.start(), t) for t in techs if not is_known(t, known) and (m := t.search(text))]
    return [t for _, t in sorted(found, key=lambda pair: pair[0])]


_ALTERNATIVE = re.compile(
    r"\bone of\b|\bat least one\b|\beither\b|\bor\b|\bany\b|\bили\b|\bлибо\b|"
    r"не принципиал",
    re.IGNORECASE,
)
_NICE = re.compile(
    r"\bplus\b|\bbonus\b|nice to have|\bpreferred\b|\bideally\b|плюсом|желательно|"
    r"преимуществ",
    re.IGNORECASE,
)


def says_optional(quote: str) -> bool:
    """The quote itself calls the skill a plus / bonus / preferred."""
    return bool(_NICE.search(quote))


def known_alternative(quote: str, known: set[str]) -> bool:
    """The quote offers a choice that includes something the user knows: "Python or Go",
    "at least one of ...", or a known name joined by a slash ("AWS/GCP/Azure", not "CI/CD")."""
    names = [re.escape(name) for name in known]
    if not names:
        return False
    word = rf"(?<![\w.])(?:{'|'.join(names)})(?![\w])"
    if re.search(rf"{word}\s*/|/\s*{word}", quote, re.IGNORECASE):
        return True
    if not _ALTERNATIVE.search(quote):
        return False
    if any(t.search(quote) for t in CATALOGUE if is_known(t, known)):
        return True
    return bool(re.search(word, quote, re.IGNORECASE))
