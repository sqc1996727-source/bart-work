"""Product Agent execution limits; configurable per isolated enterprise deployment."""
from dataclasses import dataclass
import os
import re
import unicodedata

@dataclass(frozen=True)
class ExecutionPolicy:
    rounds: int = 48
    model_calls: int = 80
    seconds: int = 1200
    tokens: int = 250000
    retries: int = 2

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        def number(name, default, low, high):
            value = int(env.get(name, default))
            if not low <= value <= high:
                raise ValueError(name + " out of range")
            return value
        return cls(number("BOT_AGENT_ROUNDS",48,1,256),
                   number("BOT_AGENT_MODEL_CALLS",80,1,512),
                   number("BOT_AGENT_SECONDS",1200,10,7200),
                   number("BOT_AGENT_TOKENS",250000,0,10000000),
                   number("BOT_AGENT_RETRIES",2,0,5))

def query_terms(value):
    """CJK bigrams/trigrams + full identifiers; never fuzzy-match numeric IDs."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    terms = set()
    for chunk in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_-]+", normalized):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            if len(chunk) >= 2:
                terms.add(chunk)
                for size in (2,3):
                    terms.update(chunk[i:i+size] for i in range(len(chunk)-size+1))
        elif len(chunk) >= 2:
            terms.add(chunk)
    return terms

def relevance(terms, value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    score = 0
    for term in terms:
        if re.fullmatch(r"[a-z0-9_-]+", term) and any(c.isdigit() for c in term):
            matched = re.search(r"(?<![a-z0-9_-])"+re.escape(term)+r"(?![a-z0-9_-])", text)
        else:
            matched = term in text
        if matched:
            score += min(len(term), 12)
    return score
