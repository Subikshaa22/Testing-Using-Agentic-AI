"""Central configuration: model settings for every agent and pipeline defaults.

All values here are reported in the run log (prompts.jsonl / report.md) so the
project report can state exactly which parameters were used.
"""
import os
from dataclasses import dataclass, field, asdict

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
RUNS_DIR = os.path.join(ROOT_DIR, "runs")

MBPP_URL = "https://raw.githubusercontent.com/google-research/google-research/master/mbpp/mbpp.jsonl"
MBPP_PATH = os.path.join(DATA_DIR, "mbpp.jsonl")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b:free"

# Hosted OpenAI-compatible providers: endpoint, the .env variable holding the key, default model.
# "custom" takes --base-url / $LLM_BASE_URL and $LLM_API_KEY.
PROVIDERS = {
    "openrouter": {"base_url": DEFAULT_BASE_URL, "key_env": "OPENROUTER_API_KEY",
                   "model": DEFAULT_MODEL},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model": "qwen/qwen3.8-27b"},
    "custom": {"base_url": None, "key_env": "LLM_API_KEY", "model": None},
}

MODES = ("coverage", "property", "statistical")


@dataclass
class LLMSettings:
    """Sampling parameters sent with every chat-completion request."""
    temperature: float
    top_p: float = 1.0
    max_tokens: int = 4000
    seed: int = 42

    def to_dict(self):
        return asdict(self)


@dataclass
class PipelineConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    provider: str = "openai-compatible"  # or "mock"
    # Test requirement the test generator must satisfy: coverage | property | statistical
    mode: str = "coverage"
    # --- coverage mode ---
    criterion: str = "branch"   # statement | branch | loop | both | all
    target: float = 100.0       # required coverage percentage
    # --- statistical mode ---
    samples: int = 5            # n: test suites generated per task
    ks: tuple = (1, 3, 5)       # k values for pass@k
    # Per-subprocess timeout (seconds) for executing generated code/tests
    exec_timeout: int = 30
    # Low temperature for code: we want the most likely correct implementation.
    code_settings: LLMSettings = field(default_factory=lambda: LLMSettings(temperature=0.2))
    # Test generation temperature (0.8 by default in statistical mode, where
    # independent, diverse samples are the point).
    test_settings: LLMSettings = field(default_factory=lambda: LLMSettings(temperature=0.4, max_tokens=6000))

    def to_dict(self):
        return asdict(self)
