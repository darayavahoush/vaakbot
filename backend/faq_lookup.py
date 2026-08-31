import json, os, re
from rapidfuzz import process, fuzz

# Common contraction -> expanded form, applied to the incoming query only
# (not to indexed FAQ questions) so casual phrasing like "whats autism"
# fuzzy-matches as well as "what is autism". token_sort_ratio otherwise
# penalizes contractions heavily since they collapse two tokens into one.
_CONTRACTIONS = {
    r"\bwhat's\b": "what is", r"\bwhats\b": "what is",
    r"\bwhere's\b": "where is", r"\bwheres\b": "where is",
    r"\bhow's\b": "how is", r"\bhows\b": "how is",
    r"\bwho's\b": "who is", r"\bwhos\b": "who is",
    r"\bdon't\b": "do not", r"\bdont\b": "do not",
    r"\bcan't\b": "cannot", r"\bcant\b": "cannot",
    r"\bwon't\b": "will not", r"\bwont\b": "will not",
    r"\bisn't\b": "is not", r"\bisnt\b": "is not",
    r"\baren't\b": "are not", r"\barent\b": "are not",
    r"\bdoesn't\b": "does not", r"\bdoesnt\b": "does not",
}
_CONTRACTION_RE = {re.compile(p, re.IGNORECASE): r for p, r in _CONTRACTIONS.items()}


def _normalize(text: str) -> str:
    t = text
    for pattern, repl in _CONTRACTION_RE.items():
        t = pattern.sub(repl, t)
    return t

FAQ_JSON_PATH = os.environ.get("FAQ_LOOKUP_PATH", "faq_lookup.json")

class FaqLookup:
    def __init__(self, path=FAQ_JSON_PATH):
        if os.path.exists(path):
            with open(path) as f:
                pairs = json.load(f)
        else:
            pairs = []
        self.questions = [p["question"] for p in pairs]
        self.pairs = pairs

    def match(self, user_msg: str, threshold: int = 88):
        if not self.questions:
            return None
        result = process.extractOne(_normalize(user_msg), self.questions, scorer=fuzz.token_sort_ratio)
        if result is None:
            return None
        match, score, idx = result
        if score >= threshold:
            return {**self.pairs[idx], "score": score}
        return None
