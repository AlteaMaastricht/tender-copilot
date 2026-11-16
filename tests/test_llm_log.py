"""Call log, payload storage and prices."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from llm.log import PAYLOAD_RETENTION_DAYS, JsonlCallLog, LlmCallRecord, PayloadStore
from llm.prices import PRICES
from llm.prompts import PromptRef, UntrustedText
from llm.providers.base import ProviderError, Usage
from llm.redact import Redactor
from pipeline.schemas import ScreeningOutput
from tests.fakes import (
    CONTEXT,
    HAIKU,
    RUN_ID,
    SONNET,
    FakeProvider,
    http_error,
    make_gateway,
    reply,
)

KEY = "sk-ant-api03-THIS-IS-A-FAKE-KEY-0123456789"
LLM_CALLS_COLUMNS = {
    "run_id", "eval_run_id", "tender_id", "stage", "provider", "model", "prompt_name",
    "prompt_version", "step_index", "input_tokens", "output_tokens", "cached_tokens", "cost_usd",
    "latency_ms", "tool_calls", "stop_reason", "validation_retries", "error", "payload_path",
    "network_retries", "backoff_ms", "status",
}  # fmt: skip
DIRTY = "Contact: Jan de Vries, jan@voorbeeld.nl, 06-12345678."


def screen(gateway, model=HAIKU, redactor=None, text="Dataplatform voor een gemeente."):
    return gateway.call(
        task="screening",
        prompt=PromptRef("screen", "v1"),
        variables={"company": "Datalink B.V."},
        data=[UntrustedText("notice", text)],
        schema=ScreeningOutput,
        model=model,
        redactor=redactor or Redactor(),
        context=CONTEXT,
    )


# --- records ---------------------------------------------------------------------


def test_record_has_every_llm_calls_column():
    fields = set(LlmCallRecord.__dataclass_fields__)
    assert fields == LLM_CALLS_COLUMNS | {"created_at"}
    assert not {"api_key", "headers", "system", "messages", "prompt"} & fields


def test_one_record_per_call_with_the_fields_filled(tmp_path):
    ticks = iter([10.0, 10.25])
    gateway, log = make_gateway(tmp_path, FakeProvider(reply()), clock=lambda: next(ticks))
    screen(gateway)
    [record] = log.records
    assert record.run_id == RUN_ID and record.tender_id == 42 and record.eval_run_id is None
    assert (record.stage, record.provider, record.model) == ("screening", "anthropic", HAIKU)
    assert (record.prompt_name, record.prompt_version, record.step_index) == ("screen", "v1", 0)
    assert (record.input_tokens, record.output_tokens, record.cached_tokens) == (1000, 200, 0)
    assert record.cost_usd == Decimal("0.000200")  # 1000 * 0.10 + 200 * 0.50, per million
    assert record.latency_ms == 250
    assert (record.status, record.stop_reason, record.error) == ("ok", "end", None)
    assert (record.validation_retries, record.network_retries) == (0, 0)
    assert record.backoff_ms == 0
    assert record.tool_calls == [] and record.payload_path is None


class SleepingClock:
    """A clock that a fake `sleep` moves forward, as real waiting would."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SlowProvider(FakeProvider):
    """Every send, failed or not, takes `seconds` on the clock."""

    def __init__(self, clock: SleepingClock, seconds: float, *script) -> None:
        super().__init__(*script)
        self._clock, self._seconds = clock, seconds

    def send(self, request):
        self._clock.now += self._seconds
        return super().send(request)


def test_latency_times_only_the_sends_and_the_backoff_wait_is_logged_apart(tmp_path):
    clock = SleepingClock()
    provider = SlowProvider(clock, 1.0, http_error(429, retry_after=30), reply())
    gateway, log = make_gateway(tmp_path, provider, clock=clock, sleep=clock.sleep)
    screen(gateway)
    [record] = log.records
    assert record.network_retries == 1
    assert record.latency_ms == 2000  # two sends of one second each; was 32000 with the wait
    assert record.backoff_ms == 30_000
    assert clock.now == 132.0  # the wait did happen


