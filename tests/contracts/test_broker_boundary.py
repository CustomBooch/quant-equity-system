"""Only quant.execution.engine can send anything to the broker. Every live module under lab/ and src/ is read (AST, not imported): an HTTP request that writes (a
Request with a body or a POST, PUT or DELETE method, or requests/httpx post, put, delete) or a mention of the order endpoint is allowed in src/quant/execution/engine.py
alone, behind its gates. A module in LOOPBACK_ONLY may write to a server on this machine (the local language model) as long as every URL in it is a
loopback address; one URL elsewhere puts it back under the rule."""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ALLOWED = {"src/quant/execution/engine.py"}
WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
# quant.features.news.llm_sentiment POSTs prompts to Ollama on 127.0.0.1 (added 2026-10-31; the contract had failed since 7b1bd56)
LOOPBACK_ONLY = {"src/quant/features/news/llm_sentiment.py"}
LOOPBACK = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


def broker_writes(source: str) -> list[str]:
    """Line-numbered descriptions of every writing request or order-endpoint string in `source`."""
    found = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "/orders" in n.value and ("equity" in n.value or "/v2/orders" in n.value):
            found.append(f"{n.lineno}: order endpoint {n.value!r}")          # Trading 212 `.../equity/orders/...`; Alpaca `/v2/orders` (amendment A10)
        if not isinstance(n, ast.Call):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        owner = getattr(n.func.value, "id", "") if isinstance(n.func, ast.Attribute) else ""
        kw = {k.arg: k.value for k in n.keywords}
        method = kw.get("method")
        if name == "Request" and ("data" in kw or len(n.args) > 1 or (isinstance(method, ast.Constant) and str(method.value).upper() in WRITE_METHODS)):
            found.append(f"{n.lineno}: writing urllib Request")
        if owner in ("requests", "httpx") and name in ("post", "put", "delete", "patch"):
            found.append(f"{n.lineno}: {owner}.{name}")
    return found


def only_loopback(source: str) -> bool:
    """True when `source` names at least one URL and every URL it names (any string constant containing "://") is on this machine."""
    urls = [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and "://" in n.value]
    return bool(urls) and all(u.startswith(LOOPBACK) for u in urls)


def live_modules():
    """Every module under lab/ and src/ (the quant package), tests aside."""
    for p in sorted([*(REPO / "lab").rglob("*.py"), *(REPO / "src").rglob("*.py")]):
        rel = p.relative_to(REPO).as_posix()
        if "/tests/" not in rel:
            yield rel, p


def test_no_module_but_the_engine_can_send_to_the_broker():
    offenders = {rel: hits for rel, p in live_modules() if rel not in ALLOWED and (hits := broker_writes(src := p.read_text(encoding="utf-8")))
                 and not (rel in LOOPBACK_ONLY and only_loopback(src))}
    assert offenders == {}


def test_the_loopback_exemption_holds_only_while_every_url_is_on_this_machine():
    assert all(only_loopback((REPO / rel).read_text(encoding="utf-8")) for rel in LOOPBACK_ONLY)
    assert only_loopback('URL = "http://127.0.0.1:11434/api/generate"\n')
    assert not only_loopback('URL = "http://127.0.0.1:11434/api/generate"\nOTHER = "https://live.trading212.com/api/v0"\n')
    assert not only_loopback('x = 1\n')                                           # no URL at all: the URL may come from outside, so no exemption


def test_the_engine_is_where_orders_are_sent():
    assert broker_writes((REPO / "src/quant/execution/engine.py").read_text(encoding="utf-8"))


def test_the_scan_recognises_each_way_of_sending():
    planted = ('import urllib.request, requests\n'
               'urllib.request.Request("https://x/y", data=b"{}")\n'
               'urllib.request.Request("https://x/y", None, {}, method="DELETE")\n'
               'requests.post("https://x/y")\n'
               'PATH = "/api/v0/equity/orders/limit"\n')
    assert [h.split(":", 1)[0] for h in broker_writes(planted)] == ["2", "3", "4", "5"]
    assert broker_writes('import urllib.request\nurllib.request.Request("https://x/equity/positions")\n') == []
    assert [h.split(":", 1)[0] for h in broker_writes('ORDERS = "/v2/orders"\n')] == ["1"]                       # Alpaca's order endpoint (amendment A10)
    assert broker_writes('import urllib.request\nurllib.request.Request("https://paper-api.alpaca.markets/v2/account", method="GET")\n') == []


def test_the_read_only_alpaca_module_cannot_send():
    assert broker_writes((REPO / "src/quant/execution/brokers/alpaca/api.py").read_text(encoding="utf-8")) == []
