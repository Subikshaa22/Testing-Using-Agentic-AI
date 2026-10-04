"""Minimal LLM clients built from first principles (no LangChain/LangGraph).

`OpenAICompatibleClient` talks to any /chat/completions endpoint (OpenRouter,
KiloAI, Groq, a local Ollama/LM Studio server, ...) using only the stdlib.
`MockClient` returns deterministic answers so the pipeline can be demoed or
tested offline without an API key.

Every request/response is appended to a JSONL log so the exact prompts and
settings can be included in the project report.
"""
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "cse731-agentic-unit-testing/1.0"
MAX_RATE_LIMIT_WAITS = 8


class LLMError(RuntimeError):
    pass


class DailyLimitError(LLMError):
    """The provider's daily free-request quota is used up; retrying is pointless."""


class BudgetExhausted(DailyLimitError):
    """The request budget set for this run (--budget) is used up."""


class BaseClient:
    """Common chat() entry point: response cache + request/response logging.

    Identical requests (same model, messages and sampling settings, including the
    seed) return the cached response instead of spending another API request.
    This lets several modes share one generated solution per task, and lets an
    interrupted run be repeated for free.
    """

    def __init__(self, model, log_path=None, cache_dir=None):
        self.model = model
        self.log_path = log_path
        self.cache_dir = cache_dir
        self.cache_hits = 0
        # Host serving the model, part of the cache key so providers never share answers.
        # Empty for OpenRouter, which keeps cache entries written before this field existed valid.
        self.provider_host = ""

    def chat(self, agent, messages, settings):
        key = self._cache_key(messages, settings)
        cached = self._cache_get(key)
        if cached is not None:
            self.cache_hits += 1
            self._log(agent, messages, settings, cached, 0.0, cached=True)
            return cached
        start = time.time()
        content = self._complete(messages, settings)
        self._cache_put(key, content)
        self._log(agent, messages, settings, content, time.time() - start)
        return content

    def _complete(self, messages, settings):
        raise NotImplementedError

    def is_cached(self, messages, settings):
        return self._cache_get(self._cache_key(messages, settings)) is not None

    # ---- cache --------------------------------------------------------------- #
    def _cache_key(self, messages, settings):
        key = {"model": self.model, "messages": messages, "settings": settings.to_dict()}
        if self.provider_host:
            key["provider"] = self.provider_host
        blob = json.dumps(key, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_get(self, key):
        if not self.cache_dir:
            return None
        path = os.path.join(self.cache_dir, key + ".json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)["response"]

    def _cache_put(self, key, content):
        if not self.cache_dir:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, key + ".json"), "w", encoding="utf-8") as f:
            json.dump({"model": self.model, "provider": self.provider_host or "openrouter.ai",
                       "response": content}, f)

    def _log(self, agent, messages, settings, content, elapsed, cached=False):
        if not self.log_path:
            return
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": agent,
            "model": self.model,
            "settings": settings.to_dict(),
            "messages": messages,
            "response": content,
            "latency_s": round(elapsed, 2),
            "cached": cached,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