def test_backoff_is_summed_over_retries_and_logged_when_the_call_fails(tmp_path):
    clock = SleepingClock()
    provider = SlowProvider(clock, 0.5, *[http_error(503)] * 4)
    gateway, log = make_gateway(tmp_path, provider, clock=clock, sleep=clock.sleep)
    with pytest.raises(ProviderError):
        screen(gateway)
    [record] = log.records
    assert (record.status, record.network_retries) == ("provider_error", 3)
    assert record.latency_ms == 2000  # four sends of half a second
    assert record.backoff_ms == 7000  # 1 + 2 + 4 seconds, jitter at its maximum in tests


def test_tokens_and_cost_are_summed_over_a_validation_retry(tmp_path):
    gateway, log = make_gateway(tmp_path, FakeProvider(reply("not json"), reply()))
    screen(gateway)
    [record] = log.records
    assert (record.input_tokens, record.output_tokens) == (2000, 400)
    assert record.cost_usd == Decimal("0.000400")


def test_cached_tokens_are_counted_in_input_tokens(tmp_path):
    usage = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=900, cache_write_tokens=50)
    gateway, log = make_gateway(tmp_path, FakeProvider(reply(usage=usage)))
    screen(gateway)
    assert (log.records[0].input_tokens, log.records[0].cached_tokens) == (1050, 900)


def test_a_key_in_an_error_message_never_reaches_the_log(tmp_path):
    other = "another-secret-value-123"
    error = ProviderError(f"401 invalid x-api-key {KEY} (Bearer {other})", status_code=401)
    gateway, log = make_gateway(tmp_path, FakeProvider(error), secrets=[other])
    sink = JsonlCallLog(tmp_path / "calls.jsonl")
    with pytest.raises(ProviderError):
        screen(gateway)
    sink.write(log.records[0])
    for text in (log.records[0].error, sink.path.read_text(), repr(log.records[0])):
        assert KEY not in text and other not in text and "sk-ant" not in text
    assert log.records[0].error == "ProviderError 401: 401 invalid x-api-key [KEY] (Bearer [KEY])"


def test_keys_are_read_from_the_environment_for_scrubbing(tmp_path, monkeypatch):
    from llm.client import Gateway
    from llm.log import MemoryCallLog
    from tests.fakes import make_prompts_dir

    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-fake-key-0123456789")
    log = MemoryCallLog()
    error = http_error(403)
    error.args = ("forbidden for mistral-fake-key-0123456789",)
    gateway = Gateway(
        {"anthropic": FakeProvider(error)}, log, prompts_dir=make_prompts_dir(tmp_path)
    )
    with pytest.raises(ProviderError):
        screen(gateway)
    assert log.records[0].error == "ProviderError 403: forbidden for [KEY]"


def test_error_text_is_shortened(tmp_path):
    gateway, log = make_gateway(tmp_path, FakeProvider(ProviderError("x" * 5000, status_code=400)))
    with pytest.raises(ProviderError):
        screen(gateway)
    assert len(log.records[0].error) == 300


def test_jsonl_sink_writes_one_valid_line_per_call(tmp_path):
    gateway, log = make_gateway(tmp_path, FakeProvider(reply(), http_error(400)))
    sink = JsonlCallLog(tmp_path / "nested" / "calls.jsonl")
    screen(gateway)
    with pytest.raises(ProviderError):
        screen(gateway)
    for record in log.records:
        sink.write(record)
    rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["ok", "provider_error"]
    assert rows[0]["run_id"] == str(RUN_ID) and rows[0]["cost_usd"] == "0.000200"
    assert set(rows[0]) == set(LlmCallRecord.__dataclass_fields__)


# --- payloads --------------------------------------------------------------------


def test_no_payload_is_stored_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PAYLOAD_DIR", raising=False)
    gateway, log = make_gateway(tmp_path, FakeProvider(reply()))
    screen(gateway)
    assert log.records[0].payload_path is None
    assert not list(tmp_path.rglob("*.json"))


