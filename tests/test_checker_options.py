import pytest

from namehunt import cli


def test_rdap_options_reach_checker(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "check_domains", lambda *args, **kw: seen.update(kw) or {})
    args = cli._build_parser().parse_args(["run", "--rdap-rps", "0.4", "--rdap-concurrency", "1"])
    cli._validate_counts(args)
    cli._build_checker(False, args.rdap_rps, args.rdap_concurrency)(["oak"], ["cz"])
    assert seen["max_in_flight"] == 1
    assert seen["limiter"]._interval == 2.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_rdap_rate_rejected(value):
    args = cli._build_parser().parse_args(["run", "--rdap-rps", value])
    with pytest.raises(ValueError, match="rdap-rps"):
        cli._validate_counts(args)


def test_invalid_concurrency_rejected():
    args = cli._build_parser().parse_args(["resume", "--session", "x", "--rdap-concurrency", "0"])
    with pytest.raises(ValueError, match="rdap-concurrency"):
        cli._validate_counts(args)
