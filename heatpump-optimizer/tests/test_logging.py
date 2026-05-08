from packages.core import logging as logging_module


def test_configure_logging_delegates_to_log_sink(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        logging_module,
        "configure_structlog_with_db",
        lambda service_name: calls.append(service_name),
    )

    logging_module.configure_logging("optimizer")

    assert calls == ["optimizer"]