class OpenAICompatibleClient(BaseClient):
    def __init__(self, model, base_url, api_key, log_path=None, cache_dir=None, budget=None, retries=4):
        super().__init__(model, log_path, cache_dir)
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.retries = retries
        self.budget = budget          # max HTTP requests this run may send (None = unlimited)
        self.requests_sent = 0
        host = urllib.parse.urlparse(base_url).hostname or ""
        self.provider_host = "" if host.endswith("openrouter.ai") else host

    def _send(self, req):
        if self.budget is not None and self.requests_sent >= self.budget:
            raise BudgetExhausted(f"request budget of {self.budget} used up")
        self.requests_sent += 1
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _complete(self, messages, settings):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "max_tokens": settings.max_tokens,
            "seed": settings.seed,
        }
        # Turn off the model's built-in "thinking": the project forbids chain-of-thought,
        # and reasoning tokens can use up max_tokens before any answer is written.
        payload.update(_no_reasoning_params(self.url, self.model))
        headers = {
            "Content-Type": "application/json",
            # Some providers (Groq, behind Cloudflare) reject urllib's default "Python-urllib"
            # User-Agent with HTTP 403 "error code: 1010".
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.api_key}",
            # Optional OpenRouter attribution headers
            "HTTP-Referer": "https://github.com/cse731-agentic-unit-testing",
            "X-Title": "CSE731 Agentic Unit Testing",
        }
        last_err = None
        empty_responses = 0
        failures = 0        # network errors, 5xx, empty answers: up to self.retries attempts
        rate_limited = 0    # per-minute rate limits (429): waited out, up to MAX_RATE_LIMIT_WAITS times
        while failures < self.retries and rate_limited <= MAX_RATE_LIMIT_WAITS:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            wait = min(2 ** failures * 5, 60)
            try:
                body = self._send(req)
                if "error" in body:
                    raise LLMError(str(body["error"]))
                choice = body["choices"][0]
                content = choice["message"].get("content") or ""
                if not content.strip():
                    # Usually a reasoning model that spent all of max_tokens thinking
                    # (finish_reason == "length"), or a provider glitch.
                    empty_responses += 1
                    err = LLMError(f"empty response from model "
                                   f"(finish_reason={choice.get('finish_reason')}, "
                                   f"reasoning_returned={bool(choice['message'].get('reasoning'))})")
                    if empty_responses >= 2:   # retrying the same request again rarely helps
                        raise _GiveUp(err)
                    raise err
                return content
            except _GiveUp as e:
                raise e.error
            except DailyLimitError:
                raise
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                last_err = LLMError(f"HTTP {e.code}: {detail}")
                # OpenRouter: "free-models-per-day"; Groq: "... on requests per day (RPD)"
                if e.code == 429 and re.search(r"per[- ]day|\(RPD\)", detail):
                    raise DailyLimitError(f"daily free-request limit reached: {detail[:200]}")
                if e.code == 400 and "reasoning" in detail and _drop_reasoning_params(payload):
                    continue  # this model rejects the no-thinking setting: retry without it
                # 429 (rate limit) and 5xx are worth retrying; 4xx otherwise is not
                if e.code != 429 and e.code < 500:
                    raise last_err
                if e.code == 429:
                    # Per-minute limits (requests, tokens, or Groq's output tokens per minute)
                    # reset within a minute: wait for the window instead of failing the task.
                    rate_limited += 1
                    retry_after = e.headers.get("retry-after") if e.headers else None
                    hinted = float(retry_after) + 1 if retry_after and                         retry_after.replace(".", "", 1).isdigit() else 0
                    wait = min(max(hinted, 20 * rate_limited), 90)
                    print(f"      rate limited by the provider - waiting {wait:.0f}s", flush=True)
                    time.sleep(wait)
                    continue
                failures += 1
            except (OSError, http.client.HTTPException, LLMError, KeyError, IndexError,
                    json.JSONDecodeError, UnicodeDecodeError) as e:
                # OSError covers URLError, timeouts and connection resets;
                # HTTPException covers truncated responses (IncompleteRead).
                last_err = LLMError(f"{type(e).__name__}: {e}")
                failures += 1
            time.sleep(wait)
        raise last_err


_REASONING_KEYS = ("reasoning", "reasoning_effort", "reasoning_format")


def _no_reasoning_params(url, model):
    """Provider-specific request fields that switch a model's built-in reasoning off."""
    if "openrouter.ai" in url:
        return {"reasoning": {"enabled": False}}
    if "groq.com" in url:
        if "gpt-oss" in model:
            return {"reasoning_effort": "low"}   # gpt-oss always reasons; keep it minimal
        if "qwen" in model:
            return {"reasoning_effort": "none"}
    return {}


def _drop_reasoning_params(payload):
    """Remove the no-reasoning fields; returns True if there was anything to remove."""
    removed = [k for k in _REASONING_KEYS if payload.pop(k, None) is not None]
    return bool(removed)


class _GiveUp(Exception):
    def __init__(self, error):
        self.error = error


def free_requests_remaining(base_url, api_key):
    """Remaining free-model requests today according to OpenRouter (None if unknown)."""
    if "openrouter.ai" not in base_url:
        return None
    req = urllib.request.Request(base_url.rstrip("/") + "/key",
                                 headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})
        return data.get("free_model_daily_requests", {}).get("remaining")
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        return None


class MockClient(BaseClient):
    """Offline stand-in for an LLM.

    Code generator -> returns the MBPP reference solution.
    Test generator -> converts the MBPP asserts into pytest functions.
    The pipeline sets `current_task` before each task is processed.
    """

    def __init__(self, log_path=None):
        super().__init__("mock", log_path)
        self.current_task = None

    def _complete(self, messages, settings):
        task = self.current_task
        system = messages[0]["content"]
        if "code generator" in system.lower():
            return f"```python\n{task['code'].strip()}\n```"
        # Property mode requires tests named test_p<k>_...; map every example to P1.
        prefix = "test_p1_" if "user-specified properties" in system else "test_"
        tests = []
        for i, assertion in enumerate(task["test_list"], 1):
            tests.append(f"def {prefix}dataset_example_{i}():\n    {assertion.strip()}\n")
        return "```python\nfrom solution import *\n\n\n" + "\n\n".join(tests) + "```"


def load_dotenv(path):
    """Tiny .env reader so no extra dependency is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def extract_code_block(text):
    """Return the largest ```python fenced block (or the whole text if none)."""
    # Some free models emit <think>...</think> before the answer; ignore it.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"
