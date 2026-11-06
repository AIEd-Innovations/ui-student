"""Policy values shared by the standalone Guardian and its callers."""

from enum import Enum


class QuestionClassification(str, Enum):
    CONCEPTUAL = "CONCEPTUAL"
    PROCEDURAL = "PROCEDURAL"
    CLARIFICATION = "CLARIFICATION"
    DIRECT_SOLUTION = "DIRECT_SOLUTION"
    ANSWER_FARMING = "ANSWER_FARMING"


class GuidanceLevel(str, Enum):
    FULL = "FULL"
    MODERATE = "MODERATE"
    MINIMAL = "MINIMAL"
    REJECTED = "REJECTED"
