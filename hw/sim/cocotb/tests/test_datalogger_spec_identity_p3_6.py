from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[4]
DATALOGGER_ROOT = REPO_ROOT / "hw" / "rtl" / "datalogger"
STANDARD_PROTOCOLS = ("uart", "i2c", "qspi", "jtag")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return yaml.safe_load(fh)


def _registry_domain_codes():
    system_ids = _load_yaml(REPO_ROOT / "system_ids.yml")
    return {
        domain["code"]
        for domain in system_ids["domains"].values()
    }


def _datalogger_protocol_specs():
    return sorted(DATALOGGER_ROOT.glob("**/*_datalogger_protocol.yml"))


def test_declared_domains_match_registry():
    registry_codes = _registry_domain_codes()

    for spec_path in _datalogger_protocol_specs():
        spec = _load_yaml(spec_path)
        identity = spec.get("identity", {}) or {}
        if "domain" not in identity:
            continue

        assert identity["domain"] in registry_codes, (
            f"{spec_path.relative_to(REPO_ROOT)} declares identity.domain "
            f"{identity['domain']:#x}, which is not in system_ids.yml domains"
        )


def test_standard_protocol_specs_declare_identity_domain():
    for proto in STANDARD_PROTOCOLS:
        spec_path = DATALOGGER_ROOT / proto / f"{proto}_datalogger_protocol.yml"
        spec = _load_yaml(spec_path)

        assert spec.get("identity", {}).get("domain") is not None, (
            f"{spec_path.relative_to(REPO_ROOT)} must declare identity.domain"
        )