def test_payloads_are_stored_redacted_when_configured(tmp_path):
    store = PayloadStore(tmp_path / "payloads")
    echo = reply({**{"decision": "reject", "matched_services": [], "exclusion_flags": ["none"]},
                  "reason": DIRTY, "confidence": 0.5})  # fmt: skip
    gateway, log = make_gateway(tmp_path, FakeProvider(echo), payload_store=store)
    screen(gateway, redactor=Redactor(["Jan de Vries"]), text=DIRTY)

    path = log.records[0].payload_path
    assert path is not None and path.startswith(str(tmp_path / "payloads"))
    stored = (tmp_path / "payloads").rglob("*.json")
    [text] = [file.read_text() for file in stored]
    for planted in ("Jan de Vries", "jan@voorbeeld.nl", "06-12345678"):
        assert planted not in text
    payload = json.loads(text)
    assert payload["stage"] == "screening" and len(payload["exchanges"]) == 1
    assert "[NAME]" in payload["exchanges"][0]["messages"][0]["text"]
    assert "[EMAIL]" in payload["exchanges"][0]["response"]["text"]


def test_payload_directory_can_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PAYLOAD_DIR", str(tmp_path / "from_env"))
    gateway, log = make_gateway(tmp_path, FakeProvider(reply()))
    screen(gateway)
    assert log.records[0].payload_path.startswith(str(tmp_path / "from_env"))


def test_purge_removes_only_dated_folders_older_than_the_retention(tmp_path):
    assert PAYLOAD_RETENTION_DAYS == 30
    store = PayloadStore(tmp_path / "payloads")
    today = date(2026, 11, 14)
    for day in (date(2026, 10, 14), date(2026, 10, 15), date(2026, 11, 14)):
        store.write({"x": 1}, today=day)
    (store.root / "notes").mkdir()
    (store.root / "2026-01-01.txt").write_text("keep")

    assert store.purge(today=today) == ["2026-10-14"]  # 31 days old; 30 days old is kept
    assert sorted(p.name for p in store.root.iterdir()) == [
        "2026-01-01.txt", "2026-10-15", "2026-11-14", "notes",
    ]  # fmt: skip
    assert PayloadStore(tmp_path / "missing").purge() == []


# --- prices ----------------------------------------------------------------------


def test_every_price_row_has_a_source_and_a_date():
    assert set(PRICES) == {HAIKU, SONNET}
    for price in PRICES.values():
        assert price.source.startswith("https://") and price.as_of == date(2026, 11, 14)
        assert price.provider == "anthropic"


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        (HAIKU, Usage(50_000, 2_000), "0.006000"),  # 50k * 0.10 + 2k * 0.50
        (HAIKU, Usage(100_000, 0), "0.010000"),  # exactly at the threshold: normal rate
        (HAIKU, Usage(100_001, 0), "0.050001"),  # over it: 0.50 per million (0.0500005 rounded)
        (HAIKU, Usage(10_000, 1_000, cache_read_tokens=95_000), "0.012250"),  # long: 105k prompt
        (HAIKU, Usage(1_000, 100, cache_read_tokens=9_000, cache_write_tokens=2_000), "0.000490"),
        (SONNET, Usage(50_000, 2_000), "0.120000"),  # 50k * 2 + 2k * 10
        (SONNET, Usage(500_000, 0), "1.000000"),  # no long-prompt rule
        (SONNET, Usage(1_000, 100, cache_read_tokens=9_000, cache_write_tokens=2_000), "0.008900"),
        (SONNET, Usage(0, 0), "0.000000"),
        (SONNET, Usage(1, 0), "0.000002"),
    ],
)
def test_cost_is_exact(model, usage, expected):
    cost = PRICES[model].cost(usage)
    assert isinstance(cost, Decimal) and cost == Decimal(expected)


def test_sonnet_calls_are_priced_at_sonnet_rates(tmp_path):
    gateway, log = make_gateway(tmp_path, FakeProvider(reply(model=SONNET)))
    screen(gateway, model=SONNET)
    assert log.records[0].cost_usd == Decimal("0.004000")  # 1000 * 2 + 200 * 10, per million
