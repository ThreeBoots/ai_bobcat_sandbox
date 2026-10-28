"""Stand-in for the Claude Science `host` object, so the BOBcat pipeline runs outside the app.

Implements the three calls the pipeline uses:
  host.llm(...)            -> Anthropic Messages API (your own ANTHROPIC_API_KEY)
  host.list_models()       -> model ids available to your key
  host.reasoning_model()   -> fallback model id (BOBCAT_FALLBACK_MODEL, else the first Sonnet id)
  requests without a model use utility_model() (BOBCAT_UTILITY_MODEL, else the first Haiku id), as in the app
Return shapes follow the app's host.llm: a single request returns a dict and raises RuntimeError on failure;
a list returns one dict per request, with {"error": str} for requests that failed.
"""
import os, time
from concurrent.futures import ThreadPoolExecutor

import anthropic

DEFAULT_MAX_TOKENS = 32000
RETRIES = 4                      # retries on rate-limit / overload / connection errors (exponential backoff)


class AnthropicHost:
    def __init__(self, api_key=None, cache_system=True, max_concurrency=8):
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"), max_retries=0)
        self.cache_system = cache_system          # mark the system prompt for prompt caching (cheaper repeat calls)
        self.default_concurrency = max_concurrency
        self._models = None
        self.usage_log = []                       # one record per API call: model, tokens, seconds, error

    # ---- model ids -------------------------------------------------------------------------------------
    def list_models(self):
        if self._models is None:
            self._models = [m.id for m in self.client.models.list(limit=1000)]
        return self._models

    def utility_model(self):
        """Model used when a request names none (the app's small default): BOBCAT_UTILITY_MODEL, else the first Haiku id."""
        env = os.environ.get("BOBCAT_UTILITY_MODEL")
        if env: return env
        ids = self.list_models()
        return next((m for m in ids if "haiku" in m), self.reasoning_model())

    def reasoning_model(self):
        env = os.environ.get("BOBCAT_FALLBACK_MODEL")
        if env: return env
        ids = self.list_models()
        return next((m for m in ids if "sonnet" in m), ids[0])

    # ---- llm ------------------------------------------------------------------------------------------
    def llm(self, request=None, system=None, model=None, max_tokens=None, max_concurrency=None, **options):
        if isinstance(request, list):
            reqs = [self._norm(r) for r in request]
            n = max(1, min(32, max_concurrency or self.default_concurrency))
            def one(r):
                try: return self._call(r)
                except Exception as e: return {"error": f"{type(e).__name__}: {e}"}
            with ThreadPoolExecutor(max_workers=n) as ex:
                return list(ex.map(one, reqs))
        r = self._norm(request if request is not None else {})
        for k, v in dict(system=system, model=model, max_tokens=max_tokens, **options).items():
            if v is not None: r[k] = v
        try:
            return self._call(r)
        except Exception as e:
            raise RuntimeError(f"{type(e).__name__}: {e}") from e

    @staticmethod
    def _norm(r):
        if isinstance(r, str): return {"prompt": r}
        return dict(r)

    def _call(self, r):
        model = r.get("model") or self.utility_model()
        messages = r.get("messages") or [{"role": "user", "content": r.get("prompt", "")}]
        kw = dict(model=model, max_tokens=int(r.get("max_tokens") or DEFAULT_MAX_TOKENS), messages=messages)
        if r.get("system"):
            kw["system"] = ([{"type": "text", "text": r["system"], "cache_control": {"type": "ephemeral"}}]
                            if self.cache_system else r["system"])
        for k in ("tools", "tool_choice", "thinking", "temperature"):
            if r.get(k) is not None: kw[k] = r[k]
        t0, err = time.time(), None
        for attempt in range(RETRIES + 1):
            try:
                with self.client.messages.stream(**kw) as s:      # streaming avoids timeouts on long outputs
                    msg = s.get_final_message()
                break
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                err = e
                if attempt == RETRIES: raise
                time.sleep(min(60, 5 * 2 ** attempt))
            except anthropic.APIStatusError as e:
                if getattr(e, "status_code", None) == 529 and attempt < RETRIES:     # overloaded
                    time.sleep(min(60, 5 * 2 ** attempt)); continue
                raise
        content, text, tool_use = [], "", None
        for b in msg.content:
            if b.type == "text":
                text += b.text; content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                d = {"type": "tool_use", "name": b.name, "input": b.input}; content.append(d)
                if tool_use is None: tool_use = {"name": b.name, "input": b.input}
            elif b.type == "thinking":
                content.append({"type": "thinking", "thinking": getattr(b, "thinking", "")})
        u = msg.usage
        usage = dict(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                     cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                     cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        self.usage_log.append(dict(model=msg.model, seconds=round(time.time() - t0, 1), **usage))
        return {"text": text, "tool_use": tool_use, "content": content, "model": msg.model,
                "usage": usage, "stop_reason": msg.stop_reason}
