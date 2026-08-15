"""Live GitHub stub availability test (not marked live_github)."""


def test_live_github_stub_unavailable() -> None:
    from openflywheel.connectors.github.live import LiveGitHubClientStub

    client = LiveGitHubClientStub()
    report = client.capability_report()
    assert report.available is False
    assert client.list_scan_items() == tuple()
